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

logger = logging.getLogger("qrls.add")
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


class Add(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")

        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.pending_channel_id = _get_env_int("PENDING_TRANSACTIONS_CHANNEL_ID")

        # ✅ public log channel for completed transactions
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

    def _count_team(self, values: list[list[str]], team_name: str) -> int:
        count = 0
        for row in values:
            if len(row) > self.COL_TEAM and _normalize(row[self.COL_TEAM]) == team_name:
                count += 1
        return count

    async def _post_in_origin_channel(self, origin_channel_id: int, message: str):
        ch = self.bot.get_channel(origin_channel_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(message)

    async def _post_transaction_log(self, team_name: str, player_member: Optional[discord.Member], player_display: str):
        """
        Post to TRANSACTIONS_CHANNEL_ID after a fully successful transaction (sheet updated).
        Message format: "@Team adds @player to their roster from Free Agency."
        """
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping transaction log post.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        # Player mention if possible
        player_text = player_member.mention if isinstance(player_member, discord.Member) else player_display

        # Team role mention (prefer TEAM_INFO id, fallback to role name lookup, fallback to plain text)
        team_role = None
        team_role_id = _get_team_role_id(team_name)
        if team_role_id and ch.guild:
            team_role = ch.guild.get_role(team_role_id)

        if not team_role and ch.guild:
            team_role = discord.utils.get(ch.guild.roles, name=team_name)

        team_text = team_role.mention if team_role else f"**{team_name}**"

        await ch.send(
            f"{team_text} adds {player_text} to their roster from Free Agency.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False)
        )


    async def _apply_discord_roles_after_approval(
        self,
        guild: discord.Guild,
        player_id: int,
        team_name: str
    ) -> tuple[bool, str]:
        """
        After sheet update: remove Free Agent role, add team role.
        Returns (ok, message).
        """
        try:
            free_agent_role_id = _get_team_role_id("Free Agent")
            team_role_id = _get_team_role_id(team_name)

            if not free_agent_role_id:
                return False, "Free Agent role ID is missing/invalid in TEAM_INFO."
            if not team_role_id:
                return False, f"Team role ID is missing/invalid in TEAM_INFO for team `{team_name}`."

            free_agent_role = guild.get_role(free_agent_role_id)
            team_role = guild.get_role(team_role_id)

            if not free_agent_role:
                return False, f"Free Agent role (id={free_agent_role_id}) not found in server."
            if not team_role:
                return False, f"Team role for `{team_name}` (id={team_role_id}) not found in server."

            member = guild.get_member(player_id)
            if member is None:
                # Try fetching if not cached
                member = await guild.fetch_member(player_id)

            logger.info(
                "Role update: member=%s free_agent_role=%s team_role=%s",
                member.id,
                free_agent_role.id,
                team_role.id
            )

            # Role operations (requires Manage Roles + role hierarchy)
            to_remove = [free_agent_role] if free_agent_role in member.roles else []
            to_add = [team_role] if team_role not in member.roles else []

            if not to_remove and not to_add:
                return True, f"No role changes needed for {member.mention}."

            if to_remove:
                await member.remove_roles(*to_remove, reason=f"/add approved: remove Free Agent, add {team_name}")
            if to_add:
                await member.add_roles(*to_add, reason=f"/add approved: add team role {team_name}")

            return True, f"Updated roles for {member.mention}: removed Free Agent, added {team_role.mention}."

        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (or role hierarchy prevents it)."
        except discord.NotFound:
            return False, "Player not found in the server when attempting role update."
        except Exception as e:
            logger.error("Role update failed: %r", e)
            traceback.print_exc()
            return False, "Unexpected error while updating roles (see console)."

    # ---------------------------
    # Approval View
    # ---------------------------
    class ApprovalView(discord.ui.View):
        def __init__(
            self,
            cog: "Add",
            origin_channel_id: int,
            captain_id: int,
            captain_team: str,
            player_id: int,
            player_display: str,
        ):
            super().__init__(timeout=60 * 60 * 24)  # 24 hour timeout
            self.cog = cog
            self.origin_channel_id = origin_channel_id
            self.captain_id = captain_id
            self.captain_team = captain_team
            self.player_id = player_id
            self.player_display = player_display
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
                # For component interactions, always respond quickly
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

            # ✅ ACK immediately to avoid "Unknown interaction"
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                # Re-open sheet and re-validate (state could have changed since request)
                ws = self.cog._open_worksheet()
                values = ws.get_all_values()

                captain_row = self.cog._find_row_index_by_discord_id(values, self.captain_id)
                if not captain_row:
                    try:
                        await interaction.followup.send("❌ Captain not found in sheet anymore.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        "❌ Transaction could not be approved (captain not found in sheet)."
                    )
                    await self._finalize_buttons(interaction, "❌ Transaction failed (captain not found in sheet).")
                    return

                # Captain team could have changed—use current value from the sheet
                captain_team_current = self.cog._get_team_from_row(values, captain_row)
                if not captain_team_current:
                    try:
                        await interaction.followup.send("❌ Captain team is blank in sheet.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        "❌ Transaction could not be approved (captain team blank in sheet)."
                    )
                    await self._finalize_buttons(interaction, "❌ Transaction failed (captain team blank).")
                    return

                player_row = self.cog._find_row_index_by_discord_id(values, self.player_id)
                if not player_row:
                    try:
                        await interaction.followup.send("❌ Player not found in sheet anymore.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        "❌ Transaction could not be approved (player not found in sheet)."
                    )
                    await self._finalize_buttons(interaction, "❌ Transaction failed (player not found in sheet).")
                    return

                player_team_current = self.cog._get_team_from_row(values, player_row)
                if not _is_free_agent(player_team_current):
                    try:
                        await interaction.followup.send(
                            f"❌ Cannot approve: player is no longer a Free Agent (currently: {player_team_current}).",
                            ephemeral=True
                        )
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"❌ Transaction approval failed: player is no longer a Free Agent (currently **{player_team_current}**)."
                    )
                    await self._finalize_buttons(interaction, "❌ Approval failed (player not Free Agent).")
                    return

                roster_count = self.cog._count_team(values, captain_team_current)
                if roster_count >= 4:
                    try:
                        await interaction.followup.send(
                            f"❌ Cannot approve: {captain_team_current} roster is full ({roster_count}/4).",
                            ephemeral=True
                        )
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"❌ Transaction approval failed: **{captain_team_current}** roster is full ({roster_count}/4)."
                    )
                    await self._finalize_buttons(interaction, "❌ Approval failed (roster full).")
                    return

                # ✅ Apply change: Column D (4th col) to captain team
                ws.update_cell(player_row, self.cog.COL_TEAM + 1, captain_team_current)

                # ✅ After sheet update: update Discord roles (remove Free Agent, add team role)
                role_ok, role_msg = await self.cog._apply_discord_roles_after_approval(
                    guild=interaction.guild,
                    player_id=self.player_id,
                    team_name=captain_team_current
                )

                # ✅ Post to TRANSACTIONS_CHANNEL_ID after sheet update (success criteria = sheet updated)
                player_member = None
                try:
                    player_member = interaction.guild.get_member(self.player_id) or await interaction.guild.fetch_member(self.player_id)
                except (discord.NotFound, discord.Forbidden):
                    player_member = None

                try:
                    await self.cog._post_transaction_log(
                        team_name=captain_team_current,
                        player_member=player_member,
                        player_display=self.player_display
                    )
                except Exception as e:
                    logger.error("Transaction log post failed: %r", e)
                    traceback.print_exc()

                # Admin confirmation (followup, not response)
                try:
                    await interaction.followup.send("✅ Approved and applied.", ephemeral=True)
                except discord.HTTPException:
                    pass

                # Notify origin channel of approval + role update status
                if role_ok:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Transaction approved by {approver.mention}. **{self.player_display}** has been added to **{captain_team_current}**.\n"
                        f"🔧 {role_msg}"
                    )
                    await self._finalize_buttons(
                        interaction,
                        f"✅ Approved by {approver.mention} — **{self.player_display}** → **{captain_team_current}**"
                    )
                else:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Transaction approved by {approver.mention}. **{self.player_display}** has been added to **{captain_team_current}**.\n"
                        f"⚠️ Role update issue: {role_msg}"
                    )
                    await self._finalize_buttons(
                        interaction,
                        f"✅ Approved by {approver.mention} — **{self.player_display}** → **{captain_team_current}** (⚠️ role update issue)"
                    )

            except Exception as e:
                logger.error("Approve failed: %r", e)
                traceback.print_exc()

                try:
                    await interaction.followup.send(
                        "❌ An error occurred while approving. Check bot console.",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    pass

                await self.cog._post_in_origin_channel(
                    self.origin_channel_id,
                    "❌ Transaction approval failed due to an internal error. (Admin notified in console.)"
                )
                await self._finalize_buttons(interaction, "❌ Approval failed due to an internal error.")

        @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
        async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            approver = interaction.user

            # ✅ ACK immediately to avoid "Unknown interaction"
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
                await interaction.followup.send("🚫 Rejected.", ephemeral=True)
            except discord.HTTPException:
                pass

            await self.cog._post_in_origin_channel(
                self.origin_channel_id,
                f"🚫 Transaction rejected by {approver.mention}."
            )
            await self._finalize_buttons(interaction, f"🚫 Rejected by {approver.mention}")

        async def on_timeout(self):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            # No message edit here (we don't have a safe message ref to update on timeout).

    # ---------------------------
    # /add command
    # ---------------------------
    @app_commands.command(
        name="add",
        description="Request to add a Free Agent to your team (requires Admin Approval)."
    )
    @app_commands.guild_only()
    async def add(self, interaction: Interaction, player1: discord.Member):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # --- Env validation ---
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

            # --- Captain-only restriction ---
            step = "CAPTAIN_CHECK"
            if not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return
            if not self._has_role_id(interaction.user, self.captains_role_id):
                await interaction.followup.send("🚫 Only captains can use this command.", ephemeral=True)
                return

            # --- Category lock restriction ---
            step = "CATEGORY_CHECK"
            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send("❌ This command must be used in a text channel.", ephemeral=True)
                return

            base_channel = channel.parent if isinstance(channel, discord.Thread) else channel
            if not isinstance(base_channel, discord.TextChannel) or base_channel.category_id != self.transactions_category_id:
                await interaction.followup.send(
                    "🚫 This command can only be used in the Transactions category.",
                    ephemeral=True
                )
                return

            origin_channel_id = base_channel.id

            # --- Open worksheet and validate everything BEFORE creating pending request ---
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_ALL"
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            # Find captain row + team
            step = "FIND_CAPTAIN_ROW"
            captain_row_index = self._find_row_index_by_discord_id(values, interaction.user.id)
            if not captain_row_index:
                await interaction.followup.send(
                    "❌ You (captain) are not found in the Google Sheet (Column A).",
                    ephemeral=True
                )
                return

            captain_team = self._get_team_from_row(values, captain_row_index)
            if not captain_team:
                await interaction.followup.send(
                    "❌ Your team name is blank in Column D for your row in the Google Sheet.",
                    ephemeral=True
                )
                return

            # Also enforce TEAM_INFO has role IDs for both "Free Agent" and captain_team (since we must update roles)
            free_agent_role_id = _get_team_role_id("Free Agent")
            team_role_id = _get_team_role_id(captain_team)
            if not free_agent_role_id:
                await interaction.followup.send(
                    "❌ TEAM_INFO is missing a valid role `id` for **Free Agent**.",
                    ephemeral=True
                )
                return
            if not team_role_id:
                await interaction.followup.send(
                    f"❌ TEAM_INFO is missing a valid role `id` for your team: **{captain_team}**.",
                    ephemeral=True
                )
                return

            # Find player row
            step = "FIND_PLAYER_ROW"
            player_row_index = self._find_row_index_by_discord_id(values, player1.id)
            if not player_row_index:
                await interaction.followup.send(
                    f"❌ `{player1.display_name}` is not found in the Google Sheet (Column A).",
                    ephemeral=True
                )
                return

            player_team_value = self._get_team_from_row(values, player_row_index)

            # Validate Free Agent
            step = "VALIDATE_FREE_AGENT"
            if not _is_free_agent(player_team_value):
                await interaction.followup.send(
                    f"🚫 Cannot add {player1.mention}. They are currently on **{player_team_value or 'Unknown'}**.",
                    ephemeral=True
                )
                return

            # Count roster
            step = "COUNT_ROSTER"
            roster_count = self._count_team(values, captain_team)
            if roster_count >= 4:
                await interaction.followup.send(
                    f"🚫 Your roster is full (**{captain_team}** already has {roster_count}/4 players).",
                    ephemeral=True
                )
                return

            # ---- Passed checks: post pending messages ----
            step = "POST_PENDING_ORIGIN"
            await base_channel.send('Your transaction is pending "Admin Approval"')

            step = "POST_PENDING_CHANNEL"
            pending_channel = self.bot.get_channel(self.pending_channel_id)
            if not isinstance(pending_channel, discord.TextChannel):
                await interaction.followup.send(
                    "❌ PENDING_TRANSACTIONS_CHANNEL_ID does not point to a valid text channel.",
                    ephemeral=True
                )
                return

            view = Add.ApprovalView(
                cog=self,
                origin_channel_id=origin_channel_id,
                captain_id=interaction.user.id,
                captain_team=captain_team,
                player_id=player1.id,
                player_display=player1.display_name,
            )

            admins_role_mention = f"<@&{self.admins_role_id}>"

            await pending_channel.send(
                content=(
                    f"{admins_role_mention} **Pending Add Request**\n"
                    f"Captain: {interaction.user.mention}\n"
                    f"Team (from sheet): **{captain_team}**\n"
                    f"Add: {player1.mention}\n"
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
                    f"❌ /add failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Add(bot))
