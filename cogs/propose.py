import json
import os
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

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


class ProposeConfirmView(discord.ui.View):
    """Confirmation buttons for a proposed time/date."""

    def __init__(self, when_text: str, author: discord.Member):
        super().__init__(timeout=60)
        self.when_text = when_text
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
            "when": self.when_text,
            "proposer_id": interaction.user.id
        }
        save_proposals(proposals)

        embed = discord.Embed(
            title="📌 Proposed Match Time",
            description=f"**{interaction.user.mention}** proposed:\n`{self.when_text}`",
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed)

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
    @app_commands.describe(when="Enter a date/time, e.g. '10/25 8:00pm'")
    async def propose(self, interaction: Interaction, when: str):
        if not await check_cooldown(interaction):
            return

        # Check location + permissions
        error = await self._check_permissions_and_location(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        # Show confirm/cancel buttons to proposer
        view = ProposeConfirmView(when_text=when, author=interaction.user)
        await interaction.response.send_message(
            f"📝 You entered: `{when}`\nPlease confirm your proposal:",
            view=view,
            ephemeral=True
        )

    @propose.error
    async def propose_error(self, interaction: Interaction, error):
        raise error


async def setup(bot):
    await bot.add_cog(Propose(bot))
