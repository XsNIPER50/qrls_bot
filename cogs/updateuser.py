import os
import json
import logging
import traceback
from typing import Optional, Any

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from utils.team_info import TEAM_INFO

load_dotenv()

logger = logging.getLogger("qrls.updateuser")
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


# Build static team choices from TEAM_INFO keys
TEAM_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name=team_name, value=team_name)
    for team_name in TEAM_INFO.keys()
]


class UpdateUser(commands.Cog):
    """
    /updateuser – Admin-only command to update a player's Nickname (B), Salary (C),
    Team (D), and Captain flag (E) in the Google Sheet.
    Also posts a change log to CHANGELOG_CHANNEL_ID.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.changelog_channel_id = _get_env_int("CHANGELOG_CHANNEL_ID")

        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        self.worksheet_name = os.getenv("GOOGLE_WORKSHEET", "")

        # Sheet columns: A=Discord ID, B=Nickname, C=Salary, D=Team, E=Captain
        self.COL_DISCORD_ID = 0
        self.COL_NICKNAME = 1
        self.COL_SALARY = 2
        self.COL_TEAM = 3
        self.COL_CAPTAIN = 4

    # ---------------------------
    # Helpers
    # ---------------------------
    def _is_admin_member(self, member: discord.Member) -> bool:
        # Server administrator OR ADMINS_ROLE_ID from .env
        if getattr(member.guild_permissions, "administrator", False):
            return True
        if self.admins_role_id and any(r.id == self.admins_role_id for r in member.roles):
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

    def _safe_get(self, row: list[str], idx: int) -> str:
        return _normalize(row[idx]) if len(row) > idx else ""

    def _capture_row_state(self, values: list[list[str]], row_index_1based: int) -> dict[str, str]:
        row = values[row_index_1based - 1]
        return {
            "nickname": self._safe_get(row, self.COL_NICKNAME),
            "salary": self._safe_get(row, self.COL_SALARY),
            "team": self._safe_get(row, self.COL_TEAM),
            "captain": self._safe_get(row, self.COL_CAPTAIN),
        }

    async def _apply_team_role_change(
        self,
        guild: discord.Guild,
        player_id: int,
        old_team: str,
        new_team: str
    ) -> str:
        """
        Remove old team role and add new team role based on TEAM_INFO.
        Returns a status message (for logging / user feedback).
        """
        try:
            member = guild.get_member(player_id) or await guild.fetch_member(player_id)
        except (discord.NotFound, discord.Forbidden):
            return "⚠️ Could not update Discord roles: player not found in server."

        old_role_id = _get_team_role_id(old_team) if old_team else None
        new_role_id = _get_team_role_id(new_team) if new_team else None

        to_remove = []
        to_add = []

        if old_role_id:
            old_role = guild.get_role(old_role_id)
            if old_role and old_role in member.roles:
                to_remove.append(old_role)

        if new_role_id:
            new_role = guild.get_role(new_role_id)
            if new_role and new_role not in member.roles:
                to_add.append(new_role)

        if not to_remove and not to_add:
            return "No Discord role changes needed for this user."

        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="/updateuser: team changed (remove old team role)")
            if to_add:
                await member.add_roles(*to_add, reason="/updateuser: team changed (add new team role)")
            return "Updated Discord roles to match new team."
        except discord.Forbidden:
            return "⚠️ Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except Exception as e:
            logger.error("Role update failed in /updateuser: %r", e)
            traceback.print_exc()
            return "⚠️ Unexpected error while updating Discord roles (see console)."

    async def _post_changelog(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        player: discord.Member,
        before: dict[str, str],
        after: dict[str, str],
    ):
        """
        Posts a "User Changes" log to CHANGELOG_CHANNEL_ID, including original and updated values.
        """
        if not self.changelog_channel_id:
            logger.warning("CHANGELOG_CHANNEL_ID missing/invalid; skipping changelog post.")
            return

        ch = self.bot.get_channel(self.changelog_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("CHANGELOG_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        # Build a compact diff + full before/after blocks
        fields = [("Nickname", "nickname"), ("Salary", "salary"), ("Team", "team"), ("Captain", "captain")]
        diffs = []
        for label, key in fields:
            b = before.get(key, "")
            a = after.get(key, "")
            if b != a:
                diffs.append(f"• **{label}**: `{b or '—'}` → `{a or '—'}`")

        diff_text = "\n".join(diffs) if diffs else "• (No changes detected)"

        embed = discord.Embed(
            title="🧾 User Changes",
            description=(
                f"**Player:** {player.mention} (`{player.id}`)\n"
                f"**Changed by:** {actor.mention}\n\n"
                f"**Changes:**\n{diff_text}"
            ),
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Before",
            value=(
                f"Nickname: `{before.get('nickname') or '—'}`\n"
                f"Salary: `{before.get('salary') or '—'}`\n"
                f"Team: `{before.get('team') or '—'}`\n"
                f"Captain: `{before.get('captain') or '—'}`"
            ),
            inline=False
        )
        embed.add_field(
            name="After",
            value=(
                f"Nickname: `{after.get('nickname') or '—'}`\n"
                f"Salary: `{after.get('salary') or '—'}`\n"
                f"Team: `{after.get('team') or '—'}`\n"
                f"Captain: `{after.get('captain') or '—'}`"
            ),
            inline=False
        )

        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ---------------------------
    # /updateuser command
    # ---------------------------
    @app_commands.command(
        name="updateuser",
        description="Admin-only: Update a player's Nickname, Salary, Team, and Captain flag in the sheet."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player="Player to update (Discord user)",
        nickname="New sheet nickname",
        salary="New salary/price in the league",
        team="New team – choose from configured teams",
        captain='Set captain flag – "TRUE" or "FALSE"'
    )
    @app_commands.choices(
        team=TEAM_CHOICES,
        captain=[
            app_commands.Choice(name="TRUE", value="TRUE"),
            app_commands.Choice(name="FALSE", value="FALSE"),
        ]
    )
    async def updateuser(
        self,
        interaction: Interaction,
        player: discord.Member,  # REQUIRED
        nickname: Optional[str] = None,
        salary: Optional[int] = None,
        team: Optional[app_commands.Choice[str]] = None,
        captain: Optional[app_commands.Choice[str]] = None,
    ):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # ---- Must be in guild + admin ----
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return

            if not self.admins_role_id:
                await interaction.followup.send(
                    "❌ ADMINS_ROLE_ID is missing/invalid in .env – cannot determine admin role.",
                    ephemeral=True
                )
                return

            if not self._is_admin_member(interaction.user):
                await interaction.followup.send("🚫 Only admins can use this command.", ephemeral=True)
                return

            # ---- Must have at least one field to update ----
            if nickname is None and salary is None and team is None and captain is None:
                await interaction.followup.send(
                    "ℹ️ No changes provided. Please specify at least one field to update (nickname, salary, team, captain).",
                    ephemeral=True
                )
                return

            # ---- Open sheet ----
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_VALUES"
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            # ---- Find player row ----
            step = "FIND_PLAYER_ROW"
            row_index = self._find_row_index_by_discord_id(values, player.id)
            if not row_index:
                await interaction.followup.send(
                    f"❌ `{player.display_name}` is not found in the Google Sheet (Column A, Discord ID).",
                    ephemeral=True
                )
                return

            # Capture "before" state (for changelog + old team role removal)
            before = self._capture_row_state(values, row_index)
            old_team = before.get("team", "")

            # ---- Apply updates ----
            step = "APPLY_UPDATES"
            updates_applied: list[str] = []
            role_update_msg: Optional[str] = None

            # Nickname (Column B)
            if nickname is not None:
                ws.update_cell(row_index, self.COL_NICKNAME + 1, nickname)
                updates_applied.append(f"**Nickname (B)** → `{nickname}`")

            # Salary (Column C)
            if salary is not None:
                ws.update_cell(row_index, self.COL_SALARY + 1, str(salary))
                updates_applied.append(f"**Salary (C)** → `{salary}`")

            # Team (Column D)
            if team is not None:
                team_name = team.value
                if team_name not in TEAM_INFO:
                    await interaction.followup.send(
                        f"❌ Team `{team_name}` is not configured in `utils/team_info.py`.",
                        ephemeral=True
                    )
                    return

                ws.update_cell(row_index, self.COL_TEAM + 1, team_name)
                updates_applied.append(f"**Team (D)** → `{team_name}`")

                # Update Discord roles to match new team
                role_update_msg = await self._apply_team_role_change(
                    guild=interaction.guild,
                    player_id=player.id,
                    old_team=old_team,
                    new_team=team_name
                )

            # Captain (Column E)
            if captain is not None:
                cap_value = captain.value.upper()
                if cap_value not in ("TRUE", "FALSE"):
                    await interaction.followup.send('❌ Captain must be either "TRUE" or "FALSE".', ephemeral=True)
                    return
                ws.update_cell(row_index, self.COL_CAPTAIN + 1, cap_value)
                updates_applied.append(f"**Captain (E)** → `{cap_value}`")

            if not updates_applied:
                await interaction.followup.send(
                    "ℹ️ No changes were applied (no valid fields to update).",
                    ephemeral=True
                )
                return

            # Re-read updated row state for "after" snapshot (for changelog)
            step = "RELOAD_AFTER"
            values_after = ws.get_all_values()
            after = self._capture_row_state(values_after, row_index)

            # Post changelog (best-effort)
            step = "POST_CHANGELOG"
            try:
                await self._post_changelog(
                    guild=interaction.guild,
                    actor=interaction.user,
                    player=player,
                    before=before,
                    after=after,
                )
            except Exception as e:
                logger.error("Failed to post changelog: %r", e)
                traceback.print_exc()

            # Respond to command user
            summary_lines = [f"- {item}" for item in updates_applied]
            summary_lines.append(f"- **Old Team:** `{old_team or '—'}`")
            if team is not None:
                summary_lines.append(f"- **New Team:** `{after.get('team') or '—'}`")
            if role_update_msg:
                summary_lines.append(f"- 🔧 Roles: {role_update_msg}")

            summary = "\n".join(summary_lines)

            await interaction.followup.send(
                f"✅ Updated user data for {player.mention}:\n{summary}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
            )

        except Exception as e:
            logger.error("ERROR at step=%s: %r", step, e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /updateuser failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(UpdateUser(bot))
