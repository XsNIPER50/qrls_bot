import json
import os
from typing import Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from utils.team_info import TEAM_INFO
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))
SCHED_CATEGORY_ID = int(os.getenv("SCHED_CATEGORY_ID", 0))

DATA_DIR = "data"
PROPOSALS_FILE = os.path.join(DATA_DIR, "proposals.json")
SCHED_CATEGORY_NAME = "Scheduling Channel"
SCHED_RESULTS_CHANNEL = "💥・scheduling"
SCHEDULED_MATCHES_CHANNEL = "scheduled-matches"

ET_TZ = ZoneInfo("America/New_York")
TWO_WEEKS = timedelta(days=14)

DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")  # M/D or MM/DD
TIME_RE = re.compile(r"^\s*(1[0-2]|0?[1-9])(?:\:([0-5]\d))?\s*([ap]m)\s*$", re.IGNORECASE)


def load_proposals() -> dict:
    if not os.path.exists(PROPOSALS_FILE):
        return {}
    with open(PROPOSALS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_proposals(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROPOSALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def user_is_admin_or_captain(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    if CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID):
        return True
    return False


def parse_et_datetime(date_str: str, time_str: str) -> tuple[Optional[datetime], Optional[str]]:
    dm = DATE_RE.match(date_str or "")
    if not dm:
        return None, "❌ Invalid date format. Use **M/D** (examples: `1/12`, `12/3`)."

    month = int(dm.group(1))
    day = int(dm.group(2))
    if not (1 <= month <= 12):
        return None, "❌ Month must be between 1 and 12."
    if not (1 <= day <= 31):
        return None, "❌ Day must be between 1 and 31."

    tm = TIME_RE.match(time_str or "")
    if not tm:
        return None, "❌ Invalid time format. Use **H[:MM]am/pm** in **EST/ET** (examples: `8pm`, `8:00pm`, `11:15am`)."

    hour12 = int(tm.group(1))
    minute = int(tm.group(2) or "0")
    ampm = tm.group(3).lower()

    hour24 = hour12 % 12
    if ampm == "pm":
        hour24 += 12

    now_et = datetime.now(ET_TZ)
    year = now_et.year

    try:
        dt_et = datetime(year, month, day, hour24, minute, tzinfo=ET_TZ)
    except ValueError:
        return None, "❌ That date/time isn’t a valid calendar date."

    if dt_et <= now_et:
        return None, "❌ That time is in the past (EST/ET). Please choose a future time."

    if dt_et > (now_et + TWO_WEEKS):
        return None, "❌ That time is more than **2 weeks** from now. Please choose a time within the next **14 days**."

    return dt_et, None


def format_dt_et(dt_et: datetime) -> str:
    month = dt_et.month
    day = dt_et.day
    hour = dt_et.hour
    minute = dt_et.minute
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{month}/{day} {hour12}:{minute:02d}{ampm} ET"


class Confirm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _check_permissions_and_location(self, interaction: Interaction) -> Optional[str]:
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return "❌ This command must be used in a text channel."

        category = interaction.channel.category
        if SCHED_CATEGORY_ID:
            if not category or category.id != SCHED_CATEGORY_ID:
                target_cat = interaction.guild.get_channel(SCHED_CATEGORY_ID)
                target_name = target_cat.name if target_cat else "the configured Scheduling category"
                return f"❌ This command can only be used in **{target_name}**."
        else:
            if not category or category.name != SCHED_CATEGORY_NAME:
                return f"❌ This command can only be used in the **{SCHED_CATEGORY_NAME}** category."

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return "❌ Could not determine your member information."
        if not user_is_admin_or_captain(member):
            return "🚫 Only Admins or Captains can use this command."

        return None

    @app_commands.command(
        name="confirm",
        description="Confirm a previously proposed match time (Admins & Captains only)."
    )
    @app_commands.describe(
        date="Date in M/D format (example: 1/12 or 12/3)",
        time="Time in EST/ET (example: 8pm or 8:00pm)"
    )
    async def confirm(self, interaction: Interaction, date: str, time: str):
        await interaction.response.defer(ephemeral=False)

        error = await self._check_permissions_and_location(interaction)
        if error:
            await interaction.followup.send(error)
            return

        dt_et, parse_err = parse_et_datetime(date, time)
        if parse_err:
            await interaction.followup.send(parse_err)
            return

        proposals = load_proposals()
        ch_id = str(interaction.channel.id)

        if ch_id not in proposals:
            await interaction.followup.send("❌ No active proposal found in this channel.")
            return

        proposal = proposals[ch_id]
        proposer_id = proposal.get("proposer_id")

        proposed_iso = proposal.get("dt_iso")
        if not proposed_iso:
            # Backward-compat message (in case older records exist)
            await interaction.followup.send(
                "⚠️ This channel’s proposal is missing the stored time format. Please re-run **/propose**."
            )
            return

        try:
            proposed_dt = datetime.fromisoformat(proposed_iso)
        except Exception:
            await interaction.followup.send("⚠️ Proposal time data is corrupted. Please re-run **/propose**.")
            return

        # Ensure timezone (should be ET, but be safe)
        if proposed_dt.tzinfo is None:
            proposed_dt = proposed_dt.replace(tzinfo=ET_TZ)

        if proposed_dt != dt_et:
            await interaction.followup.send("⚠️ The date/time you entered does not match the current proposal.")
            return

        if interaction.user.id == proposer_id:
            await interaction.followup.send("🚫 You cannot confirm your own proposal.")
            return

        proposer = interaction.guild.get_member(proposer_id) if proposer_id else None
        proposer_mention = proposer.mention if proposer else (f"<@{proposer_id}>" if proposer_id else "Unknown proposer")

        # --- Extract rough team names from channel and map to real TEAM_INFO keys ---
        parts = interaction.channel.name.split("-vs-")
        raw_a = parts[0].split("-", 1)[-1].replace("-", " ").title()
        raw_b = parts[1].replace("-", " ").title()

        def resolve_team_name(raw):
            for key in TEAM_INFO.keys():
                if raw.lower() in key.lower():
                    return key
            return raw

        team_a = resolve_team_name(raw_a)
        team_b = resolve_team_name(raw_b)

        # --- Determine confirmer's role (Admin or Captain) ---
        if interaction.user.guild_permissions.administrator:
            role_label = "Admin"
        elif CAPTAINS_ROLE_ID and discord.utils.get(interaction.user.roles, id=CAPTAINS_ROLE_ID):
            role_label = "Captain"
        else:
            role_label = "Member"

        display = format_dt_et(dt_et)

        embed = discord.Embed(
            title="✅ Match Time Confirmed",
            description=(
                f"**{display}** was proposed by {proposer_mention} "
                f"and confirmed by {interaction.user.mention}."
            ),
            color=discord.Color.green()
        )
        embed.add_field(name="🏆 Matchup", value=f"**{team_a}** vs **{team_b}**", inline=False)
        embed.add_field(name="🕒 Time", value=f"{display}", inline=True)
        embed.set_footer(text=f"Confirmed by {interaction.user.display_name} ({role_label})")

        # ✅ Ping captains OUTSIDE the embed so the role actually pings
        allowed_mentions = discord.AllowedMentions(roles=True, users=True, everyone=False)

        captains_role = interaction.guild.get_role(CAPTAINS_ROLE_ID) if (interaction.guild and CAPTAINS_ROLE_ID) else None
        if captains_role:
            await interaction.followup.send(
                content=f"{captains_role.mention} — A match time has been confirmed.",
                allowed_mentions=allowed_mentions,
                ephemeral=False
            )
        else:
            await interaction.followup.send(
                content="@Captains — A match time has been confirmed.",
                ephemeral=False
            )

        await interaction.followup.send(embed=embed, allowed_mentions=allowed_mentions, ephemeral=False)

        # --- Post to both #💥・scheduling and #scheduled-matches ---
        sched_channel = discord.utils.get(interaction.guild.text_channels, name=SCHED_RESULTS_CHANNEL)
        scheduled_matches_channel = discord.utils.get(interaction.guild.text_channels, name=SCHEDULED_MATCHES_CHANNEL)

        role_a = discord.utils.get(interaction.guild.roles, name=team_a)
        role_b = discord.utils.get(interaction.guild.roles, name=team_b)
        team_a_mention = role_a.mention if role_a else f"@{team_a}"
        team_b_mention = role_b.mention if role_b else f"@{team_b}"

        emoji_a_name = TEAM_INFO.get(team_a, {}).get("emoji", "")
        emoji_b_name = TEAM_INFO.get(team_b, {}).get("emoji", "")

        emoji_a_obj = discord.utils.get(interaction.guild.emojis, name=emoji_a_name)
        emoji_b_obj = discord.utils.get(interaction.guild.emojis, name=emoji_b_name)

        emoji_a_str = str(emoji_a_obj) if emoji_a_obj else (f":{emoji_a_name}:" if emoji_a_name else "")
        emoji_b_str = str(emoji_b_obj) if emoji_b_obj else (f":{emoji_b_name}:" if emoji_b_name else "")

        msg = f"{emoji_a_str} {team_a_mention} vs {team_b_mention} {emoji_b_str} — {display}"

        for channel in (sched_channel, scheduled_matches_channel):
            if channel:
                sent_message = await channel.send(msg, allowed_mentions=allowed_mentions)

                if channel.name == SCHEDULED_MATCHES_CHANNEL:
                    try:
                        await sent_message.add_reaction("🎙️")
                        await sent_message.add_reaction("🎥")
                    except Exception as e:
                        print(f"⚠️ Failed to add reactions in {channel.name}: {e}")

        # --- Cleanup proposal record ---
        del proposals[ch_id]
        save_proposals(proposals)

    @confirm.error
    async def confirm_error(self, interaction: Interaction, error):
        raise error


async def setup(bot):
    await bot.add_cog(Confirm(bot))
