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

logger = logging.getLogger("qrls.trade")
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


def _is_true(value: str) -> bool:
    return _normalize(value).lower() == "true"


def _is_free_agent(team_name: str) -> bool:
    return _normalize(team_name).lower() == "free agent"


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


class Trade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.pending_channel_id = _get_env_int("PENDING_TRANSACTIONS_CHANNEL_ID")
        self.transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")

        # ✅ public log channel for completed transactions
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

    def _is_captain_member(self, member: discord.Member) -> bool:
        if self.captains_role_id and self._has_role_id(member, self.captains_role_id):
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

    def _get_team_from_row(self, values: list[list[str]], row_index_1based: int) -> str:
        row = values[row_index_1based - 1]
        return _normalize(row[self.COL_TEAM]) if len(row) > self.COL_TEAM else ""

    def _get_captain_flag_from_row(self, values: list[list[str]], row_index_1based: int) -> bool:
        row = values[row_index_1based - 1]
        v = _normalize(row[self.COL_CAPTAIN]) if len(row) > self.COL_CAPTAIN else ""
        return _is_true(v)

    def _is_captain_in_sheet(self, values: list[list[str]], discord_id: int) -> bool:
        row = self._find_row_index_by_discord_id(values, discord_id)
        if not row:
            return False
        return self._get_captain_flag_from_row(values, row)

    def _find_team_captain_id(self, values: list[list[str]], team_name: str) -> Optional[int]:
        """
        Returns the first Discord ID found where Team==team_name and Captain==True.
        """
        for row in values:
            if len(row) <= max(self.COL_CAPTAIN, self.COL_TEAM, self.COL_DISCORD_ID):
                continue
            if _normalize(row[self.COL_TEAM]) == team_name and _is_true(row[self.COL_CAPTAIN]):
                did = _normalize(row[self.COL_DISCORD_ID])
                if did.isdigit():
                    return int(did)
        return None

    async def _apply_role_swap(
        self,
        guild: discord.Guild,
        member_id: int,
        old_team: str,
        new_team: str,
        reason: str
    ) -> tuple[bool, str]:
        """
        Remove old team role, add new team role.
        """
        try:
            old_role_id = _get_team_role_id(old_team)
            new_role_id = _get_team_role_id(new_team)

            if not old_role_id:
                return False, f"TEAM_INFO missing/invalid role id for old team `{old_team}`."
            if not new_role_id:
                return False, f"TEAM_INFO missing/invalid role id for new team `{new_team}`."

            old_role = guild.get_role(old_role_id)
            new_role = guild.get_role(new_role_id)
            if not old_role:
                return False, f"Old team role not found in server (id={old_role_id})."
            if not new_role:
                return False, f"New team role not found in server (id={new_role_id})."

            member = guild.get_member(member_id) or await guild.fetch_member(member_id)

            to_remove = [old_role] if old_role in member.roles else []
            to_add = [new_role] if new_role not in member.roles else []

            if to_remove:
                await member.remove_roles(*to_remove, reason=reason)
            if to_add:
                await member.add_roles(*to_add, reason=reason)

            return True, f"Updated roles for {member.mention}: removed `{old_team}`, added `{new_team}`."
        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except discord.NotFound:
            return False, "Member not found in server."
        except Exception as e:
            logger.error("Role swap failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while updating roles (see console)."

    async def _grant_channel_access(
        self,
        channel: discord.TextChannel,
        member: discord.Member
    ) -> tuple[bool, str]:
        """
        Add channel overwrite so member can view/read/send/embed.
        """
        try:
            overwrite = channel.overwrites_for(member)
            overwrite.view_channel = True
            overwrite.read_message_history = True
            overwrite.send_messages = True
            overwrite.embed_links = True

            await channel.set_permissions(member, overwrite=overwrite, reason="Trade approval access")
            return True, f"Granted channel access to {member.mention}."
        except discord.Forbidden:
            return False, "Bot lacks permission to edit channel permissions."
        except Exception as e:
            logger.error("Grant channel access failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while updating channel permissions."

    async def _post_trade_log(
        self,
        guild: discord.Guild,
        team1_name: str,
        team2_name: str,
        player1_id: int,
        player2_id: int
    ):
        """
        Post to TRANSACTIONS_CHANNEL_ID after a fully successful trade (sheet updated).
        Message format: "@Team1 trades @player1 to @Team2 for @player2"
        """
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping trade log post.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        role1 = discord.utils.get(guild.roles, name=team1_name)
        role2 = discord.utils.get(guild.roles, name=team2_name)

        team1_mention = role1.mention if role1 else f"@{team1_name}"
        team2_mention = role2.mention if role2 else f"@{team2_name}"

        player1_mention = f"<@{player1_id}>"
        player2_mention = f"<@{player2_id}>"

        await ch.send(
            f"{team1_mention} trades {player1_mention} to {team2_mention} for {player2_mention}",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False)
        )

    # ---------------------------
    # Captain Approval View
    # ---------------------------
    class CaptainApprovalView(discord.ui.View):
        def __init__(
            self,
            cog: "Trade",
            origin_channel_id: int,
            requestor_id: int,
            player1_id: int,
            player2_id: int,
            team1: str,
            team2: str,
            opposing_captain_id: int,
        ):
            super().__init__(timeout=60 * 60 * 24)  # 24 hour
            self.cog = cog
            self.origin_channel_id = origin_channel_id
            self.requestor_id = requestor_id
            self.player1_id = player1_id
            self.player2_id = player2_id
            self.team1 = team1
            self.team2 = team2
            self.opposing_captain_id = opposing_captain_id
            self.decided = False

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                try:
                    await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if self.decided:
                try:
                    await interaction.response.send_message("ℹ️ This trade has already been decided.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if interaction.user.id != self.opposing_captain_id:
                try:
                    await interaction.response.send_message(
                        "🚫 Only the opposing captain can approve/decline this trade.",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    pass
                return False

            return True

        async def _disable_buttons(self, interaction: discord.Interaction, status_text: str):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            try:
                await interaction.message.edit(content=status_text, view=self)
            except discord.HTTPException:
                pass

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
        async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                pending_channel_id = self.cog.pending_channel_id
                admins_role_id = self.cog.admins_role_id
                if not pending_channel_id or not admins_role_id:
                    await interaction.followup.send("❌ Pending channel/admin role not configured in .env.", ephemeral=True)
                    await self._disable_buttons(interaction, "❌ Trade failed (missing config).")
                    return

                pending_channel = self.cog.bot.get_channel(pending_channel_id)
                if not isinstance(pending_channel, discord.TextChannel):
                    await interaction.followup.send("❌ Pending channel ID does not resolve to a text channel.", ephemeral=True)
                    await self._disable_buttons(interaction, "❌ Trade failed (invalid pending channel).")
                    return

                admins_mention = f"<@&{admins_role_id}>"
                origin_channel_mention = f"<#{self.origin_channel_id}>"

                view = Trade.AdminApprovalView(
                    cog=self.cog,
                    origin_channel_id=self.origin_channel_id,
                    requestor_id=self.requestor_id,
                    player1_id=self.player1_id,
                    player2_id=self.player2_id,
                    expected_team1=self.team1,
                    expected_team2=self.team2,
                )

                await pending_channel.send(
                    content=(
                        f"{admins_mention} **Pending Trade Request**\n"
                        f"Origin channel: {origin_channel_mention}\n"
                        f"Requested by: <@{self.requestor_id}>\n"
                        f"Trade: <@{self.player1_id}> (**{self.team1}**) ↔ <@{self.player2_id}> (**{self.team2}**)\n"
                        f"Opposing captain approved: {interaction.user.mention}"
                    ),
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                    view=view
                )

                await interaction.followup.send("✅ Approved. Sent to admins for final approval.", ephemeral=True)
                await self._disable_buttons(interaction, f"✅ Approved by {interaction.user.mention} — pending Admin approval.")

            except Exception as e:
                logger.error("Captain approve failed: %r", e)
                traceback.print_exc()
                try:
                    await interaction.followup.send("❌ An error occurred. Check bot console.", ephemeral=True)
                except discord.HTTPException:
                    pass
                await self._disable_buttons(interaction, "❌ Trade failed due to an internal error.")

        @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                await interaction.followup.send("🚫 Declined.", ephemeral=True)
                await self._disable_buttons(interaction, f"🚫 Declined by {interaction.user.mention}.")
            except Exception:
                pass

    # ---------------------------
    # Admin Approval View (matches add.py styling)
    # ---------------------------
    class AdminApprovalView(discord.ui.View):
        def __init__(
            self,
            cog: "Trade",
            origin_channel_id: int,
            requestor_id: int,
            player1_id: int,
            player2_id: int,
            expected_team1: str,
            expected_team2: str
        ):
            super().__init__(timeout=60 * 60 * 24)  # 24 hour
            self.cog = cog
            self.origin_channel_id = origin_channel_id
            self.requestor_id = requestor_id
            self.player1_id = player1_id
            self.player2_id = player2_id
            self.expected_team1 = expected_team1
            self.expected_team2 = expected_team2
            self.decided = False

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                try:
                    await interaction.response.send_message("❌ Must be used in a server.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if self.decided:
                try:
                    await interaction.response.send_message("ℹ️ This transaction has already been decided.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if not self.cog._is_admin_member(interaction.user):
                try:
                    await interaction.response.send_message("🚫 Only admins can approve/reject.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            return True

        async def _finalize(self, interaction: discord.Interaction, status_text: str):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            try:
                await interaction.message.edit(content=status_text, view=self)
            except discord.HTTPException:
                pass

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
        async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            step = "START"
            try:
                guild = interaction.guild

                step = "OPEN_SHEET"
                ws = self.cog._open_worksheet()
                values = ws.get_all_values()
                if not values:
                    await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                    await self._finalize(interaction, "❌ Trade failed (empty sheet).")
                    return

                step = "FIND_PLAYER_ROWS"
                p1_row = self.cog._find_row_index_by_discord_id(values, self.player1_id)
                p2_row = self.cog._find_row_index_by_discord_id(values, self.player2_id)
                if not p1_row or not p2_row:
                    await interaction.followup.send("❌ One or both players are not found in the sheet (Column A).", ephemeral=True)
                    await self._finalize(interaction, "❌ Trade failed (player not found).")
                    return

                step = "READ_TEAMS"
                p1_team_current = self.cog._get_team_from_row(values, p1_row)
                p2_team_current = self.cog._get_team_from_row(values, p2_row)

                # Block Free Agent trades (re-check at approval time)
                if _is_free_agent(p1_team_current) or _is_free_agent(p2_team_current):
                    await interaction.followup.send("❌ Trades involving Free Agents are not allowed.", ephemeral=True)
                    await self._finalize(interaction, "❌ Trade failed (Free Agent involved).")
                    return

                # Block if TEAM_INFO missing (re-check at approval time)
                if _get_team_role_id(p1_team_current) is None or _get_team_role_id(p2_team_current) is None:
                    await interaction.followup.send("❌ Trade failed: one or both teams are not configured in TEAM_INFO.", ephemeral=True)
                    await self._finalize(interaction, "❌ Trade failed (TEAM_INFO missing).")
                    return

                # Validate state hasn't changed since request
                if p1_team_current != self.expected_team1 or p2_team_current != self.expected_team2:
                    await interaction.followup.send(
                        "❌ Trade cannot be approved because team state changed.\n"
                        f"Current: player1={p1_team_current}, player2={p2_team_current}\n"
                        f"Expected: player1={self.expected_team1}, player2={self.expected_team2}",
                        ephemeral=True
                    )
                    await self._finalize(interaction, "❌ Trade failed (team state changed).")
                    return

                step = "UPDATE_SHEET"
                # Swap: player1 -> team2, player2 -> team1
                ws.update_cell(p1_row, self.cog.COL_TEAM + 1, self.expected_team2)
                ws.update_cell(p2_row, self.cog.COL_TEAM + 1, self.expected_team1)

                step = "UPDATE_ROLES_P1"
                ok1, msg1 = await self.cog._apply_role_swap(
                    guild=guild,
                    member_id=self.player1_id,
                    old_team=self.expected_team1,
                    new_team=self.expected_team2,
                    reason="/trade approved: swap roles"
                )

                step = "UPDATE_ROLES_P2"
                ok2, msg2 = await self.cog._apply_role_swap(
                    guild=guild,
                    member_id=self.player2_id,
                    old_team=self.expected_team2,
                    new_team=self.expected_team1,
                    reason="/trade approved: swap roles"
                )

                origin_ch = self.cog.bot.get_channel(self.origin_channel_id)
                if isinstance(origin_ch, discord.TextChannel):
                    await origin_ch.send(
                        f"✅ Trade approved by {interaction.user.mention}: <@{self.player1_id}> ↔ <@{self.player2_id}>\n"
                        f"🔧 {msg1}\n"
                        f"🔧 {msg2}",
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                    )

                # ✅ Post to TRANSACTIONS_CHANNEL_ID:
                # "@Team1 trades @player1 to @Team2 for @player2"
                try:
                    await self.cog._post_trade_log(
                        guild=guild,
                        team1_name=self.expected_team1,
                        team2_name=self.expected_team2,
                        player1_id=self.player1_id,
                        player2_id=self.player2_id
                    )
                except Exception as e:
                    logger.error("Trade log post failed: %r", e)
                    traceback.print_exc()

                await interaction.followup.send("✅ Approved and applied.", ephemeral=True)

                suffix = ""
                if not (ok1 and ok2):
                    suffix = " (⚠️ role update issue — see origin channel)"

                await self._finalize(
                    interaction,
                    f"✅ Approved by {interaction.user.mention} — <@{self.player1_id}> ↔ <@{self.player2_id}>{suffix}"
                )

            except Exception as e:
                logger.error("Admin approve failed at step=%s: %r", step, e)
                traceback.print_exc()
                try:
                    await interaction.followup.send(
                        f"❌ /trade approval failed at step: **{step}** (check bot console).",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    pass
                await self._finalize(interaction, "❌ Approval failed due to an internal error.")

        @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
        async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                origin_ch = self.cog.bot.get_channel(self.origin_channel_id)
                if isinstance(origin_ch, discord.TextChannel):
                    await origin_ch.send(
                        f"🚫 Trade rejected by {interaction.user.mention}: <@{self.player1_id}> ↔ <@{self.player2_id}>",
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                    )
                await interaction.followup.send("🚫 Rejected.", ephemeral=True)
            except discord.HTTPException:
                pass

            await self._finalize(interaction, f"🚫 Rejected by {interaction.user.mention}")

    # ---------------------------
    # /trade command
    # ---------------------------
    @app_commands.command(
        name="trade",
        description="Request a player trade (requires opposing captain approval + Admin Approval)."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player1="Your player",
        player2="Player you want"
    )
    async def trade(self, interaction: Interaction, player1: discord.Member, player2: discord.Member):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # ---- Env validation ----
            if not self.captains_role_id:
                await interaction.followup.send("❌ CAPTAINS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.admins_role_id:
                await interaction.followup.send("❌ ADMINS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.pending_channel_id:
                await interaction.followup.send("❌ PENDING_TRANSACTIONS_CHANNEL_ID is missing/invalid in .env", ephemeral=True)
                return

            # ---- Must be used in a server + by a captain (role restriction) ----
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return

            if not self._is_captain_member(interaction.user):
                await interaction.followup.send("🚫 Only captains can use this command.", ephemeral=True)
                return

            # ---- Category lock (optional but consistent with add.py) ----
            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send("❌ This command must be used in a text channel.", ephemeral=True)
                return

            base_channel = channel.parent if isinstance(channel, discord.Thread) else channel
            if self.transactions_category_id:
                if not isinstance(base_channel, discord.TextChannel) or base_channel.category_id != self.transactions_category_id:
                    await interaction.followup.send(
                        "🚫 This command can only be used in the Transactions category.",
                        ephemeral=True
                    )
                    return

            origin_channel_id = base_channel.id

            # ---- Validate inputs ----
            if player1.id == player2.id:
                await interaction.followup.send("🚫 You cannot trade a player for themselves.", ephemeral=True)
                return

            # ---- Read from sheet ----
            step = "OPEN_SHEET"
            ws = self._open_worksheet()
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            # ---- Sheet-based captain enforcement (extra safety) ----
            step = "SHEET_CAPTAIN_CHECK"
            if not self._is_captain_in_sheet(values, interaction.user.id):
                await interaction.followup.send(
                    "🚫 You must be marked as a captain in the Google Sheet (Column E = TRUE) to use /trade.",
                    ephemeral=True
                )
                return

            # ---- Find rows ----
            step = "FIND_ROWS"
            requester_row = self._find_row_index_by_discord_id(values, interaction.user.id)
            p1_row = self._find_row_index_by_discord_id(values, player1.id)
            p2_row = self._find_row_index_by_discord_id(values, player2.id)

            if not requester_row:
                await interaction.followup.send("❌ You are not found in the Google Sheet (Column A).", ephemeral=True)
                return
            if not p1_row:
                await interaction.followup.send("❌ player1 is not found in the Google Sheet (Column A).", ephemeral=True)
                return
            if not p2_row:
                await interaction.followup.send("❌ player2 is not found in the Google Sheet (Column A).", ephemeral=True)
                return

            # ---- Read teams ----
            step = "READ_TEAMS"
            requester_team = self._get_team_from_row(values, requester_row)
            team1 = self._get_team_from_row(values, p1_row)  # player1 team
            team2 = self._get_team_from_row(values, p2_row)  # player2 team

            if not requester_team:
                await interaction.followup.send("❌ Your team is blank in Column D in the sheet.", ephemeral=True)
                return
            if not team1:
                await interaction.followup.send("❌ player1 team is blank in Column D in the sheet.", ephemeral=True)
                return
            if not team2:
                await interaction.followup.send("❌ player2 team is blank in Column D in the sheet.", ephemeral=True)
                return

            # Block Free Agent trades
            if _is_free_agent(team1) or _is_free_agent(team2) or _is_free_agent(requester_team):
                await interaction.followup.send("🚫 Trades involving **Free Agents** are not allowed.", ephemeral=True)
                return

            # Block if either team is not configured in TEAM_INFO
            if TEAM_INFO.get(team1) is None or TEAM_INFO.get(team2) is None:
                await interaction.followup.send(
                    "🚫 Trade blocked: one or both teams are not configured in `utils/team_info.py`.",
                    ephemeral=True
                )
                return
            if _get_team_role_id(team1) is None or _get_team_role_id(team2) is None:
                await interaction.followup.send(
                    "🚫 Trade blocked: one or both teams are missing a valid role `id` in `utils/team_info.py`.",
                    ephemeral=True
                )
                return

            # player1 must be on requester's team
            if team1 != requester_team:
                await interaction.followup.send(
                    f"🚫 You can only trade players from **your team**.\n"
                    f"Your team (sheet): **{requester_team}**\n"
                    f"player1 team (sheet): **{team1}**",
                    ephemeral=True
                )
                return

            if team1 == team2:
                await interaction.followup.send("🚫 Both players are already on the same team.", ephemeral=True)
                return

            # ---- Find opposing captain (captain of team2) ----
            step = "FIND_OPPOSING_CAPTAIN"
            opposing_captain_id = self._find_team_captain_id(values, team2)
            if not opposing_captain_id:
                await interaction.followup.send(
                    f"❌ Could not find a captain for **{team2}** in the sheet (Column E=True).",
                    ephemeral=True
                )
                return

            opposing_captain = interaction.guild.get_member(opposing_captain_id)
            if opposing_captain is None:
                try:
                    opposing_captain = await interaction.guild.fetch_member(opposing_captain_id)
                except (discord.NotFound, discord.Forbidden):
                    opposing_captain = None

            if not opposing_captain:
                await interaction.followup.send("❌ Opposing captain is not in the server (or cannot be fetched).", ephemeral=True)
                return

            # ---- Grant opposing captain access to the channel ----
            step = "GRANT_CHANNEL_ACCESS"
            ok_perm, perm_msg = await self._grant_channel_access(base_channel, opposing_captain)
            if not ok_perm:
                await interaction.followup.send(f"❌ Failed to add opposing captain to channel: {perm_msg}", ephemeral=True)
                return

            # ---- Post captain approval embed + buttons (visible to everyone) ----
            step = "POST_CAPTAIN_APPROVAL"
            allowed_mentions = discord.AllowedMentions(roles=True, users=True, everyone=False)

            captains_role = interaction.guild.get_role(self.captains_role_id) if self.captains_role_id else None
            if captains_role:
                await base_channel.send(
                    content=f"{captains_role.mention} — A trade has been proposed and needs opposing captain approval.",
                    allowed_mentions=allowed_mentions
                )
            else:
                await base_channel.send("@Captains — A trade has been proposed and needs opposing captain approval.")

            embed = discord.Embed(
                title="🔁 Trade Proposed",
                description=(
                    f"Requested by: {interaction.user.mention}\n\n"
                    f"Trade request:\n"
                    f"**{player1.mention}** (from **{team1}**) ↔ **{player2.mention}** (from **{team2}**)\n\n"
                    f"Opposing captain to approve: {opposing_captain.mention}"
                ),
                color=discord.Color.orange()
            )
            embed.set_footer(text="Only the opposing captain can approve/decline. Admin approval is required after that.")

            view = Trade.CaptainApprovalView(
                cog=self,
                origin_channel_id=origin_channel_id,
                requestor_id=interaction.user.id,
                player1_id=player1.id,
                player2_id=player2.id,
                team1=team1,
                team2=team2,
                opposing_captain_id=opposing_captain.id
            )

            await base_channel.send(embed=embed, view=view, allowed_mentions=allowed_mentions)

            await interaction.followup.send(
                f"✅ Trade request created.\n{perm_msg}\nWaiting on {opposing_captain.mention} to approve/decline.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
            )

        except Exception as e:
            logger.error("ERROR at step=%s: %r", step, e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /trade failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Trade(bot))
