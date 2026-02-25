import os
import json
import logging
import traceback
from typing import Optional
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from utils.team_info import TEAM_INFO


load_dotenv()

logger = logging.getLogger("qrls.unretire")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

DATA_DIR = "data"
WAIVERS_FILE = os.path.join(DATA_DIR, "waivers.json")


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


def _get_team_role_id(team_name: str) -> Optional[int]:
    """
    Same pattern as in add.py, using TEAM_INFO.
    """
    info = TEAM_INFO.get(team_name)
    if not isinstance(info, dict):
        return None
    role_id = info.get("id")
    if isinstance(role_id, int):
        return role_id
    if isinstance(role_id, str) and role_id.isdigit():
        return int(role_id)
    return None


class Unretire(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Permissions / Discord
        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.waivers_role_id = _get_env_int("WAIVERS_ROLE_ID")
        self.retired_role_id = _get_env_int("RETIRED_ROLE_ID")
        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        # Google Sheets
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

    def _count_team(self, values: list[list[str]], team_name: str) -> int:
        """
        Count how many rows currently list this team in the Team column.
        (Same style as Add._count_team; used to enforce roster limits.)
        """
        count = 0
        t_norm = _normalize(team_name)
        for row in values:
            if len(row) > self.COL_TEAM and _normalize(row[self.COL_TEAM]) == t_norm:
                count += 1
        return count

    async def _post_transaction_log(self, player: discord.Member, destination: str):
        """
        Post a message to TRANSACTIONS_CHANNEL_ID describing the unretire outcome.
        - If destination == "Waivers": old-style waivers text.
        - Else: treat destination as a team name and ping the team role if possible.
        """
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping transaction log post.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        dest_norm = _normalize(destination)
        if dest_norm.lower() == "waivers":
            msg = f"{player.mention} has unretired and will be placed on 2 Day Waivers."
            await ch.send(
                msg,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False)
            )
            return

        # Otherwise, destination is a team name
        team_name = dest_norm
        team_role = None
        team_role_id = _get_team_role_id(team_name)
        if team_role_id and ch.guild:
            team_role = ch.guild.get_role(team_role_id)
        if not team_role and ch.guild:
            team_role = discord.utils.get(ch.guild.roles, name=team_name)

        team_text = team_role.mention if team_role else f"**{team_name}**"

        msg = f"{player.mention} has unretired and has been added to {team_text}."
        await ch.send(
            msg,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False)
        )

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

    async def _apply_team_role(self, guild: discord.Guild, member: discord.Member, team_name: str) -> tuple[bool, str]:
        """
        Assign the specified team role to the member.
        Also removes the Waivers role if present.
        Retired role removal is handled separately.
        """
        try:
            team_role_id = _get_team_role_id(team_name)
            if not team_role_id:
                return False, f"Team role ID is missing/invalid in TEAM_INFO for team `{team_name}`."

            team_role = guild.get_role(team_role_id)
            if not team_role:
                return False, f"Team role for `{team_name}` (id={team_role_id}) not found in server."

            to_remove = []
            if self.waivers_role_id:
                waivers_role = guild.get_role(self.waivers_role_id)
                if waivers_role and waivers_role in member.roles:
                    to_remove.append(waivers_role)

            changes = []
            if to_remove:
                await member.remove_roles(*to_remove, reason=f"/unretire: remove Waivers for {team_name}")
                changes.append("removed Waivers")

            if team_role not in member.roles:
                await member.add_roles(team_role, reason=f"/unretire: add team role {team_name}")
                changes.append(f"added {team_role.mention}")

            if not changes:
                return True, f"No team-role changes needed for {member.mention}."

            return True, f"Updated roles for {member.mention}: " + ", ".join(changes) + "."

        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except discord.NotFound:
            return False, "Player not found in the server when attempting role update."
        except Exception as e:
            logger.error("Team role update failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while updating team roles (see console)."

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

    def _record_waiver(
        self,
        guild_id: int,
        player_id: int,
        requested_at_iso: str,
        expires_at_iso: str,
        original_team: str,
        dropped_by_id: int,
    ):
        """
        Mirror of the drop.py behavior: record waiver timing in JSON.

        Structure (by guild_id):
        {
          "123456789": [
            {
              "player_id": 111,
              "requested_at": "...",
              "expires_at": "...",
              "original_team": "Some Team",
              "dropped_by": 222
            },
            ...
          ]
        }
        """
        try:
            os.makedirs(DATA_DIR, exist_ok=True)

            data = {}
            if os.path.exists(WAIVERS_FILE):
                with open(WAIVERS_FILE, "r", encoding="utf-8") as f:
                    try:
                        data = json.load(f) or {}
                    except json.JSONDecodeError:
                        data = {}

            gkey = str(guild_id)
            entries = data.get(gkey, [])

            # Remove existing entries for this player (if any) to avoid duplicates
            entries = [e for e in entries if int(e.get("player_id", 0)) != int(player_id)]

            entries.append(
                {
                    "player_id": int(player_id),
                    "requested_at": requested_at_iso,
                    "expires_at": expires_at_iso,
                    "original_team": original_team,
                    "dropped_by": int(dropped_by_id),
                }
            )

            data[gkey] = entries

            with open(WAIVERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            logger.info(
                "Recorded waiver for player %s in guild %s (expires %s).",
                player_id,
                guild_id,
                expires_at_iso,
            )

        except Exception as e:
            logger.error("Failed to record waiver json (unretire): %r", e)
            traceback.print_exc()

    # ---------------------------
    # /unretire command
    # ---------------------------
    @app_commands.command(
        name="unretire",
        description="Unretire a player: set salary and place them on Waivers or a Team."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player1="Player who is unretiring",
        salary="New salary for the player",
        destination="Type 'Waivers' to place on waivers, or choose a team name."
    )
    async def unretire(self, interaction: Interaction, player1: discord.Member, salary: int, destination: str):
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

            step = "DEST_VALIDATE"
            dest_norm = _normalize(destination)
            if not dest_norm:
                await interaction.followup.send(
                    "❌ You must specify a destination: either 'Waivers' or a valid team name.",
                    ephemeral=True
                )
                return

            is_waivers = dest_norm.lower() == "waivers"
            team_name = None
            if not is_waivers:
                team_name = dest_norm
                # Validate team exists in TEAM_INFO
                if team_name not in TEAM_INFO:
                    await interaction.followup.send(
                        f"❌ `{team_name}` is not a recognized team in TEAM_INFO.",
                        ephemeral=True
                    )
                    return

            # ----- open sheet -----
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_ALL"
            values = ws.get_all_values() or []

            # Find player row
            step = "FIND_PLAYER"
            player_row_index = self._find_row_index_by_discord_id(values, player1.id)

            # Record original team before we overwrite it (for waivers JSON + roster logic)
            original_team = ""
            if player_row_index:
                row = values[player_row_index - 1]
                if len(row) > self.COL_TEAM:
                    original_team = _normalize(row[self.COL_TEAM])

            # ----- roster limit when going directly to a team -----
            if not is_waivers:
                step = "ROSTER_CHECK"
                roster_count = self._count_team(values, team_name)
                # If they're already listed on that team in the sheet, this doesn't increase roster size
                is_already_on_team = (player_row_index is not None and original_team == team_name)
                if roster_count >= 4 and not is_already_on_team:
                    await interaction.followup.send(
                        f"🚫 Cannot unretire {player1.mention} directly to **{team_name}**. "
                        f"The roster is full ({roster_count}/4).",
                        ephemeral=True
                    )
                    return

            # Decide target team value for the sheet
            target_team_value = "Waivers" if is_waivers else team_name

            if player_row_index:
                # Update salary (C) + team (D)
                step = "UPDATE_EXISTING"
                ws.update_cell(player_row_index, self.COL_SALARY + 1, int(salary))          # C
                ws.update_cell(player_row_index, self.COL_TEAM + 1, target_team_value)      # D
                logger.info(
                    "Updated existing UserInfo row %s for %s (%s). New team: %s",
                    player_row_index,
                    player1.display_name,
                    player1.id,
                    target_team_value,
                )
            else:
                # Append new row:
                # A: id, B: name, C: salary, D: team/waivers, E: FALSE
                step = "APPEND_NEW"
                ws.append_row([str(player1.id), player1.display_name, int(salary), target_team_value, "FALSE"])
                logger.info(
                    "Appended new UserInfo row for %s (%s) with team %s.",
                    player1.display_name,
                    player1.id,
                    target_team_value,
                )

            # ----- remove Retired role (if configured) -----
            step = "REMOVE_RETIRED_ROLE"
            retired_ok, retired_msg = await self._remove_retired_role(interaction.guild, player1)
            if not retired_ok:
                # Non-fatal, but log it
                logger.warning("Retired role removal issue: %s", retired_msg)

            extra_lines = []
            if self.retired_role_id and retired_msg:
                extra_lines.append(f"🧹 Retired role: {retired_msg}")

            if is_waivers:
                # Apply Waivers role
                step = "ROLE_APPLY_WAIVERS"
                waivers_ok, waivers_msg = await self._apply_waivers_role(interaction.guild, player1)
                if not waivers_ok:
                    await interaction.followup.send(
                        f"⚠️ Sheet updated, but Waivers role update failed: {waivers_msg}",
                        ephemeral=True
                    )
                    return
                if waivers_msg:
                    extra_lines.append(f"🎫 Waivers role: {waivers_msg}")

                # Record waiver timing in JSON (2-day waivers)
                step = "RECORD_WAIVER_JSON"
                requested_at = datetime.now(timezone.utc)
                expires_at = requested_at + timedelta(days=2)
                try:
                    self._record_waiver(
                        guild_id=interaction.guild.id,
                        player_id=player1.id,
                        requested_at_iso=requested_at.isoformat(),
                        expires_at_iso=expires_at.isoformat(),
                        original_team=original_team or "Retired",
                        dropped_by_id=interaction.user.id,
                    )
                except Exception as e:
                    logger.error("Failed to record waiver json from /unretire: %r", e)
                    traceback.print_exc()

                # ----- post transaction message -----
                step = "POST_TX_WAIVERS"
                await self._post_transaction_log(player1, "Waivers")

                extra = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
                await interaction.followup.send(
                    f"✅ Done. {player1.mention} has been placed on **Waivers** with salary **{salary}**."
                    f"{extra}",
                    ephemeral=True
                )
            else:
                # Directly place on a team (no waivers)
                step = "ROLE_APPLY_TEAM"
                team_ok, team_msg = await self._apply_team_role(interaction.guild, player1, team_name)
                if not team_ok:
                    await interaction.followup.send(
                        f"⚠️ Sheet updated, but team role update failed: {team_msg}",
                        ephemeral=True
                    )
                    return
                if team_msg:
                    extra_lines.append(f"🏷 Team role: {team_msg}")

                # ----- post transaction message -----
                step = "POST_TX_TEAM"
                await self._post_transaction_log(player1, team_name)

                extra = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
                await interaction.followup.send(
                    f"✅ Done. {player1.mention} has been added to **{team_name}** with salary **{salary}**."
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

    @unretire.autocomplete("destination")
    async def destination_autocomplete(
        self,
        interaction: Interaction,
        current: str,
    ):
        """
        Autocomplete for the 'destination' argument.

        - Always suggests "Waivers"
        - Also suggests team names from TEAM_INFO
        - Filters based on the user's current input
        - Max 25 results (Discord limit)
        """
        current_norm = (current or "").lower()
        choices: list[app_commands.Choice[str]] = []

        # Always offer "Waivers"
        waivers_label = "Waivers"
        if (
            not current_norm
            or waivers_label.lower().startswith(current_norm)
            or current_norm in waivers_label.lower()
        ):
            choices.append(app_commands.Choice(name="Waivers", value="Waivers"))

        # Add teams from TEAM_INFO (sorted for stable/nice ordering)
        for team_name in sorted(TEAM_INFO.keys()):
            if current_norm in team_name.lower():
                choices.append(app_commands.Choice(name=team_name, value=team_name))
            if len(choices) >= 25:
                break

        return choices


async def setup(bot: commands.Bot):
    await bot.add_cog(Unretire(bot))