import os
import json
import logging
import traceback
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from utils.team_info import TEAM_INFO

load_dotenv()

logger = logging.getLogger("qrls.waiverclaim")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

DATA_DIR = "data"
WAIVERS_FILE = os.path.join(DATA_DIR, "waivers.json")


# ---------------------------
# Small utilities
# ---------------------------
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


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


def _is_waivers_team(value: str) -> bool:
    return _normalize(value).lower() == "waivers"


def _is_free_agent_team(value: str) -> bool:
    return _normalize(value).lower() == "free agent"


# ---------------------------
# Cog
# ---------------------------
class WaiverClaim(commands.Cog):
    """
    /waiverclaim flow (high-level):
    - Validates target player has Waivers role AND sheet team == "Waivers"
    - Validates user is a captain (CAPTAINS_ROLE_ID) and on a real team
    - Reads waiver order mapping from a waiver order worksheet
    - Writes/replaces claim inside data/waivers.json
    - Background loop checks expiry:
        - If no claim: finalize to Free Agent
        - If claim: ask claimant to confirm they still want it
            - If yes: send admin approval request
            - If no: clear claim and finalize to Free Agent
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Permissions / channel locks
        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")
        self.pending_channel_id = _get_env_int("PENDING_TRANSACTIONS_CHANNEL_ID")
        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        # Roles
        self.waivers_role_id = _get_env_int("WAIVERS_ROLE_ID")

        # Google Sheet
        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        self.roster_worksheet_name = os.getenv("GOOGLE_WORKSHEET", "")

        # Waiver order worksheet (NEW)
        self.waiver_order_worksheet_name = os.getenv("WAIVER_ORDER_WORKSHEET", "WaiverOrder")

        # Roster sheet columns: A=Discord ID, D=Team
        self.COL_DISCORD_ID = 0
        self.COL_TEAM = 3

        os.makedirs(DATA_DIR, exist_ok=True)

    async def cog_load(self):
        try:
            if not self.process_waiver_expirations.is_running():
                self.process_waiver_expirations.start()
        except Exception as e:
            logger.error("Failed starting waiver expiration loop: %r", e)

    async def cog_unload(self):
        try:
            if self.process_waiver_expirations.is_running():
                self.process_waiver_expirations.cancel()
        except Exception:
            pass

    # ---------------------------
    # Discord / Permission helpers
    # ---------------------------
    def _has_role_id(self, member: discord.Member, role_id: int) -> bool:
        return any(r.id == role_id for r in member.roles)

    def _is_admin_member(self, member: discord.Member) -> bool:
        if getattr(member.guild_permissions, "administrator", False):
            return True
        if self.admins_role_id and self._has_role_id(member, self.admins_role_id):
            return True
        return False

    # ---------------------------
    # JSON storage helpers
    # ---------------------------
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

    # ---------------------------
    # Google Sheet helpers
    # ---------------------------
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

    def _open_worksheet(self, worksheet_name: str):
        if not self.sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is missing from .env")
        if not worksheet_name:
            raise RuntimeError("Worksheet name is missing.")

        gc = self._get_gspread_client()
        sh = gc.open_by_key(self.sheet_id)
        ws = sh.worksheet(worksheet_name)
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

    def _count_team_players(self, values: list[list[str]], team_name: str) -> int:
        t = _normalize(team_name).lower()
        count = 0
        for row in values:
            if len(row) > self.COL_TEAM and _normalize(row[self.COL_TEAM]).lower() == t:
                count += 1
        return count

    def _load_waiver_order_map(self) -> Dict[str, int]:
        """
        Reads waiver order from worksheet WAIVER_ORDER_WORKSHEET (default: "WaiverOrder")

        Expected layout:
        - Column A: Team Name
        - Column B: Waiver Rank (1 = highest priority, 16 = lowest)

        Returns dict {normalized_team_name_lower: rank_int}
        """
        ws = self._open_worksheet(self.waiver_order_worksheet_name)
        values = ws.get("A1:B16")
        order: Dict[str, int] = {}

        for row in values:
            if not row:
                continue
            team = _normalize(row[0]) if len(row) > 0 else ""
            rank_raw = _normalize(row[1]) if len(row) > 1 else ""
            if not team or not rank_raw:
                continue
            try:
                rank = int(rank_raw)
            except ValueError:
                continue
            order[team.lower()] = rank

        return order

    # ---------------------------
    # Posting helpers
    # ---------------------------
    async def _post_transaction_log_waiver_win(self, team_name: str, player: discord.Member):
        if not self.transactions_channel_id:
            return
        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            return

        team_role_id = _get_team_role_id(team_name)
        team_text = f"<@&{team_role_id}>" if team_role_id else f"**{team_name}**"
        await ch.send(f"{team_text} has won the waiver claim for {player.mention}.")

    async def _post_to_channel(self, channel_id: int, message: str, allowed_mentions: Optional[discord.AllowedMentions] = None):
        ch = self.bot.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(message, allowed_mentions=allowed_mentions)

    # ---------------------------
    # Core: claim compare + save
    # ---------------------------
    def _get_record(self, data: Dict[str, Any], player_id: int) -> Optional[Dict[str, Any]]:
        rec = data.get(str(player_id))
        return rec if isinstance(rec, dict) else None

    def _get_claim(self, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        claim = rec.get("claim")
        return claim if isinstance(claim, dict) else None

    def _set_claim(
        self,
        data: Dict[str, Any],
        rec: Dict[str, Any],
        player_id: int,
        team_name: str,
        team_rank: int,
        claimed_by_id: int,
        origin_channel_id: int,
    ) -> None:
        rec["claim"] = {
            "team_name": team_name,
            "team_rank": team_rank,
            "claimed_by_id": claimed_by_id,
            "claimed_at": _utc_now().isoformat(),
            "origin_channel_id": origin_channel_id,
            "confirmed": None,  # None until expiry prompt, then True/False
            "confirmed_at": None,
        }
        data[str(player_id)] = rec
        self._save_waivers_json(data)

    def _clear_claim(self, data: Dict[str, Any], rec: Dict[str, Any], player_id: int) -> None:
        rec.pop("claim", None)
        data[str(player_id)] = rec
        self._save_waivers_json(data)

    # ---------------------------
    # Expiry finalize + role updates
    # ---------------------------
    async def _finalize_to_free_agent(self, guild: discord.Guild, player_id: int) -> Tuple[bool, str]:
        """
        If waiver expires with no claim (or claim declined), finalize:
        - Sheet team -> "Free Agent"
        - Remove Waivers role
        - Ensure Free Agent role
        """
        try:
            roster_ws = self._open_worksheet(self.roster_worksheet_name)
            values = roster_ws.get_all_values()
            row = self._find_row_index_by_discord_id(values, player_id)
            if not row:
                return False, "Player not found in roster sheet."

            roster_ws.update_cell(row, self.COL_TEAM + 1, "Free Agent")
        except Exception as e:
            logger.error("Finalize to FA failed (sheet) player=%s: %r", player_id, e)
            traceback.print_exc()
            return False, "Sheet update failed while finalizing to Free Agent."

        try:
            member = guild.get_member(player_id) or await guild.fetch_member(player_id)

            free_agent_role_id = _get_team_role_id("Free Agent")
            free_agent_role = guild.get_role(free_agent_role_id) if free_agent_role_id else None
            waivers_role = guild.get_role(self.waivers_role_id) if self.waivers_role_id else None

            to_remove = []
            to_add = []

            if waivers_role and waivers_role in member.roles:
                to_remove.append(waivers_role)
            if free_agent_role and free_agent_role not in member.roles:
                to_add.append(free_agent_role)

            if to_remove:
                await member.remove_roles(*to_remove, reason="Waiver expired: move to Free Agent")
            if to_add:
                await member.add_roles(*to_add, reason="Waiver expired: ensure Free Agent role")

            return True, f"Finalized {member.mention} -> Free Agent."
        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (sheet updated)."
        except discord.NotFound:
            return False, "Member not found in server (sheet updated)."
        except Exception as e:
            logger.error("Finalize to FA failed (roles) player=%s: %r", player_id, e)
            traceback.print_exc()
            return False, "Role update failed while finalizing to Free Agent (sheet updated)."

    async def _apply_claim_award(
        self,
        guild: discord.Guild,
        player_id: int,
        winning_team: str,
    ) -> Tuple[bool, str]:
        """
        Called after admin approves:
        - Check roster spot available (<4)
        - Sheet team -> winning_team
        - Discord: remove Waivers + Free Agent, add winning team role
        """
        roster_ws = self._open_worksheet(self.roster_worksheet_name)
        values = roster_ws.get_all_values()
        row = self._find_row_index_by_discord_id(values, player_id)
        if not row:
            return False, "Player not found in roster sheet."

        # Re-check player is still on Waivers in sheet
        current_team = self._get_team_from_row(values, row)
        if not _is_waivers_team(current_team):
            return False, f"Approval failed: player is no longer listed as Waivers in the sheet (currently: {current_team or 'blank'})."

        # Roster cap check
        team_count = self._count_team_players(values, winning_team)
        if team_count >= 4:
            return False, f"Approval failed: **{winning_team}** already has **{team_count}** players (max 4). Run `/drop` to free a roster spot."

        # Sheet update
        try:
            roster_ws.update_cell(row, self.COL_TEAM + 1, winning_team)
        except Exception as e:
            logger.error("Claim award sheet update failed player=%s: %r", player_id, e)
            traceback.print_exc()
            return False, "Sheet update failed while awarding waiver claim."

        # Discord role updates
        try:
            member = guild.get_member(player_id) or await guild.fetch_member(player_id)

            waivers_role = guild.get_role(self.waivers_role_id) if self.waivers_role_id else None
            free_agent_role_id = _get_team_role_id("Free Agent")
            free_agent_role = guild.get_role(free_agent_role_id) if free_agent_role_id else None

            team_role_id = _get_team_role_id(winning_team)
            if not team_role_id:
                return False, f"Winning team role id missing/invalid in TEAM_INFO for `{winning_team}`."
            team_role = guild.get_role(team_role_id)
            if not team_role:
                return False, f"Winning team role not found in server (id={team_role_id})."

            to_remove = []
            to_add = []

            if waivers_role and waivers_role in member.roles:
                to_remove.append(waivers_role)
            if free_agent_role and free_agent_role in member.roles:
                to_remove.append(free_agent_role)
            if team_role not in member.roles:
                to_add.append(team_role)

            if to_remove:
                await member.remove_roles(*to_remove, reason="Waiver claim awarded")
            if to_add:
                await member.add_roles(*to_add, reason="Waiver claim awarded")

            return True, f"Awarded waiver claim: {member.mention} -> **{winning_team}**."
        except discord.Forbidden:
            return False, "Bot lacks permission to manage roles (sheet updated)."
        except discord.NotFound:
            return False, "Member not found in server (sheet updated)."
        except Exception as e:
            logger.error("Claim award role update failed player=%s: %r", player_id, e)
            traceback.print_exc()
            return False, "Role update failed while awarding waiver claim (sheet updated)."

    # ---------------------------
    # Views for expiry confirmation + admin approval
    # ---------------------------
    class ConfirmClaimView(discord.ui.View):
        def __init__(self, cog: "WaiverClaim", player_id: int, claimant_id: int):
            super().__init__(timeout=60 * 30)  # 30 minutes to confirm
            self.cog = cog
            self.player_id = player_id
            self.claimant_id = claimant_id
            self.decided = False

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if self.decided:
                try:
                    await interaction.response.send_message("ℹ️ Already decided.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            if interaction.user.id != self.claimant_id:
                try:
                    await interaction.response.send_message("🚫 Only the claimant can respond.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False

            return True

        async def _disable(self, interaction: discord.Interaction, text: str):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            try:
                await interaction.message.edit(content=text, view=self)
            except discord.HTTPException:
                pass

        @discord.ui.button(label="Yes, keep my claim", style=discord.ButtonStyle.success)
        async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass

            data = self.cog._load_waivers_json()
            rec = self.cog._get_record(data, self.player_id)
            if not rec:
                try:
                    await interaction.followup.send("❌ This waiver record no longer exists.", ephemeral=True)
                except discord.HTTPException:
                    pass
                await self._disable(interaction, "❌ Claim confirmation failed (record missing).")
                return

            claim = self.cog._get_claim(rec)
            if not claim or claim.get("claimed_by_id") != self.claimant_id:
                try:
                    await interaction.followup.send("❌ This claim is no longer yours (or no longer exists).", ephemeral=True)
                except discord.HTTPException:
                    pass
                await self._disable(interaction, "❌ Claim confirmation failed (claim missing).")
                return

            claim["confirmed"] = True
            claim["confirmed_at"] = _utc_now().isoformat()
            rec["claim"] = claim
            data[str(self.player_id)] = rec
            self.cog._save_waivers_json(data)

            # Notify claimant we are sending to admins
            try:
                await interaction.followup.send("✅ Got it — sending to admins for approval now.", ephemeral=True)
            except discord.HTTPException:
                pass

            # Kick off admin approval request
            try:
                await self.cog._send_admin_approval_request(
                    guild=interaction.guild,
                    player_id=self.player_id
                )
            except Exception as e:
                logger.error("Failed to send admin approval request: %r", e)
                traceback.print_exc()

            await self._disable(interaction, "✅ Claim confirmed — awaiting admin approval.")

        @discord.ui.button(label="No, drop the claim", style=discord.ButtonStyle.danger)
        async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass

            data = self.cog._load_waivers_json()
            rec = self.cog._get_record(data, self.player_id)
            if rec:
                claim = self.cog._get_claim(rec)
                if claim and claim.get("claimed_by_id") == self.claimant_id:
                    claim["confirmed"] = False
                    claim["confirmed_at"] = _utc_now().isoformat()
                    rec["claim"] = claim
                    data[str(self.player_id)] = rec
                    self.cog._save_waivers_json(data)

            # Finalize to Free Agent and clear record
            try:
                if interaction.guild:
                    ok, msg = await self.cog._finalize_to_free_agent(interaction.guild, self.player_id)
                    if ok:
                        # remove record entirely
                        data2 = self.cog._load_waivers_json()
                        data2.pop(str(self.player_id), None)
                        self.cog._save_waivers_json(data2)
                    await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass

            await self._disable(interaction, "🚫 Claim declined — player will become Free Agent.")

        async def on_timeout(self):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    class AdminApproveView(discord.ui.View):
        def __init__(self, cog: "WaiverClaim", player_id: int):
            super().__init__(timeout=60 * 60)  # 1 hour
            self.cog = cog
            self.player_id = player_id
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
                    await interaction.response.send_message("ℹ️ Already decided.", ephemeral=True)
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

        async def _finalize_buttons(self, interaction: discord.Interaction, status_text: str):
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

            data = self.cog._load_waivers_json()
            rec = self.cog._get_record(data, self.player_id)
            if not rec:
                try:
                    await interaction.followup.send("❌ Waiver record missing.", ephemeral=True)
                except discord.HTTPException:
                    pass
                await self._finalize_buttons(interaction, "❌ Approval failed (record missing).")
                return

            claim = self.cog._get_claim(rec)
            if not claim:
                try:
                    await interaction.followup.send("❌ No claim exists on this player.", ephemeral=True)
                except discord.HTTPException:
                    pass
                await self._finalize_buttons(interaction, "❌ Approval failed (no claim).")
                return

            winning_team = _normalize(str(claim.get("team_name") or ""))
            claimant_id = claim.get("claimed_by_id")
            origin_channel_id = claim.get("origin_channel_id")

            if not winning_team or not isinstance(claimant_id, int) or not isinstance(origin_channel_id, int):
                try:
                    await interaction.followup.send("❌ Claim data is invalid.", ephemeral=True)
                except discord.HTTPException:
                    pass
                await self._finalize_buttons(interaction, "❌ Approval failed (invalid claim data).")
                return

            # Apply award (includes roster spot check)
            ok, msg = await self.cog._apply_claim_award(
                guild=interaction.guild,
                player_id=self.player_id,
                winning_team=winning_team,
            )

            try:
                await interaction.followup.send(("✅ " + msg) if ok else ("❌ " + msg), ephemeral=True)
            except discord.HTTPException:
                pass

            # Notify claimant in origin channel
            try:
                claimant_member = interaction.guild.get_member(claimant_id) or await interaction.guild.fetch_member(claimant_id)
            except Exception:
                claimant_member = None

            try:
                player_member = interaction.guild.get_member(self.player_id) or await interaction.guild.fetch_member(self.player_id)
            except Exception:
                player_member = None

            if ok:
                # Transaction log
                if player_member:
                    try:
                        await self.cog._post_transaction_log_waiver_win(winning_team, player_member)
                    except Exception:
                        pass

                # Clear record (resolved)
                data2 = self.cog._load_waivers_json()
                data2.pop(str(self.player_id), None)
                self.cog._save_waivers_json(data2)

                if origin_channel_id:
                    await self.cog._post_to_channel(
                        origin_channel_id,
                        f"✅ **Waiver Claim Approved** — {('**' + winning_team + '**')} wins the claim for {player_member.mention if player_member else f'`{self.player_id}`'}."
                    )

                await self._finalize_buttons(interaction, f"✅ Approved — **{winning_team}** wins the claim.")
            else:
                # Keep claim record (still pending), but tell origin channel it failed (usually roster spot)
                if origin_channel_id:
                    await self.cog._post_to_channel(
                        origin_channel_id,
                        f"❌ **Waiver Claim Approval Failed** — {msg}\n"
                        f"ℹ️ If this is a roster spot issue, run `/drop` to free a spot and then have admins re-approve."
                    )
                await self._finalize_buttons(interaction, f"❌ Approval failed — {msg}")

        @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
        async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass

            data = self.cog._load_waivers_json()
            rec = self.cog._get_record(data, self.player_id)
            claim = self.cog._get_claim(rec) if rec else None

            origin_channel_id = claim.get("origin_channel_id") if claim else None
            winning_team = _normalize(str(claim.get("team_name") or "")) if claim else ""
            claimant_id = claim.get("claimed_by_id") if claim else None

            # On admin reject, we just clear the claim and finalize to Free Agent (since waiver is already expired)
            try:
                if interaction.guild:
                    ok, msg = await self.cog._finalize_to_free_agent(interaction.guild, self.player_id)
                    if ok:
                        # clear record entirely
                        data2 = self.cog._load_waivers_json()
                        data2.pop(str(self.player_id), None)
                        self.cog._save_waivers_json(data2)
            except Exception:
                msg = "Rejected; encountered an error finalizing to Free Agent (check console)."

            if origin_channel_id:
                await self.cog._post_to_channel(
                    origin_channel_id,
                    f"🚫 **Waiver Claim Rejected** — admins rejected {('**' + winning_team + '**') if winning_team else 'the claim'}."
                )

            try:
                await interaction.followup.send("🚫 Rejected.", ephemeral=True)
            except discord.HTTPException:
                pass

            await self._finalize_buttons(interaction, "🚫 Rejected by admin.")

        async def on_timeout(self):
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    async def _send_admin_approval_request(self, guild: discord.Guild, player_id: int):
        """
        Posts to PENDING_TRANSACTIONS_CHANNEL_ID for admins to approve claim.
        Includes roster spot warning message.
        """
        if not self.pending_channel_id:
            return
        if not self.admins_role_id:
            return

        pending_ch = self.bot.get_channel(self.pending_channel_id)
        if not isinstance(pending_ch, discord.TextChannel):
            return

        data = self._load_waivers_json()
        rec = self._get_record(data, player_id)
        if not rec:
            return

        claim = self._get_claim(rec)
        if not claim:
            return

        team_name = _normalize(str(claim.get("team_name") or ""))
        claimant_id = claim.get("claimed_by_id")
        origin_channel_id = claim.get("origin_channel_id")

        if not team_name or not isinstance(claimant_id, int) or not isinstance(origin_channel_id, int):
            return

        player_member = guild.get_member(player_id) or await guild.fetch_member(player_id)
        claimant_member = guild.get_member(claimant_id) or await guild.fetch_member(claimant_id)

        admins_role_mention = f"<@&{self.admins_role_id}>"
        view = WaiverClaim.AdminApproveView(cog=self, player_id=player_id)

        await pending_ch.send(
            content=(
                f"{admins_role_mention} **Pending Waiver Claim Approval**\n"
                f"Claiming Team: **{team_name}**\n"
                f"Claimant: {claimant_member.mention}\n"
                f"Player: {player_member.mention}\n"
                f"Origin channel: <#{origin_channel_id}>\n\n"
                f"⚠️ **Roster Limit Notice**: If **{team_name}** already has 4 players on the roster sheet, approval will fail.\n"
                f"➡️ If needed, run `/drop` first to create a roster spot."
            ),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
            view=view
        )

        # Tell origin channel it's waiting for admin approval
        await self._post_to_channel(
            origin_channel_id,
            f"ℹ️ {claimant_member.mention} — your waiver claim for {player_member.mention} is **waiting for admin approval**.\n"
            f"⚠️ If your roster is full (4/4), the approval will fail until you free a spot using `/drop`.",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )

    # ---------------------------
    # Background loop: process expirations
    # ---------------------------
    @tasks.loop(minutes=5)
    async def process_waiver_expirations(self):
        """
        Every 5 minutes:
        - If expires_at <= now:
            - If no claim: finalize to Free Agent + remove record
            - If claim exists:
                - If not yet confirmed: prompt claimant in origin channel with Yes/No buttons
                - If confirmed True: (ensure admin request exists by sending it now)
                - If confirmed False: finalize to Free Agent + remove record
        """
        try:
            data = self._load_waivers_json()
            if not data:
                return

            now = _utc_now()
            changed = False

            for player_key, rec in list(data.items()):
                if not isinstance(rec, dict):
                    continue

                expires_at = _parse_iso_dt(str(rec.get("expires_at") or ""))
                if not expires_at:
                    continue
                if expires_at > now:
                    continue

                guild_id = rec.get("guild_id")
                player_id = rec.get("player_id")
                if not isinstance(guild_id, int) or not isinstance(player_id, int):
                    continue

                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue

                claim = rec.get("claim") if isinstance(rec.get("claim"), dict) else None

                # No claim -> finalize to Free Agent
                if not claim:
                    ok, msg = await self._finalize_to_free_agent(guild, player_id)
                    if ok:
                        data.pop(player_key, None)
                        changed = True
                    continue

                # Claim exists -> handle confirmation
                claimant_id = claim.get("claimed_by_id")
                origin_channel_id = claim.get("origin_channel_id")
                team_name = _normalize(str(claim.get("team_name") or ""))

                if not isinstance(claimant_id, int) or not isinstance(origin_channel_id, int) or not team_name:
                    continue

                confirmed = claim.get("confirmed", None)

                # If explicitly declined -> finalize to FA and remove record
                if confirmed is False:
                    ok, msg = await self._finalize_to_free_agent(guild, player_id)
                    if ok:
                        data.pop(player_key, None)
                        changed = True
                    continue

                # If confirmed True -> send to admins (idempotent: sending twice is annoying but safe)
                if confirmed is True:
                    try:
                        await self._send_admin_approval_request(guild=guild, player_id=player_id)
                    except Exception:
                        pass
                    continue

                # confirmed is None -> prompt claimant to confirm
                try:
                    ch = self.bot.get_channel(origin_channel_id)
                    if not isinstance(ch, discord.TextChannel):
                        continue

                    player_member = guild.get_member(player_id) or await guild.fetch_member(player_id)
                    claimant_member = guild.get_member(claimant_id) or await guild.fetch_member(claimant_id)

                    view = WaiverClaim.ConfirmClaimView(cog=self, player_id=player_id, claimant_id=claimant_id)
                    await ch.send(
                        content=(
                            f"{claimant_member.mention} — **Waivers have expired** for {player_member.mention}.\n"
                            f"Do you still want to proceed with your claim for **{team_name}**?\n"
                            f"✅ If yes, this will be sent to admins for approval.\n"
                            f"⚠️ If your roster is full (4/4), approval will fail until you run `/drop`."
                        ),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                        view=view
                    )

                    # Mark that we prompted (so we don't spam every 5 minutes)
                    claim["confirmed"] = "PROMPTED"
                    claim["confirmed_at"] = _utc_now().isoformat()
                    rec["claim"] = claim
                    data[player_key] = rec
                    changed = True

                except Exception as e:
                    logger.error("Failed sending expiry confirmation prompt: %r", e)
                    traceback.print_exc()

            # Convert PROMPTED -> None behavior:
            # We store PROMPTED just to avoid spamming; if claimant never answers,
            # you can decide later to auto-decline. For now we simply leave it as PROMPTED.
            if changed:
                self._save_waivers_json(data)

        except Exception as e:
            logger.error("process_waiver_expirations error: %r", e)
            traceback.print_exc()

    @process_waiver_expirations.before_loop
    async def before_process_waiver_expirations(self):
        await self.bot.wait_until_ready()

    # ---------------------------
    # /waiverclaim command
    # ---------------------------
    @app_commands.command(
        name="waiverclaim",
        description="Place a waiver claim on a player (must currently be on Waivers)."
    )
    @app_commands.guild_only()
    async def waiverclaim(self, interaction: Interaction, player1: discord.Member):
        step = "START"
        try:
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # --- Env validation ---
            step = "ENV_VALIDATE"
            if not isinstance(interaction.user, discord.Member):
                await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
                return
            if not self.captains_role_id:
                await interaction.followup.send("❌ CAPTAINS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.transactions_category_id:
                await interaction.followup.send("❌ TRANSACTIONS_CATEGORY_ID is missing/invalid in .env", ephemeral=True)
                return
            if not self.waivers_role_id:
                await interaction.followup.send("❌ WAIVERS_ROLE_ID is missing/invalid in .env", ephemeral=True)
                return

            # --- Captain-only restriction ---
            step = "CAPTAIN_CHECK"
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
                await interaction.followup.send("🚫 This command can only be used in the Transactions category.", ephemeral=True)
                return
            origin_channel_id = base_channel.id

            # 1) Check target player's roles: must have Waivers role
            step = "TARGET_ROLE_CHECK"
            waivers_role = interaction.guild.get_role(self.waivers_role_id) if interaction.guild else None
            if not waivers_role:
                await interaction.followup.send("❌ Waivers role not found in server (check WAIVERS_ROLE_ID).", ephemeral=True)
                return
            if waivers_role not in player1.roles:
                await interaction.followup.send("🚫 That player does not have the Waivers role.", ephemeral=True)
                return

            # 2) Check target player's team in sheet: must be "Waivers"
            step = "OPEN_ROSTER_SHEET"
            roster_ws = self._open_worksheet(self.roster_worksheet_name)
            values = roster_ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Roster worksheet is empty.", ephemeral=True)
                return

            step = "FIND_TARGET_ROW"
            target_row = self._find_row_index_by_discord_id(values, player1.id)
            if not target_row:
                await interaction.followup.send("❌ Target player not found in roster sheet (Column A).", ephemeral=True)
                return
            target_team = self._get_team_from_row(values, target_row)
            if not _is_waivers_team(target_team):
                await interaction.followup.send(
                    f"🚫 Target player is not listed as **Waivers** in the sheet (currently: **{target_team or 'blank'}**).",
                    ephemeral=True
                )
                return

            # Determine claimant team from sheet (not from roles)
            step = "FIND_CLAIMANT_ROW"
            claimant_row = self._find_row_index_by_discord_id(values, interaction.user.id)
            if not claimant_row:
                await interaction.followup.send("❌ You (claimant) are not found in the roster sheet (Column A).", ephemeral=True)
                return
            claimant_team = self._get_team_from_row(values, claimant_row)
            if not claimant_team:
                await interaction.followup.send("❌ Your team is blank in the roster sheet.", ephemeral=True)
                return
            if _is_free_agent_team(claimant_team) or _is_waivers_team(claimant_team):
                await interaction.followup.send("🚫 You must be on a valid team to place a claim.", ephemeral=True)
                return

            # 3) Waiver order compare
            step = "LOAD_WAIVER_ORDER"
            try:
                waiver_order = self._load_waiver_order_map()
            except Exception as e:
                logger.error("Failed reading waiver order worksheet: %r", e)
                traceback.print_exc()
                await interaction.followup.send(
                    f"❌ Could not read waiver order from worksheet `{self.waiver_order_worksheet_name}`.\n"
                    f"Make sure it exists and has **Team Name (col A)** and **Rank (col B)**.",
                    ephemeral=True
                )
                return

            claimant_rank = waiver_order.get(claimant_team.lower())
            if claimant_rank is None:
                await interaction.followup.send(
                    f"❌ Your team (**{claimant_team}**) was not found in the waiver order sheet `{self.waiver_order_worksheet_name}`.",
                    ephemeral=True
                )
                return

            # 4) Must have an existing waiver record in waivers.json (created by /drop approval)
            step = "LOAD_WAIVER_RECORD"
            data = self._load_waivers_json()
            rec = self._get_record(data, player1.id)
            if not rec:
                await interaction.followup.send(
                    "❌ This player does not have an active waiver record in `data/waivers.json`.\n"
                    "They may not have been dropped correctly (or the record was removed).",
                    ephemeral=True
                )
                return

            # Verify still not expired
            expires_at = _parse_iso_dt(str(rec.get("expires_at") or ""))
            if not expires_at:
                await interaction.followup.send("❌ Waiver record is missing/invalid `expires_at`.", ephemeral=True)
                return
            if expires_at <= _utc_now():
                await interaction.followup.send("🚫 Waivers have already expired for this player.", ephemeral=True)
                return

            # Compare claims
            step = "COMPARE_EXISTING_CLAIM"
            existing_claim = self._get_claim(rec)
            if existing_claim:
                existing_team = _normalize(str(existing_claim.get("team_name") or ""))
                existing_rank = existing_claim.get("team_rank")
                if not isinstance(existing_rank, int):
                    # If rank missing, treat as lowest priority and allow overwrite
                    existing_rank = 9999

                # 1 is highest priority. Smaller number = better claim.
                if existing_rank < claimant_rank:
                    await interaction.followup.send("🚫 A claim already exists that is higher than yours.", ephemeral=True)
                    return

                # If claimant is higher priority (smaller rank) -> replace
                if claimant_rank < existing_rank:
                    self._set_claim(
                        data=data,
                        rec=rec,
                        player_id=player1.id,
                        team_name=claimant_team,
                        team_rank=claimant_rank,
                        claimed_by_id=interaction.user.id,
                        origin_channel_id=origin_channel_id,
                    )
                    await interaction.followup.send(
                        f"✅ Your claim replaced the existing claim.\n"
                        f"Player: {player1.mention}\n"
                        f"Claiming Team: **{claimant_team}** (rank {claimant_rank})",
                        ephemeral=True
                    )
                    await base_channel.send(
                        f"✅ **Waiver Claim Updated** — {interaction.user.mention} placed a higher-priority claim for {player1.mention} "
                        f"as **{claimant_team}**."
                    )
                    return

                # Equal rank (shouldn't happen normally): do not replace
                await interaction.followup.send("🚫 A claim already exists with equal priority.", ephemeral=True)
                return

            # No claim yet -> add claim
            step = "SET_NEW_CLAIM"
            self._set_claim(
                data=data,
                rec=rec,
                player_id=player1.id,
                team_name=claimant_team,
                team_rank=claimant_rank,
                claimed_by_id=interaction.user.id,
                origin_channel_id=origin_channel_id,
            )

            await interaction.followup.send(
                f"✅ Claim placed.\nPlayer: {player1.mention}\nClaiming Team: **{claimant_team}** (rank {claimant_rank})\n"
                f"Waivers end: <t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)",
                ephemeral=True
            )

            await base_channel.send(
                f"✅ **Waiver Claim Placed** — {interaction.user.mention} claimed {player1.mention} as **{claimant_team}**.\n"
                f"🗓️ Waivers end: <t:{int(expires_at.timestamp())}:F> (<t:{int(expires_at.timestamp())}:R>)"
            )

        except Exception as e:
            logger.error("ERROR at step=%s: %r", step, e)
            traceback.print_exc()
            try:
                await interaction.followup.send(
                    f"❌ /waiverclaim failed at step: **{step}** (check bot console for traceback).",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(WaiverClaim(bot))