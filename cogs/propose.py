import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# Optional: reuse cooldowns if desired
try:
    from utils.global_cooldown import check_cooldown
except Exception:
    async def check_cooldown(interaction: Interaction) -> bool:
        return True

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))
SCHED_CATEGORY_ID = int(os.getenv("SCHED_CATEGORY_ID", 0))  # 👈 category ID from .env

DATA_DIR = "data"
PROPOSALS_FILE = os.path.join(DATA_DIR, "proposals.json")

# Fallback name (only used if SCHED_CATEGORY_ID not provided)
SCHED_CATEGORY_NAME = "Scheduling Channel"

ET_TZ = ZoneInfo("America/New_York")
TWO_WEEKS = timedelta(days=14)

DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")  # M/D or MM/DD
TIME_RE = re.compile(r"^\s*(1[0-2]|0?[1-9])(?:\:([0-5]\d))?\s*([ap]m)\s*$", re.IGNORECASE)


def ensure_data_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(PROPOSALS_FILE):
        with open(PROPOSALS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def load_proposals() -> dict:
    ensure_data_file()
    with open(PROPOSALS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_proposals(data: dict):
    ensure_data_file()
    with open(PROPOSALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def user_is_admin_or_captain(member: discord.Member) -> bool:
    """Checks if the member is an Admin or Captain using .env role IDs."""
    if member.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    if CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID):
        return True
    return False


def parse_et_datetime(date_str: str, time_str: str) -> tuple[Optional[datetime], Optional[str]]:
    """
    Parses:
      date_str: M/D or MM/DD
      time_str: H(am/pm) or H:MM(am/pm)
    Returns (dt_et, error_message).
    """
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

    # Build ET datetime (no year provided; assume current year)
    try:
        dt_et = datetime(year, month, day, hour24, minute, tzinfo=ET_TZ)
    except ValueError:
        return None, "❌ That date/time isn’t a valid calendar date."

    # Reject past
    if dt_et <= now_et:
        return None, "❌ That proposed time is in the past (EST/ET). Please choose a future time."

    # Only within 14 days
    if dt_et > (now_et + TWO_WEEKS):
        return None, "❌ That proposed time is more than **2 weeks** from now. Please choose a time within the next **14 days**."

    return dt_et, None


def format_dt_et(dt_et: datetime) -> str:
    # Display: M/D h:mm AM/PM ET
    # (strftime on Windows may not support %-m / %-I; do it manually)
    month = dt_et.month
    day = dt_et.day
    hour = dt_et.hour
    minute = dt_et.minute
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{month}/{day} {hour12}:{minute:02d}{ampm} ET"


class ProposeConfirmView(discord.ui.View):
    """Confirmation buttons for a proposed time/date."""

    def __init__(self, dt_iso: str, display_text: str, author: discord.Member):
        super().__init__(timeout=60 * 60 * 24) # 24hrs
        self.dt_iso = dt_iso
        self.display_text = display_text
        self.author = author
        self.result: Optional[bool] = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "🚫 Only the user who started this proposal can use these buttons.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        proposals = load_proposals()
        proposals[str(interaction.channel.id)] = {
            "dt_iso": self.dt_iso,               # canonical
            "display": self.display_text,        # nice text
            "proposer_id": interaction.user.id
        }
        save_proposals(proposals)

        # ✅ Ping captains OUTSIDE the embed so the role actually pings
        allowed_mentions = discord.AllowedMentions(roles=True, users=True, everyone=False)

        captains_role = None
        if interaction.guild and CAPTAINS_ROLE_ID:
            captains_role = interaction.guild.get_role(CAPTAINS_ROLE_ID)

        if captains_role:
            await interaction.followup.send(
                content=f"{captains_role.mention} — A match time has been proposed.",
                allowed_mentions=allowed_mentions
            )
        else:
            await interaction.followup.send(content="@Captains — A match time has been proposed.")

        embed = discord.Embed(
            title="📌 Proposed Match Time",
            description=f"**{interaction.user.mention}** proposed:\n**{self.display_text}**",
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed, allowed_mentions=allowed_mentions)

        self.result = True
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("❎ Proposal cancelled.", ephemeral=True)
        self.result = False
        self.stop()


class Propose(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _check_permissions_and_location(self, interaction: Interaction) -> Optional[str]:
        # Must be in a text channel
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return "❌ This command must be used in a text channel."

        # Must be in the configured Scheduling category
        category = interaction.channel.category
        if SCHED_CATEGORY_ID:
            if not category or category.id != SCHED_CATEGORY_ID:
                target_cat = interaction.guild.get_channel(SCHED_CATEGORY_ID)
                target_name = target_cat.name if target_cat else "the configured Scheduling category"
                return f"❌ This command can only be used in **{target_name}**."
        else:
            if not category or category.name != SCHED_CATEGORY_NAME:
                return f"❌ This command can only be used in the **{SCHED_CATEGORY_NAME}** category."

        # Check roles
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return "❌ Could not determine your member information."
        if not user_is_admin_or_captain(member):
            return "🚫 Only Admins or Captains can use this command."

        return None

    @app_commands.command(
        name="propose",
        description="Propose a match time in this scheduling channel (Admins & Captains only)."
    )
    @app_commands.describe(
        date="Date in M/D format (example: 1/12 or 12/3)",
        time="Time in EST/ET (example: 8pm or 8:00pm)"
    )
    async def propose(self, interaction: Interaction, date: str, time: str):
        if not await check_cooldown(interaction):
            return

        error = await self._check_permissions_and_location(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        dt_et, parse_err = parse_et_datetime(date, time)
        if parse_err:
            await interaction.response.send_message(parse_err, ephemeral=True)
            return

        display = format_dt_et(dt_et)
        view = ProposeConfirmView(dt_iso=dt_et.isoformat(), display_text=display, author=interaction.user)

        await interaction.response.send_message(
            f"📝 You entered: **{display}**\nPlease confirm your proposal:",
            view=view,
            ephemeral=True
        )

    @propose.error
    async def propose_error(self, interaction: Interaction, error):
        raise error


async def setup(bot):
    await bot.add_cog(Propose(bot))
