import os
import json
import logging
import traceback
import asyncio
from typing import Optional, Dict, Any, List
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


def _get_team_name_from_role_id(role_id: int) -> Optional[str]:
    for team_name, info in TEAM_INFO.items():
        if isinstance(info, dict) and info.get("id") == role_id:
            return team_name
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
    """
    /sub:
      - Captain-only (CAPTAINS_ROLE_ID)
      - Category-locked (TRANSACTIONS_CATEGORY_ID)
      - Requires Admin Approval (ADMINS_ROLE_ID) via buttons in PENDING_TRANSACTIONS_CHANNEL_ID
      - No Google Sheet changes
      - Validations:
          1) player1 must be on captain's team (sheet col D)
          2) player2 must be Free Agent (sheet col D)
          3) player2 cannot already have an active sub (persisted)
      - On approve:
          - add captain team role to player2 temporarily until Sunday 11:59pm ET
          - keep Free Agent role
          - persist to data/subs.json so it survives restarts
      - Transaction log (TRANSACTIONS_CHANNEL_ID):
          "@[Team] signs @[player2] in place of @[player1] on a sub deal for the week."
      - Expiration log (CHANGELOG_CHANNEL_ID):
          sent when the bot removes the temp role at expiry
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        self.transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")

        self.admins_role_id = _get_env_int("ADMINS_ROLE_ID")
        self.pending_channel_id = _get_env_int("PENDING_TRANSACTIONS_CHANNEL_ID")
        self.transactions_channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")

        # ✅ new: changelog channel
        self.changelog_channel_id = _get_env_int("CHANGELOG_CHANNEL_ID")

        self.sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        self.sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        self.worksheet_name = os.getenv("GOOGLE_WORKSHEET", "")

        # Sheet columns: A=Discord ID, D=Team
        self.COL_DISCORD_ID = 0
        self.COL_TEAM = 3

        # Persistence
        self.subs_path = os.path.join("data", "subs.json")
        self._subs_lock = asyncio.Lock()
        self._removal_tasks: Dict[str, asyncio.Task] = {}

        # Kick off rehydration ASAP
        self.bot.loop.create_task(self._rehydrate_subs())

    # ---------------------------
    # Helpers: Permissions
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
    # Helpers: Google Sheet
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
        Returns 1-based row index for gspread.
        """
        target = str(discord_id)
        for i, row in enumerate(values, start=1):
            if len(row) > self.COL_DISCORD_ID and _normalize(row[self.COL_DISCORD_ID]) == target:
                return i
        return None

    def _get_team_from_row(self, values: list[list[str]], row_index_1based: int) -> str:
        row = values[row_index_1based - 1]
        return _normalize(row[self.COL_TEAM]) if len(row) > self.COL_TEAM else ""

    # ---------------------------
    # Helpers: Messaging
    # ---------------------------
    async def _post_in_origin_channel(self, origin_channel_id: int, message: str):
        ch = self.bot.get_channel(origin_channel_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(message)

    async def _post_transaction_log(self, team_name: str, player1: discord.Member, player2: discord.Member):
        """
        "@Team signs @player2 in place of @player1 on a sub deal for the week."
        Team should be role-pinged via TEAM_INFO.
        """
        if not self.transactions_channel_id:
            logger.warning("TRANSACTIONS_CHANNEL_ID missing/invalid; skipping transaction log.")
            return

        ch = self.bot.get_channel(self.transactions_channel_id)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel; skipping.")
            return

        team_role_id = _get_team_role_id(team_name)
        team_text = f"<@&{team_role_id}>" if team_role_id else f"**{team_name}**"

        await ch.send(f"{team_text} signs {player2.mention} in place of {player1.mention} on a sub deal for the week.")

    async def _post_changelog_expiration(
        self,
        guild: discord.Guild,
        user_id: int,
        role_id: int,
        record: Optional[Dict[str, Any]] = None
    ):
        """
        Log to CHANGELOG_CHANNEL_ID when a temp sub role is removed by the bot.
        """
        if not self.changelog_channel_id:
            return

        ch = self.bot.get_channel(self.changelog_channel_id)
        if not isinstance(ch, discord.TextChannel):
            return

        member = guild.get_member(user_id)
        # Use role mention if possible
        role = guild.get_role(role_id)

        # Best-effort names from record
        team_name = None
        if record and record.get("team_name"):
            team_name = record.get("team_name")
        if not team_name:
            team_name = _get_team_name_from_role_id(role_id)

        team_role_id = _get_team_role_id(team_name) if team_name else None
        team_text = f"<@&{team_role_id}>" if team_role_id else (f"**{team_name}**" if team_name else f"role_id={role_id}")
        player_text = member.mention if isinstance(member, discord.Member) else f"<@{user_id}>"

        # Include who they subbed for if we have it
        p1_id = record.get("player1_id") if record else None
        p1_text = f"<@{p1_id}>" if p1_id else None

        if p1_text:
            msg = f"🕒 Sub deal expired — removed {team_text} from {player_text} (was subbing in place of {p1_text})."
        else:
            msg = f"🕒 Sub deal expired — removed {team_text} from {player_text}."

        await ch.send(
            msg,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )

    # ---------------------------
    # Persistence: subs.json
    # ---------------------------
    def _make_sub_key(self, guild_id: int, user_id: int, role_id: int, expires_at_iso: str) -> str:
        return f"{guild_id}:{user_id}:{role_id}:{expires_at_iso}"

    async def _load_subs(self) -> List[Dict[str, Any]]:
        async with self._subs_lock:
            os.makedirs(os.path.dirname(self.subs_path), exist_ok=True)
            if not os.path.exists(self.subs_path):
                return []
            try:
                with open(self.subs_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
                return []
            except Exception as e:
                logger.error("Failed to read %s: %r", self.subs_path, e)
                traceback.print_exc()
                return []

    async def _save_subs(self, subs: List[Dict[str, Any]]):
        async with self._subs_lock:
            os.makedirs(os.path.dirname(self.subs_path), exist_ok=True)
            with open(self.subs_path, "w", encoding="utf-8") as f:
                json.dump(subs, f, indent=2)

    async def _add_sub_record(self, record: Dict[str, Any]):
        subs = await self._load_subs()
        subs.append(record)
        await self._save_subs(subs)

    async def _remove_sub_record_by_key(self, key: str):
        subs = await self._load_subs()
        new_subs = [r for r in subs if r.get("_key") != key]
        await self._save_subs(new_subs)

    async def _find_active_sub_for_user(self, guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Returns an active (not expired) sub record for this user, if any.
        This enforces: a sub cannot be used for another team while active.
        """
        subs = await self._load_subs()
        now_et = datetime.now(EASTERN)
        for r in subs:
            try:
                if int(r.get("guild_id", 0)) != int(guild_id):
                    continue
                if int(r.get("user_id", 0)) != int(user_id):
                    continue
                expires_at = datetime.fromisoformat(r["expires_at"])
                if expires_at > now_et:
                    return r
            except Exception:
                continue
        return None

    async def _rehydrate_subs(self):
        """
        On startup: load subs.json and schedule removals (or remove immediately if expired).
        """
        await self.bot.wait_until_ready()

        subs = await self._load_subs()
        if not subs:
            logger.info("No persisted subs to rehydrate.")
            return

        logger.info("Rehydrating %s persisted sub(s)...", len(subs))
        for rec in subs:
            try:
                guild_id = int(rec["guild_id"])
                user_id = int(rec["user_id"])
                role_id = int(rec["role_id"])
                expires_at = datetime.fromisoformat(rec["expires_at"])
                key = rec.get("_key") or self._make_sub_key(guild_id, user_id, role_id, rec["expires_at"])
                rec["_key"] = key

                now_et = datetime.now(EASTERN)
                if expires_at <= now_et:
                    self.bot.loop.create_task(self._remove_role_and_cleanup(guild_id, user_id, role_id, key, rec))
                    continue

                self._schedule_removal(guild_id, user_id, role_id, expires_at, key, rec)
            except Exception as e:
                logger.error("Bad sub record in file: %r | %r", e, rec)

        await self._save_subs(subs)

    def _schedule_removal(
        self,
        guild_id: int,
        user_id: int,
        role_id: int,
        expires_at: datetime,
        key: str,
        record: Optional[Dict[str, Any]] = None
    ):
        if key in self._removal_tasks and not self._removal_tasks[key].done():
            return

        seconds = max(0, (expires_at - datetime.now(EASTERN)).total_seconds())

        async def _job():
            try:
                await asyncio.sleep(seconds)
                await self._remove_role_and_cleanup(guild_id, user_id, role_id, key, record)
            except Exception as e:
                logger.error("Scheduled removal job failed: %r", e)
                traceback.print_exc()

        self._removal_tasks[key] = self.bot.loop.create_task(_job())
        logger.info("Scheduled sub role removal key=%s in %ss", key, int(seconds))

    async def _remove_role_and_cleanup(
        self,
        guild_id: int,
        user_id: int,
        role_id: int,
        key: str,
        record: Optional[Dict[str, Any]] = None
    ):
        """
        Remove the temp team role and remove the record from subs.json.
        Also logs to CHANGELOG_CHANNEL_ID when removal happens.
        """
        guild = self.bot.get_guild(guild_id)
        if not guild:
            await self._remove_sub_record_by_key(key)
            return

        role = guild.get_role(role_id)
        if not role:
            await self._remove_sub_record_by_key(key)
            return

        removed = False
        try:
            member = guild.get_member(user_id)
            if member is None:
                member = await guild.fetch_member(user_id)

            if role in member.roles:
                await member.remove_roles(role, reason="/sub expired: remove temporary sub role")
                removed = True
                logger.info("Expired sub: removed role_id=%s from user_id=%s in guild=%s", role_id, user_id, guild_id)

        except discord.Forbidden:
            logger.error("Expired sub: missing perms to remove role_id=%s from user_id=%s", role_id, user_id)
        except discord.NotFound:
            logger.warning("Expired sub: user_id=%s not found in guild=%s", user_id, guild_id)
        except Exception as e:
            logger.error("Expired sub: unexpected error: %r", e)
            traceback.print_exc()
        finally:
            # ✅ Changelog only when bot actually removed the role
            if removed:
                try:
                    await self._post_changelog_expiration(guild, user_id, role_id, record)
                except Exception as e:
                    logger.error("Changelog post failed: %r", e)

            await self._remove_sub_record_by_key(key)
            t = self._removal_tasks.pop(key, None)
            if t and not t.done():
                t.cancel()

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
            player1_id: int,
            player1_display: str,
            player2_id: int,
            player2_display: str,
            expires_at: datetime,
        ):
            super().__init__(timeout=60 * 60 * 24)  # 24 hour
            self.cog = cog
            self.origin_channel_id = origin_channel_id
            self.captain_id = captain_id
            self.captain_team = captain_team

            self.player1_id = player1_id
            self.player1_display = player1_display
            self.player2_id = player2_id
            self.player2_display = player2_display

            self.expires_at = expires_at
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
                    await interaction.response.send_message("ℹ️ This request has already been decided.", ephemeral=True)
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
                guild = interaction.guild
                if guild is None:
                    await self._finalize_buttons(interaction, "❌ Failed (no guild).")
                    try:
                        await interaction.followup.send("❌ Server not found.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                # ✅ Enforce "sub not already in use" at approval-time too
                active = await self.cog._find_active_sub_for_user(guild.id, self.player2_id)
                if active:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"🚫 Sub auto-rejected: {self.player2_display} already has an active sub deal."
                    )
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (player2 already subbed).")
                    try:
                        await interaction.followup.send("🚫 Auto-rejected: player2 already has an active sub deal.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                # Re-check sheet conditions:
                ws = self.cog._open_worksheet()
                values = ws.get_all_values()

                cap_row = self.cog._find_row_index_by_discord_id(values, self.captain_id)
                if not cap_row:
                    await self.cog._post_in_origin_channel(self.origin_channel_id, "❌ Sub approval failed (captain not found in sheet).")
                    await self._finalize_buttons(interaction, "❌ Failed (captain not found in sheet).")
                    try:
                        await interaction.followup.send("❌ Captain not found in sheet anymore.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                cap_team_current = self.cog._get_team_from_row(values, cap_row)
                if _normalize(cap_team_current) != _normalize(self.captain_team):
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"🚫 Sub auto-rejected: captain team changed (was **{self.captain_team}**, now **{cap_team_current or 'Unknown'}**)."
                    )
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (captain team changed).")
                    try:
                        await interaction.followup.send("🚫 Auto-rejected: captain team changed in sheet.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                p1_row = self.cog._find_row_index_by_discord_id(values, self.player1_id)
                if not p1_row:
                    await self.cog._post_in_origin_channel(self.origin_channel_id, "🚫 Sub auto-rejected: player being subbed is no longer in the sheet.")
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (player1 not in sheet).")
                    try:
                        await interaction.followup.send("🚫 Auto-rejected: player1 not found in sheet.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                p1_team = self.cog._get_team_from_row(values, p1_row)
                if _normalize(p1_team) != _normalize(self.captain_team):
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"🚫 Sub auto-rejected: {self.player1_display} is not on **{self.captain_team}** (currently **{p1_team or 'Unknown'}**)."
                    )
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (player1 not on captain team).")
                    try:
                        await interaction.followup.send("🚫 Auto-rejected: player1 is not on captain's team.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                p2_row = self.cog._find_row_index_by_discord_id(values, self.player2_id)
                if not p2_row:
                    await self.cog._post_in_origin_channel(self.origin_channel_id, "🚫 Sub auto-rejected: player subbing in is no longer in the sheet.")
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (player2 not in sheet).")
                    try:
                        await interaction.followup.send("🚫 Auto-rejected: player2 not found in sheet.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                p2_team = self.cog._get_team_from_row(values, p2_row)
                if not _is_free_agent(p2_team):
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"🚫 Sub auto-rejected: {self.player2_display} is not a Free Agent (currently **{p2_team or 'Unknown'}**)."
                    )
                    await self._finalize_buttons(interaction, "🚫 Auto-rejected (player2 not Free Agent).")
                    try:
                        await interaction.followup.send("🚫 Auto-rejected: player2 is not Free Agent.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                team_role_id = _get_team_role_id(self.captain_team)
                if not team_role_id:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"❌ Sub approved by {approver.mention}, but TEAM_INFO has no role id for **{self.captain_team}**."
                    )
                    await self._finalize_buttons(interaction, "❌ Approved (missing TEAM_INFO role id).")
                    try:
                        await interaction.followup.send("❌ TEAM_INFO missing role id for that team.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                team_role = guild.get_role(team_role_id)
                if not team_role:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"❌ Sub approved by {approver.mention}, but team role not found in server (id={team_role_id})."
                    )
                    await self._finalize_buttons(interaction, "❌ Approved (team role not found).")
                    try:
                        await interaction.followup.send("❌ Team role not found in server.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                player1_member = guild.get_member(self.player1_id) or await guild.fetch_member(self.player1_id)
                player2_member = guild.get_member(self.player2_id) or await guild.fetch_member(self.player2_id)

                # Add role now
                try:
                    if team_role not in player2_member.roles:
                        await player2_member.add_roles(
                            team_role,
                            reason=f"/sub approved: temp add {self.captain_team} until {self.expires_at.isoformat()}"
                        )
                except discord.Forbidden:
                    await self.cog._post_in_origin_channel(
                        self.origin_channel_id,
                        f"✅ Sub approved by {approver.mention}, but ⚠️ bot cannot add roles (permission/hierarchy)."
                    )
                    await self._finalize_buttons(interaction, "✅ Approved (⚠️ role add failed).")
                    try:
                        await interaction.followup.send("✅ Approved, but ⚠️ role add failed (check perms/hierarchy).", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return

                # Persist + schedule removal
                expires_iso = self.expires_at.isoformat()
                key = self.cog._make_sub_key(guild.id, player2_member.id, team_role.id, expires_iso)

                record = {
                    "_key": key,
                    "guild_id": guild.id,
                    "user_id": player2_member.id,
                    "role_id": team_role.id,
                    "expires_at": expires_iso,
                    # ✅ extra fields for better changelog + audits
                    "team_name": self.captain_team,
                    "captain_id": self.captain_id,
                    "player1_id": self.player1_id,
                    "player2_id": self.player2_id,
                }
                await self.cog._add_sub_record(record)
                self.cog._schedule_removal(guild.id, player2_member.id, team_role.id, self.expires_at, key, record)

                # Log + origin
                try:
                    await self.cog._post_transaction_log(self.captain_team, player1_member, player2_member)
                except Exception as e:
                    logger.error("Sub transaction log failed: %r", e)
                    traceback.print_exc()

                await self.cog._post_in_origin_channel(
                    self.origin_channel_id,
                    f"✅ Transaction approved by {approver.mention}. {player2_member.mention} has been subbed in for {player1_member.mention} until **Sunday 11:59pm ET**."
                )

                await self._finalize_buttons(
                    interaction,
                    f"✅ Approved by {approver.mention} — {player2_member.mention} subbing for {player1_member.mention} (expires Sunday 11:59pm ET)"
                )

                try:
                    await interaction.followup.send("✅ Approved.", ephemeral=True)
                except discord.HTTPException:
                    pass

            except Exception as e:
                logger.error("Approve failed: %r", e)
                traceback.print_exc()
                await self.cog._post_in_origin_channel(self.origin_channel_id, "❌ Sub approval failed due to an internal error (see bot console).")
                await self._finalize_buttons(interaction, "❌ Failed (internal error).")
                try:
                    await interaction.followup.send("❌ Error while approving. Check console.", ephemeral=True)
                except discord.HTTPException:
                    pass

        @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
        async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.decided = True
            approver = interaction.user

            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

            await self.cog._post_in_origin_channel(self.origin_channel_id, f"🚫 Transaction rejected by {approver.mention}.")
            await self._finalize_buttons(interaction, f"🚫 Rejected by {approver.mention}")

            try:
                await interaction.followup.send("🚫 Rejected.", ephemeral=True)
            except discord.HTTPException:
                pass

    # ---------------------------
    # /sub command
    # ---------------------------
    @app_commands.command(
        name="sub",
        description="Request to sign a Free Agent on a temporary sub deal until Sunday 11:59pm ET (Admin Approval)."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player1="Player being subbed (must be on your team)",
        player2="Player subbing in (must be a Free Agent)"
    )
    async def sub(self, interaction: Interaction, player1: discord.Member, player2: discord.Member):
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

            # Avoid nonsense
            if player1.id == player2.id:
                await interaction.followup.send("🚫 player1 and player2 cannot be the same person.", ephemeral=True)
                return

            # ✅ Restriction: player2 cannot already have an active sub deal
            step = "ACTIVE_SUB_CHECK"
            active = await self._find_active_sub_for_user(interaction.guild.id, player2.id)  # type: ignore
            if active:
                await interaction.followup.send(
                    f"🚫 {player2.mention} is already signed on a sub deal and cannot be subbed for another team.",
                    ephemeral=True
                )
                return

            # Determine expiration
            now_et = datetime.now(EASTERN)
            expires_at = _next_sunday_2359(now_et)

            # Open sheet + validate
            step = "OPEN_SHEET"
            ws = self._open_worksheet()

            step = "READ_ALL"
            values = ws.get_all_values()
            if not values:
                await interaction.followup.send("❌ Worksheet is empty.", ephemeral=True)
                return

            # Captain row + team
            step = "FIND_CAPTAIN_ROW"
            captain_row = self._find_row_index_by_discord_id(values, interaction.user.id)
            if not captain_row:
                await interaction.followup.send("❌ You (captain) are not found in the Google Sheet (Column A).", ephemeral=True)
                return

            captain_team = self._get_team_from_row(values, captain_row)
            if not captain_team:
                await interaction.followup.send("❌ Your team name is blank in Column D for your row in the Google Sheet.", ephemeral=True)
                return

            # Team role must exist
            step = "TEAM_ROLE_VALIDATE"
            team_role_id = _get_team_role_id(captain_team)
            if not team_role_id:
                await interaction.followup.send(f"❌ TEAM_INFO is missing a valid role `id` for your team: **{captain_team}**.", ephemeral=True)
                return

            # Player1 must be on captain team
            step = "FIND_PLAYER1_ROW"
            p1_row = self._find_row_index_by_discord_id(values, player1.id)
            if not p1_row:
                await interaction.followup.send(f"❌ `{player1.display_name}` is not found in the Google Sheet (Column A).", ephemeral=True)
                return

            p1_team = self._get_team_from_row(values, p1_row)
            step = "VALIDATE_PLAYER1_TEAM"
            if _normalize(p1_team) != _normalize(captain_team):
                await interaction.followup.send(
                    f"🚫 You can only sub in place of someone on your own team. {player1.mention} is currently on **{p1_team or 'Unknown'}**.",
                    ephemeral=True
                )
                return

            # Player2 must be Free Agent
            step = "FIND_PLAYER2_ROW"
            p2_row = self._find_row_index_by_discord_id(values, player2.id)
            if not p2_row:
                await interaction.followup.send(f"❌ `{player2.display_name}` is not found in the Google Sheet (Column A).", ephemeral=True)
                return

            p2_team = self._get_team_from_row(values, p2_row)
            step = "VALIDATE_PLAYER2_FREE_AGENT"
            if not _is_free_agent(p2_team):
                await interaction.followup.send(
                    f"🚫 {player2.mention} is not a Free Agent. They are currently on **{p2_team or 'Unknown'}**.",
                    ephemeral=True
                )
                return

            # Pending posts
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
                player1_id=player1.id,
                player1_display=player1.display_name,
                player2_id=player2.id,
                player2_display=player2.display_name,
                expires_at=expires_at
            )

            admins_role_mention = f"<@&{self.admins_role_id}>"

            await pending_channel.send(
                content=(
                    f"{admins_role_mention} **Pending Sub Request**\n"
                    f"Captain: {interaction.user.mention}\n"
                    f"Team (from sheet): **{captain_team}**\n"
                    f"Player being subbed: {player1.mention}\n"
                    f"Player subbing in: {player2.mention}\n"
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
