import os
import json
import logging
import traceback
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from utils.team_info import TEAM_INFO


load_dotenv()

logger = logging.getLogger("qrls.drop")
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
    info = TEAM_INFO.get(team_name)
    if not isinstance(info, dict):
        return None
    role_id = info.get("id")
    if isinstance(role_id, int):
        return role_id
    if isinstance(role_id, str) and role_id.isdigit():
        return int(role_id)
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


class Drop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")

        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.pending_channel_id = _get_env_int("PENDING_TRANSACTIONS_CHANNEL_ID")

        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        # Waivers role is not a team role, so keep it in .env
        self.waivers_role_id = _get_env_int("WAIVERS_ROLE_ID")

        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        self.worksheet_name = os.getenv("GOOGLE_WORKSHEET", "")

        # Sheet columns: A=Discord ID, D=Team
        self.COL_DISCORD_ID = 0
        self.COL_TEAM = 3

        os.makedirs(DATA_DIR, exist_ok=True)

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

    async def _post_in_origin_channel(self, origin_channel_id: int, message: str):
        ch = self.bot.get_channel(origin_channel_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(message)

    async def _post_transaction_log(
        self,
        team_name: str,
        player_member: Optional[discord.Member],
        player_display: str
    ):
        """
        Post to TRANSACTIONS_CHANNEL_ID after a fully successful transaction (sheet updated).
        Message format:
        "@TeamRole drops @player to 2 Day Waivers."
        """
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping transaction log post.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        team_role_id = _get_team_role_id(team_name)
        if team_role_id:
            team_text = f"<@&{team_role_id}>"
        else:
            logger.warning("No role ID found for team '%s'; falling back to text.", team_name)
            team_text = f"**{team_name}**"

        player_text = (
            player_member.mention
            if isinstance(player_member, discord.Member)
            else player_display
        )

        await ch.send(f"{team_text} drops {player_text} to **2 Day Waivers**.")

    def _load_waivers_json(self) -> Dict[str, Any]:
        try:
            if not os.path.exists(WAIVERS_FILE):
                return {}
            with open(WAIVERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error("Failed to read %s: %r", WAIVERS_FILE, e)
            return {}

    def _save_waivers_json(self, data: Dict[str, Any]) -> None:
        try:
            tmp = WAIVERS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, WAIVERS_FILE)
        except Exception as e:
            logger.error("Failed to write %s: %r", WAIVERS_FILE, e)

    def _record_waiver(
        self,
        guild_id: int,
        player_id: int,
        requested_at_iso: str,
        expires_at_iso: str,
        original_team: str,
        dropped_by_id: int,
    ) -> None:
        """
        Create/overwrite the waiver record for this player.
        Note: waiverclaim.py will own expiry + claim logic, so we only store timing + origin team.
        """
        data = self._load_waivers_json()
        key = str(player_id)
        data[key] = {
            "guild_id": guild_id,
            "player_id": player_id,
            "requested_at": requested_at_iso,
            "expires_at": expires_at_iso,
            "original_team": original_team,
            "dropped_by_id": dropped_by_id,

            # waiverclaim.py uses "claim" object instead of these legacy fields,
            # but we leave them out to avoid conflicts.
            # "claim": {...} will be added by waiverclaim.py
        }
        self._save_waivers_json(data)

    async def _apply_discord_roles_after_approval(
        self,
        guild: discord.Guild,
        player_id: int,
        team_name: str
    ) -> tuple[bool, str]:
        """
        After sheet update:
        - remove TEAM role
        - add Free Agent role
        - add Waivers role
        Returns (ok, message).
        """
        try:
            free_agent_role_id = _get_team_role_id("Free Agent")
            team_role_id = _get_team_role_id(team_name)

            if not free_agent_role_id:
                return False, "Free Agent role ID is missing/invalid in TEAM_INFO."
            if not team_role_id:
                return False, f"Team role ID is missing/invalid in TEAM_INFO for team `{team_name}`."
            if not self.waivers_role_id:
                return False, "WAIVERS_ROLE_ID is missing/invalid in .env."

            free_agent_role = guild.get_role(free_agent_role_id)
            team_role = guild.get_role(team_role_id)
            waivers_role = guild.get_role(self.waivers_role_id)

            if not free_agent_role:
                return False, f"Free Agent role (id={free_agent_role_id}) not found in server."
            if not team_role:
                return False, f"Team role for `{team_name}` (id={team_role_id}) not found in server."
            if not waivers_role:
                return False, f"Waivers role (id={self.waivers_role_id}) not found in server."

            member = guild.get_member(player_id)
            if member is None:
                member = await guild.fetch_member(player_id)

            logger.info(
                "Role update (drop->waivers): member=%s remove_team_role=%s add_free_agent_role=%s add_waivers_role=%s",
                member.id,
                team_role.id,
                free_agent_role.id,
                waivers_role.id
            )

            to_remove = [team_role] if team_role in member.roles else []
            to_add = []
            if free_agent_role not in member.roles:
                to_add.append(free_agent_role)
            if waivers_role not in member.roles:
                to_add.append(waivers_role)

            if not to_remove and not to_add:
                return True, f"No role changes needed for {member.mention}."

            if to_remove:
                await member.remove_roles(
                    *to_remove,
                    reason=f"/drop approved: move {team_name} -> Waivers (add Free Agent + Waivers)"
                )
            if to_add:
                await member.add_roles(
                    *to_add,
                    reason=f"/drop approved: add Free Agent + Waivers"
                )

            added_mentions = ", ".join(r.mention for r in to_add) if to_add else "none"
            removed_mentions = ", ".join(r.mention for r in to_remove) if to_remove else "none"
            return True, f"Updated roles for {member.mention}: removed {removed_mentions}, added {added_mentions}."

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
            cog: "Drop",
            origin_channel_id: int,
            captain_id: int,
            captain_team: str,
            player_id: int,
            player_display: str,
            requested_at_iso: str,
        ):
            super().__init__(timeout=60 * 60)  # 1 hour timeout
            self.cog = cog
            self.origin_channel_id = origin_channel_id
            self.captain_id = captain_id
            self.captain_team = captain_team
            self.player_id = player_id
            self.player_display = player_display
            self.requested_at_iso = requested_at_iso
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

            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            try:
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

                if _normalize(player_team_current) != _normalize(captain_team_current):
                    try:
                        await interaction.followup.send(
                            f"❌ Cannot approve: player is not on the captain's team (currently: {player_team_current}).",
                            ephemeral=True
                        )
                    except discord.HTTPException:
                        pass
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"❌ Transaction approval failed: player is currently on **{player_team_current or 'Unknown'}**, not **{captain_team_current}**."
                    )
                    await self._finalize_buttons(interaction, "❌ Approval failed (player not on captain team).")
                    return

                requested_at = _parse_iso_dt(self.requested_at_iso) or _utc_now()
                expires_at = requested_at + timedelta(days=2)

                # Sheet: set to Waivers
                ws.update_cell(player_row, self.cog.COL_TEAM + 1, "Waivers")

                # Roles: remove team role, add Free Agent + Waivers
                role_ok, role_msg = await self.cog._apply_discord_roles_after_approval(
                    guild=interaction.guild,
                    player_id=self.player_id,
                    team_name=captain_team_current
                )

                # Record waiver timing in JSON
                try:
                    self.cog._record_waiver(
                        guild_id=interaction.guild.id,
                        player_id=self.player_id,
                        requested_at_iso=requested_at.isoformat(),
                        expires_at_iso=expires_at.isoformat(),
                        original_team=captain_team_current,
                        dropped_by_id=self.captain_id,
                    )
                except Exception as e:
                    logger.error("Failed to record waiver json: %r", e)
                    traceback.print_exc()

                # Transaction log (drop)
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

                try:
                    await interaction.followup.send("✅ Approved and applied.", ephemeral=True)
                except discord.HTTPException:
                    pass

                expiry_text = f"<t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)"

                if role_ok:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Transaction approved by {approver.mention}. **{self.player_display}** has been dropped to **2 Day Waivers**.\n"
                        f"🗓️ Waivers end: {expiry_text}\n"
                        f"🔧 {role_msg}"
                    )
                    await self._finalize_buttons(
                        interaction,
                        f"✅ Approved by {approver.mention} — **{self.player_display}** → **2 Day Waivers** (ends {expiry_text})"
                    )
                else:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Transaction approved by {approver.mention}. **{self.player_display}** has been dropped to **2 Day Waivers**.\n"
                        f"🗓️ Waivers end: {expiry_text}\n"
                        f"⚠️ Role update issue: {role_msg}"
                    )
                    await self._finalize_buttons(
                        interaction,
                        f"✅ Approved by {approver.mention} — **{self.player_display}** → **2 Day Waivers** (ends {expiry_text}, ⚠️ role issue)"
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

    # ---------------------------
    # /drop command
    # ---------------------------
    @app_commands.command(
        name="drop",
        description="Request to drop a player to 2 Day Waivers (requires Admin Approval)."
    )
    @app_commands.guild_only()
    async def drop(self, interaction: Interaction, player1: discord.Member):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            requested_at_iso = _utc_now().isoformat()

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
            if not self.waivers_role_id:
                await interaction.followup.send("❌ WAIVERS_ROLE_ID is missing/invalid in .env", ephemeral=True)
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

            # --- Open worksheet and validate BEFORE creating pending request ---
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_ALL"
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            # Captain row + team
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

            # Ensure TEAM_INFO has role IDs for Free Agent + captain team
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

            # Player row + must match captain team
            step = "FIND_PLAYER_ROW"
            player_row_index = self._find_row_index_by_discord_id(values, player1.id)
            if not player_row_index:
                await interaction.followup.send(
                    f"❌ `{player1.display_name}` is not found in the Google Sheet (Column A).",
                    ephemeral=True
                )
                return

            player_team_value = self._get_team_from_row(values, player_row_index)

            step = "VALIDATE_OWN_ROSTER"
            if _normalize(player_team_value) != _normalize(captain_team):
                await interaction.followup.send(
                    f"🚫 You can only drop players from your own team. `{player1.display_name}` is currently on **{player_team_value or 'Unknown'}**.",
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

            view = Drop.ApprovalView(
                cog=self,
                origin_channel_id=origin_channel_id,
                captain_id=interaction.user.id,
                captain_team=captain_team,
                player_id=player1.id,
                player_display=player1.display_name,
                requested_at_iso=requested_at_iso,
            )

            admins_role_mention = f"<@&{self.admins_role_id}>"

            await pending_channel.send(
                content=(
                    f"{admins_role_mention} **Pending Drop Request**\n"
                    f"Captain: {interaction.user.mention}\n"
                    f"Team (from sheet): **{captain_team}**\n"
                    f"Drop: {player1.mention}\n"
                    f"Origin channel: <#{origin_channel_id}>\n"
                    f"Requested at (UTC): `{requested_at_iso}`"
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
                    f"❌ /drop failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Drop(bot))
