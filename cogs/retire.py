import os
import json
import logging
import traceback
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from utils.team_info import TEAM_INFO

load_dotenv()

logger = logging.getLogger("qrls.retire")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


def log_exception(step: str, error: Exception):
    """
    Centralized exception logger for /retire.
    """
    logger.error("❌ /retire crashed at step=%s | %s: %r", step, type(error).__name__, error)
    traceback.print_exc()


def _get_env_int(name: str) -> Optional[int]:
    v = os.getenv(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _normalize(s: str) -> str:
    return (s or "").strip()


def _get_gspread_client(sa_json: str) -> gspread.Client:
    """
    Supports GOOGLE_SERVICE_ACCOUNT_JSON as:
    - a file path, OR
    - raw json content (string starting with '{')
    """
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing from .env")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    sa_val = sa_json.strip()
    if sa_val.startswith("{"):
        info = json.loads(sa_val)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)

    if not os.path.exists(sa_val):
        raise RuntimeError(f"Service account json not found at path: {sa_val}")

    return gspread.service_account(filename=sa_val)


def _get_team_role_id(team_name: str) -> Optional[int]:
    info = TEAM_INFO.get(team_name)
    if not isinstance(info, dict):
        return None
    role_id = info.get("id")
    if isinstance(role_id, int):
        return role_id
    if isinstance(role_id, str) and role_id.isdigit():
        return int(role_id)
    return None


class Retire(commands.Cog):
    """
    /retire – Admin-only command to retire a player:
      • Remove all team/waiver/free-agent/captain roles in Discord
      • Set Column D (Team) to "Retired" in the sheet
      • Set Column E (Captain) to "FALSE" in the sheet
      • Log a message in TRANSACTIONS_CHANNEL_ID (with optional reasoning)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.waivers_role_id = _get_env_int("WAIVERS_ROLE_ID")  # optional, if you use it
        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        self.worksheet_name = os.getenv("GOOGLE_WORKSHEET", "")

        # Sheet columns: A=Discord ID, D=Team, E=Captain
        self.COL_DISCORD_ID = 0
        self.COL_TEAM = 3
        self.COL_CAPTAIN = 4

    # ---------------------------
    # Helpers
    # ---------------------------
    def _has_role_id(self, member: discord.Member, role_id: int) -> bool:
        return any(r.id == role_id for r in member.roles)

    def _is_admin_member(self, member: discord.Member) -> bool:
        if getattr(member.guild_permissions, "administrator", False):
            return True
        if self.admins_role_id and self._has_role_id(member, self.admins_role_id):
            return True
        return False

    def _open_worksheet(self):
        if not self.sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is missing from .env")
        if not self.worksheet_name:
            raise RuntimeError("GOOGLE_WORKSHEET is missing from .env")

        gc = _get_gspread_client(self.sa_json)
        sh = gc.open_by_key(self.sheet_id)
        return sh.worksheet(self.worksheet_name)

    def _find_row_index_by_discord_id(self, values: list[list[str]], discord_id: int) -> Optional[int]:
        """
        Returns 1-based row index for gspread (since update_cell uses 1-based indexes).
        """
        target = str(discord_id)
        for i, row in enumerate(values, start=1):
            if len(row) > self.COL_DISCORD_ID and _normalize(row[self.COL_DISCORD_ID]) == target:
                return i
        return None

    async def _remove_team_and_special_roles(self, member: discord.Member) -> str:
        """
        Removes:
          • Any role whose ID matches TEAM_INFO's ids (team roles, Free Agent, Waivers-as-team, etc.)
          • CAPTAINS_ROLE_ID
          • WAIVERS_ROLE_ID (if present as a separate role)
        Returns a short status message.
        """
        guild = member.guild

        # Gather all possible team role IDs from TEAM_INFO
        team_role_ids: set[int] = set()
        for team_name in TEAM_INFO.keys():
            rid = _get_team_role_id(team_name)
            if rid:
                team_role_ids.add(rid)

        # Add captain role + waivers role if configured
        special_ids: set[int] = set()
        if self.captains_role_id:
            special_ids.add(self.captains_role_id)
        if self.waivers_role_id:
            special_ids.add(self.waivers_role_id)

        ids_to_remove = team_role_ids | special_ids

        roles_to_remove = [r for r in member.roles if r.id in ids_to_remove]

        if not roles_to_remove:
            return "No team/waiver/captain roles to remove."

        try:
            await member.remove_roles(
                *roles_to_remove,
                reason="/retire: removing team/waiver/free-agent/captain roles"
            )
            return f"Removed {len(roles_to_remove)} role(s) from {member.mention}."
        except discord.Forbidden:
            return "⚠️ Bot lacks permission to remove some roles (check role hierarchy/permissions)."
        except Exception as e:
            logger.error("Error removing roles in /retire: %r", e)
            traceback.print_exc()
            return "⚠️ Unexpected error while removing roles (see console)."

    async def _post_transactions_log(
        self,
        guild: discord.Guild,
        player: discord.Member,
        reason: Optional[str] = None
    ):
        """
        Posts '@player is retiring from the QRLS.' (and optional reason) to TRANSACTIONS_CHANNEL_ID.
        """
        try:
            if not self.transactions_channel_id:
                logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping retire log post.")
                return

            ch = self.bot.get_channel(self.transactions_channel_id)
            if not isinstance(ch, discord.TextChannel):
                logger.warning(
                    "TRANSACTIONS_CHANNEL_ID=%s does not resolve to a text channel; skipping.",
                    self.transactions_channel_id
                )
                return

            base_message = f"{player.mention} is retiring from the QRLS."
            if reason:
                # New line with the reasoning, like pressing Enter in a document
                base_message += f"\nReason: {reason}"

            logger.info("Posting retire log message: %s", base_message)

            await ch.send(
                content=base_message,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
            )
        except Exception as e:
            logger.error("Failed posting retire log for player_id=%s", getattr(player, "id", None))
            traceback.print_exc()

    # ---------------------------
    # /retire command
    # ---------------------------
    @app_commands.command(
        name="retire",
        description="Retire a player from the QRLS (updates sheet and removes team roles)."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player1="Player to retire from the QRLS",
        reason="Reasoning for the retirement (optional)"
    )
    async def retire(
        self,
        interaction: Interaction,
        player1: discord.Member,
        reason: Optional[str] = None
    ):
        step = "START"
        logger.info(
            "/retire invoked by user_id=%s target_id=%s reason=%r",
            getattr(interaction.user, "id", None),
            getattr(player1, "id", None),
            reason
        )
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # ---- Guild/Admin checks ----
            step = "GUILD_CHECK"
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return

            guild = interaction.guild
            actor = interaction.user

            step = "ADMIN_CHECK"
            if not self.admins_role_id:
                await interaction.followup.send(
                    "❌ ADMINS_ROLE_ID is missing/invalid in .env – cannot determine admin role.",
                    ephemeral=True
                )
                return

            if not self._is_admin_member(actor):
                await interaction.followup.send("🚫 Only admins can use this command.", ephemeral=True)
                return

            # ---- Open worksheet & locate player row ----
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_VALUES"
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            step = "FIND_ROW"
            row_index = self._find_row_index_by_discord_id(values, player1.id)
            if not row_index:
                await interaction.followup.send(
                    f"❌ `{player1.display_name}` is not found in the Google Sheet (Column A, Discord ID).",
                    ephemeral=True
                )
                return

            # ---- Update sheet (Retired / FALSE) ----
            step = "UPDATE_SHEET"
            ws.update_cell(row_index, self.COL_TEAM + 1, "Retired")
            ws.update_cell(row_index, self.COL_CAPTAIN + 1, "FALSE")

            # ---- Remove roles in Discord ----
            step = "REMOVE_ROLES"
            role_msg = await self._remove_team_and_special_roles(player1)

            # ---- Post to transactions channel ----
            step = "POST_LOG"
            await self._post_transactions_log(guild, player1, reason)

            # ---- Reply to command invoker ----
            step = "RESPOND"
            extra_reason_line = f"\n📝 Reason: {reason}" if reason else ""
            await interaction.followup.send(
                content=(
                    f"✅ {player1.mention} has been marked as **Retired** in the sheet and captain flag set to `FALSE`."
                    f"{extra_reason_line}\n"
                    f"🔧 {role_msg}"
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
            )

        except Exception as e:
            log_exception(step, e)
            try:
                await interaction.followup.send(
                    f"❌ /retire failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Retire(bot))
