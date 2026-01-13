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


load_dotenv()

logger = logging.getLogger("qrls.unretire")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


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


class Unretire(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Permissions / Discord
        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.waivers_role_id = _get_env_int("WAIVERS_ROLE_ID")
        self.retired_role_id = _get_env_int("RETIRED_ROLE_ID")
        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        # Google Sheets (match your add.py style)
        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")

        # Target UserInfo by default
        self.worksheet_name = os.getenv("GOOGLE_WORKSHEET", "").strip() or "UserInfo"

        # Column indexes (0-based when reading arrays)
        # A=Discord ID, B=Discord Name, C=Salary, D=Team, E=Captain?
        self.COL_DISCORD_ID = 0
        self.COL_DISCORD_NAME = 1
        self.COL_SALARY = 2
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

    def _get_gspread_client(self) -> gspread.Client:
        """
        Supports GOOGLE_SERVICE_ACCOUNT_JSON as:
        - a file path, OR
        - raw json content (string starting with '{')
        """
        if not self.sa_json:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing from .env")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        sa_val = self.sa_json.strip()
        if sa_val.startswith("{"):
            info = json.loads(sa_val)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            return gspread.authorize(creds)

        if not os.path.exists(sa_val):
            raise RuntimeError(f"Service account json not found at path: {sa_val}")

        return gspread.service_account(filename=sa_val)

    def _open_worksheet(self):
        if not self.sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is missing from .env")
        if not self.worksheet_name:
            raise RuntimeError("GOOGLE_WORKSHEET is missing from .env")

        gc = self._get_gspread_client()
        sh = gc.open_by_key(self.sheet_id)
        ws = sh.worksheet(self.worksheet_name)
        return ws

    def _find_row_index_by_discord_id(self, values: list[list[str]], discord_id: int) -> Optional[int]:
        """
        Returns 1-based row index for gspread (since update_cell uses 1-based indexes).
        """
        target = str(discord_id)
        for i, row in enumerate(values, start=1):
            if len(row) > self.COL_DISCORD_ID and _normalize(row[self.COL_DISCORD_ID]) == target:
                return i
        return None

    async def _post_transaction_log(self, player: discord.Member):
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping transaction log post.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        await ch.send(f"{player.mention} has unretired and will be placed on 2 Day Waivers.")

    async def _apply_waivers_role(self, guild: discord.Guild, member: discord.Member) -> tuple[bool, str]:
        if not self.waivers_role_id:
            return False, "WAIVERS_ROLE_ID is missing/invalid in .env"

        role = guild.get_role(self.waivers_role_id)
        if not role:
            return False, f"Waivers role (id={self.waivers_role_id}) not found in server."

        try:
            if role in member.roles:
                return True, "Player already has Waivers role."
            await member.add_roles(role, reason="/unretire: placed on waivers")
            return True, f"Added role {role.mention}."
        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except Exception as e:
            logger.error("Adding waivers role failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while adding Waivers role (see console)."

    async def _remove_retired_role(self, guild: discord.Guild, member: discord.Member) -> tuple[bool, str]:
        """
        Remove RETIRED_ROLE_ID role if present.
        This is non-fatal: if it fails we report it, but the sheet update can still succeed.
        """
        if not self.retired_role_id:
            return False, "RETIRED_ROLE_ID is missing/invalid in .env"

        role = guild.get_role(self.retired_role_id)
        if not role:
            return False, f"Retired role (id={self.retired_role_id}) not found in server."

        try:
            if role not in member.roles:
                return True, "Player did not have Retired role."
            await member.remove_roles(role, reason="/unretire: unretired (remove Retired role)")
            return True, f"Removed role {role.mention}."
        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except Exception as e:
            logger.error("Removing retired role failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while removing Retired role (see console)."

    # ---------------------------
    # /unretire command
    # ---------------------------
    @app_commands.command(
        name="unretire",
        description="Unretire a player: set salary and place them on Waivers."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player1="Player who is unretiring",
        salary="New salary for the player"
    )
    async def unretire(self, interaction: Interaction, player1: discord.Member, salary: int):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # ----- basic checks -----
            step = "GUILD_CHECK"
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return

            # ----- permission restriction -----
            step = "ADMIN_CHECK"
            if not self.admins_role_id:
                await interaction.followup.send("❌ ADMINS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self._is_admin_member(interaction.user):
                await interaction.followup.send("🚫 Only admins can use this command.", ephemeral=True)
                return

            step = "SALARY_VALIDATE"
            if salary < 0:
                await interaction.followup.send("❌ Salary must be 0 or higher.", ephemeral=True)
                return

            # ----- open sheet -----
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_ALL"
            values = ws.get_all_values() or []

            # Find player row
            step = "FIND_PLAYER"
            player_row_index = self._find_row_index_by_discord_id(values, player1.id)

            if player_row_index:
                # Update salary (C) + team (D)
                step = "UPDATE_EXISTING"
                ws.update_cell(player_row_index, self.COL_SALARY + 1, int(salary))  # C
                ws.update_cell(player_row_index, self.COL_TEAM + 1, "Waivers")      # D
                logger.info("Updated existing UserInfo row %s for %s (%s).",
                            player_row_index, player1.display_name, player1.id)
            else:
                # Append new row:
                # A: id, B: name, C: salary, D: Waivers, E: FALSE
                step = "APPEND_NEW"
                ws.append_row([str(player1.id), player1.display_name, int(salary), "Waivers", "FALSE"])
                logger.info("Appended new UserInfo row for %s (%s).", player1.display_name, player1.id)

            # ----- remove Retired role (if configured) -----
            step = "REMOVE_RETIRED_ROLE"
            retired_ok, retired_msg = await self._remove_retired_role(interaction.guild, player1)
            if not retired_ok:
                # Non-fatal, but let the admin know
                logger.warning("Retired role removal issue: %s", retired_msg)

            # ----- apply Waivers role -----
            step = "ROLE_APPLY_WAIVERS"
            waivers_ok, waivers_msg = await self._apply_waivers_role(interaction.guild, player1)
            if not waivers_ok:
                await interaction.followup.send(
                    f"⚠️ Sheet updated, but Waivers role update failed: {waivers_msg}",
                    ephemeral=True
                )
                return

            # ----- post transaction message -----
            step = "POST_TX"
            await self._post_transaction_log(player1)

            extra = ""
            if retired_ok:
                # only mention this if we actually had RETIRED_ROLE_ID configured
                if self.retired_role_id:
                    extra = f"\n🧹 Retired role: {retired_msg}"

            await interaction.followup.send(
                f"✅ Done. {player1.mention} has been placed on **Waivers** with salary **{salary}**."
                f"{extra}",
                ephemeral=True
            )

        except Exception as e:
            logger.error("ERROR at step=%s: %r", step, e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /unretire failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Unretire(bot))
