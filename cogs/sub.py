import os
import json
import logging
import traceback
import asyncio
from typing import Optional
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from utils.team_info import TEAM_INFO


load_dotenv()

logger = logging.getLogger("qrls.sub")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


EASTERN = ZoneInfo("America/New_York")


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


def _is_free_agent(value: str) -> bool:
    return _normalize(value).lower() == "free agent"


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


def _next_sunday_2359(now_et: datetime) -> datetime:
    """
    Return the upcoming Sunday at 23:59 ET.
    If today is Sunday, uses today at 23:59.
    """
    # Monday=0 ... Sunday=6
    days_until_sunday = (6 - now_et.weekday()) % 7
    target_date = (now_et + timedelta(days=days_until_sunday)).date()
    return datetime.combine(target_date, time(23, 59), tzinfo=EASTERN)


class Sub(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")

        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.pending_channel_id = _get_env_int("PENDING_TRANSACTIONS_CHANNEL_ID")
        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        self.worksheet_name = os.getenv("GOOGLE_WORKSHEET", "")

        # Sheet columns: A=Discord ID, D=Team
        self.COL_DISCORD_ID = 0
        self.COL_TEAM = 3

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
        target = str(discord_id)
        for i, row in enumerate(values, start=1):
            if len(row) > self.COL_DISCORD_ID and _normalize(row[self.COL_DISCORD_ID]) == target:
                return i
        return None

    def _get_team_from_row(self, values: list[list[str]], row_index_1based: int) -> str:
        row = values[row_index_1based - 1]
        return _normalize(row[self.COL_TEAM]) if len(row) > self.COL_TEAM else ""

    async def _post_in_origin_channel(self, origin_channel_id: int, message: str):
        ch = self.bot.get_channel(origin_channel_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(message)

    async def _post_transaction_log(self, team_name: str, player: discord.Member):
        """
        Post to TRANSACTIONS_CHANNEL_ID after approval.
        Message format:
        "@Team signs @player on a sub deal"
        """
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping transaction log post.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        team_role_id = _get_team_role_id(team_name)
        team_text = f"<@&{team_role_id}>" if team_role_id else f"**{team_name}**"

        await ch.send(f"{team_text} signs {player.mention} on a sub deal")

    async def _apply_temp_team_role(
        self,
        guild: discord.Guild,
        player_id: int,
        team_name: str,
        end_dt_et: datetime
    ) -> tuple[bool, str]:
        """
        Adds team role (keeps Free Agent role), and schedules removal at end_dt_et.
        """
        try:
            team_role_id = _get_team_role_id(team_name)
            if not team_role_id:
                return False, f"Team role ID missing/invalid in TEAM_INFO for team `{team_name}`."

            team_role = guild.get_role(team_role_id)
            if not team_role:
                return False, f"Team role for `{team_name}` (id={team_role_id}) not found in server."

            member = guild.get_member(player_id) or await guild.fetch_member(player_id)

            # Add team role if needed
            if team_role not in member.roles:
                await member.add_roles(team_role, reason=f"/sub approved: temp add {team_name} until {end_dt_et.isoformat()}")

            # Schedule removal
            seconds = max(0, (end_dt_et - datetime.now(EASTERN)).total_seconds())

            async def _remove_later():
                try:
                    await asyncio.sleep(seconds)
                    # Re-fetch member to ensure current roles
                    m = guild.get_member(player_id)
                    if m is None:
                        try:
                            m = await guild.fetch_member(player_id)
                        except (discord.NotFound, discord.Forbidden):
                            return

                    if team_role in m.roles:
                        await m.remove_roles(team_role, reason=f"/sub expired: remove {team_name} temp role")
                        logger.info("Temp sub expired; removed role %s from user_id=%s", team_role.id, player_id)
                except Exception as e:
                    logger.error("Temp removal task failed: %r", e)
                    traceback.print_exc()

            self.bot.loop.create_task(_remove_later())

            return True, f"Added {team_role.mention} temporarily until {end_dt_et.strftime('%Y-%m-%d %H:%M ET')}."

        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except discord.NotFound:
            return False, "Player not found in the server when attempting role update."
        except Exception as e:
            logger.error("Temp role update failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while updating roles (see console)."

    # ---------------------------
    # Approval View
    # ---------------------------
    class ApprovalView(discord.ui.View):
        def __init__(
            self,
            cog: "Sub",
            origin_channel_id: int,
            captain_id: int,
            captain_team: str,
            player_id: int,
            player_display: str,
            end_dt_et: datetime,
        ):
            super().__init__(timeout=60 * 60)
            self.cog = cog
            self.origin_channel_id = origin_channel_id
            self.captain_id = captain_id
            self.captain_team = captain_team
            self.player_id = player_id
            self.player_display = player_display
            self.end_dt_et = end_dt_et
            self.decided = False

        async def _finalize_buttons(self, interaction: discord.Interaction, status_text: str):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            try:
                await interaction.message.edit(content=status_text, view=self)
            except discord.HTTPException:
                pass

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                try:
                    await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if not self.cog._is_admin_member(interaction.user):
                try:
                    await interaction.response.send_message("🚫 Only admins can approve/reject.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if self.decided:
                try:
                    await interaction.response.send_message("ℹ️ This transaction has already been decided.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            return True

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
        async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            approver = interaction.user

            # ACK immediately
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                # Re-check sheet status: player must still be Free Agent
                ws = self.cog._open_worksheet()
                values = ws.get_all_values()

                player_row = self.cog._find_row_index_by_discord_id(values, self.player_id)
                if not player_row:
                    try:
                        await interaction.followup.send("❌ Player not found in sheet anymore.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(self.origin_channel_id, "❌ Sub approval failed (player not found in sheet).")
                    await self._finalize_buttons(interaction, "❌ Failed (player not found in sheet).")
                    return

                player_team_current = self.cog._get_team_from_row(values, player_row)
                if not _is_free_agent(player_team_current):
                    # Auto reject
                    try:
                        await interaction.followup.send(
                            f"🚫 Auto-rejected: player is not a Free Agent (currently: {player_team_current}).",
                            ephemeral=True
                        )
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"🚫 Sub request auto-rejected: player is currently on **{player_team_current or 'Unknown'}**."
                    )
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (player not Free Agent).")
                    return

                # Apply temp team role (no sheet changes)
                role_ok, role_msg = await self.cog._apply_temp_team_role(
                    guild=interaction.guild,
                    player_id=self.player_id,
                    team_name=self.captain_team,
                    end_dt_et=self.end_dt_et
                )

                # Post transaction log
                player_member = None
                try:
                    player_member = interaction.guild.get_member(self.player_id) or await interaction.guild.fetch_member(self.player_id)
                except (discord.NotFound, discord.Forbidden):
                    player_member = None

                if isinstance(player_member, discord.Member):
                    try:
                        await self.cog._post_transaction_log(self.captain_team, player_member)
                    except Exception as e:
                        logger.error("Sub transaction log post failed: %r", e)
                        traceback.print_exc()

                try:
                    await interaction.followup.send("✅ Approved.", ephemeral=True)
                except discord.HTTPException:
                    pass

                if role_ok:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Sub approved by {approver.mention}. **{self.player_display}** has been subbed in until **Sunday 11:59pm ET**.\n"
                        f"🔧 {role_msg}"
                    )
                    await self._finalize_buttons(
                        interaction,
                        f"✅ Approved by {approver.mention} — **{self.player_display}** subbed in until Sunday 11:59pm ET"
                    )
                else:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Sub approved by {approver.mention}, but ⚠️ role update issue: {role_msg}"
                    )
                    await self._finalize_buttons(
                        interaction,
                        f"✅ Approved by {approver.mention} (⚠️ role update issue)"
                    )

            except Exception as e:
                logger.error("Approve failed: %r", e)
                traceback.print_exc()

                try:
                    await interaction.followup.send("❌ Error while approving. Check console.", ephemeral=True)
                except discord.HTTPException:
                    pass

                await self.cog._post_in_origin_channel(self.origin_channel_id, "❌ Sub approval failed due to an internal error.")
                await self._finalize_buttons(interaction, "❌ Failed (internal error).")

        @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
        async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            approver = interaction.user

            # ACK immediately
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                await interaction.followup.send("🚫 Rejected.", ephemeral=True)
            except discord.HTTPException:
                pass

            await self.cog._post_in_origin_channel(self.origin_channel_id, f"🚫 Sub request rejected by {approver.mention}.")
            await self._finalize_buttons(interaction, f"🚫 Rejected by {approver.mention}")

    # ---------------------------
    # /sub command
    # ---------------------------
    @app_commands.command(
        name="sub",
        description="Request to temporarily sub in a Free Agent until Sunday 11:59pm ET (requires Admin Approval)."
    )
    @app_commands.guild_only()
    async def sub(self, interaction: Interaction, player1: discord.Member):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # Env validation
            step = "ENV_VALIDATE"
            if not self.captains_role_id:
                await interaction.followup.send("❌ CAPTAINS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.transactions_category_id:
                await interaction.followup.send("❌ TRANSACTIONS_CATEGORY_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.pending_channel_id:
                await interaction.followup.send("❌ PENDING_TRANSACTIONS_CHANNEL_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.admins_role_id:
                await interaction.followup.send("❌ ADMINS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return

            # Captain-only
            step = "CAPTAIN_CHECK"
            if not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return
            if not self._has_role_id(interaction.user, self.captains_role_id):
                await interaction.followup.send("🚫 Only captains can use this command.", ephemeral=True)
                return

            # Category lock
            step = "CATEGORY_CHECK"
            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send("❌ This command must be used in a text channel.", ephemeral=True)
                return

            base_channel = channel.parent if isinstance(channel, discord.Thread) else channel
            if not isinstance(base_channel, discord.TextChannel) or base_channel.category_id != self.transactions_category_id:
                await interaction.followup.send("🚫 This command can only be used in the Transactions category.", ephemeral=True)
                return

            origin_channel_id = base_channel.id

            # Determine end time (upcoming Sunday 11:59pm ET)
            now_et = datetime.now(EASTERN)
            end_dt_et = _next_sunday_2359(now_et)

            # Open sheet + validate captain team + player is Free Agent
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_ALL"
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            # captain row + team
            step = "FIND_CAPTAIN_ROW"
            captain_row_index = self._find_row_index_by_discord_id(values, interaction.user.id)
            if not captain_row_index:
                await interaction.followup.send("❌ You (captain) are not found in the Google Sheet (Column A).", ephemeral=True)
                return

            captain_team = self._get_team_from_row(values, captain_row_index)
            if not captain_team:
                await interaction.followup.send("❌ Your team name is blank in Column D for your row in the Google Sheet.", ephemeral=True)
                return

            # Ensure team role exists in TEAM_INFO
            team_role_id = _get_team_role_id(captain_team)
            if not team_role_id:
                await interaction.followup.send(f"❌ TEAM_INFO is missing a valid role `id` for your team: **{captain_team}**.", ephemeral=True)
                return

            # Player must exist + be Free Agent
            step = "FIND_PLAYER_ROW"
            player_row_index = self._find_row_index_by_discord_id(values, player1.id)
            if not player_row_index:
                await interaction.followup.send(f"❌ `{player1.display_name}` is not found in the Google Sheet (Column A).", ephemeral=True)
                return

            player_team_value = self._get_team_from_row(values, player_row_index)
            step = "VALIDATE_FREE_AGENT"
            if not _is_free_agent(player_team_value):
                await interaction.followup.send(
                    f"🚫 Cannot sub {player1.mention}. They are currently on **{player_team_value or 'Unknown'}**.",
                    ephemeral=True
                )
                return

            # Post pending messages
            step = "POST_PENDING_ORIGIN"
            await base_channel.send('Your transaction is pending "Admin Approval"')

            step = "POST_PENDING_CHANNEL"
            pending_channel = self.bot.get_channel(self.pending_channel_id)
            if not isinstance(pending_channel, discord.TextChannel):
                await interaction.followup.send("❌ PENDING_TRANSACTIONS_CHANNEL_ID does not point to a valid text channel.", ephemeral=True)
                return

            view = Sub.ApprovalView(
                cog=self,
                origin_channel_id=origin_channel_id,
                captain_id=interaction.user.id,
                captain_team=captain_team,
                player_id=player1.id,
                player_display=player1.display_name,
                end_dt_et=end_dt_et
            )

            admins_role_mention = f"<@&{self.admins_role_id}>"
            end_display = end_dt_et.strftime("%a %b %-d, %-I:%M %p ET") if os.name != "nt" else end_dt_et.strftime("%a %b %d, %I:%M %p ET")

            await pending_channel.send(
                content=(
                    f"{admins_role_mention} **Pending Sub Request**\n"
                    f"Captain: {interaction.user.mention}\n"
                    f"Team (from sheet): **{captain_team}**\n"
                    f"Sub In: {player1.mention}\n"
                    f"Expires: **{end_display}**\n"
                    f"Origin channel: <#{origin_channel_id}>"
                ),
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                view=view
            )

            await interaction.followup.send("✅ Request submitted for Admin Approval.", ephemeral=True)

        except Exception as e:
            logger.error("ERROR at step=%s: %r", step, e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /sub failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Sub(bot))
