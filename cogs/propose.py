import json
import os
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

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

TWO_WEEKS = timedelta(days=14)


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


def _validate_unix_time(unix_time: int) -> Optional[str]:
    """
    Validates:
      - unix_time is plausibly epoch seconds
      - is in the future
      - is within 14 days from now (UTC)
    Returns an error string if invalid, otherwise None.
    """
    # Basic plausibility check (epoch seconds; typically 10 digits in modern dates)
    # Keep this permissive but block obviously wrong values.
    if unix_time < 1_000_000_000 or unix_time > 9_999_999_999:
        return "❌ Invalid Unix time. Please paste the **Unix seconds** value from hammertime.cyou/en (example: `1767813000`)."

    now = datetime.now(timezone.utc)
    proposed = datetime.fromtimestamp(unix_time, tz=timezone.utc)

    if proposed <= now:
        return "❌ That proposed time is in the past. Please choose a future time."

    if proposed > (now + TWO_WEEKS):
        return "❌ That proposed time is more than **2 weeks** from now. Please choose a time within the next **14 days**."

    return None


class ProposeConfirmView(discord.ui.View):
    """Confirmation buttons for a proposed time/date."""

    def __init__(self, unix_time: int, author: discord.Member):
        super().__init__(timeout=60)
        self.unix_time = unix_time
        self.author = author
        self.result: Optional[bool] = None

    @property
    def when_display(self) -> str:
        # Discord will render this nicely for each user’s locale.
        return f"<t:{self.unix_time}:F>"

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
            "when": self.when_display,          # keep for backward compatibility with any readers
            "unix_time": self.unix_time,        # store the raw epoch too (useful later)
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
            await interaction.followup.send(
                content="@Captains — A match time has been proposed."
            )

        embed = discord.Embed(
            title="📌 Proposed Match Time",
            description=f"**{interaction.user.mention}** proposed:\n{self.when_display}",
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
                # Try to show the intended category name if we can resolve it
                target_cat = interaction.guild.get_channel(SCHED_CATEGORY_ID)
                target_name = target_cat.name if target_cat else "the configured Scheduling category"
                return f"❌ This command can only be used in **{target_name}**."
        else:
            # Fallback to name check if ID not configured
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
    @app_commands.describe(unix_time="Use a Unix time from hammertime.cyou/en to propose a time")
    async def propose(self, interaction: Interaction, unix_time: int):
        if not await check_cooldown(interaction):
            return

        # Check location + permissions
        error = await self._check_permissions_and_location(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        # Validate unix time (future + within 14 days)
        unix_error = _validate_unix_time(unix_time)
        if unix_error:
            await interaction.response.send_message(unix_error, ephemeral=True)
            return

        # Show confirm/cancel buttons to proposer
        view = ProposeConfirmView(unix_time=unix_time, author=interaction.user)
        await interaction.response.send_message(
            f"📝 You entered: {view.when_display}\nPlease confirm your proposal:",
            view=view,
            ephemeral=True
        )

    @propose.error
    async def propose_error(self, interaction: Interaction, error):
        raise error


async def setup(bot):
    await bot.add_cog(Propose(bot))
    