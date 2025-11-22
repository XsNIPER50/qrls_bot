import json
import os
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from utils.team_info import TEAM_INFO
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))
SCHED_CATEGORY_ID = int(os.getenv("SCHED_CATEGORY_ID", 0))  # 👈 Added category ID support

DATA_DIR = "data"
PROPOSALS_FILE = os.path.join(DATA_DIR, "proposals.json")
SCHED_CATEGORY_NAME = "Scheduling Channel"
SCHED_RESULTS_CHANNEL = "💥・scheduling"
SCHEDULED_MATCHES_CHANNEL = "scheduled-matches"


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
    """Check if a member is an Admin or Captain using role IDs from .env"""
    if member.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    if CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID):
        return True
    return False


class Confirm(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _check_permissions_and_location(self, interaction: Interaction) -> Optional[str]:
        """Ensures the command is used inside the proper Scheduling category."""
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return "❌ This command must be used in a text channel."

        category = interaction.channel.category
        if SCHED_CATEGORY_ID:
            if not category or category.id != SCHED_CATEGORY_ID:
                target_cat = interaction.guild.get_channel(SCHED_CATEGORY_ID)
                target_name = target_cat.name if target_cat else "the configured Scheduling category"
                return f"❌ This command can only be used in **{target_name}**."
        else:
            # fallback if ID not set
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
    @app_commands.describe(when="Enter the date/time exactly as proposed (e.g., '10/25 8:00pm')")
    async def confirm(self, interaction: Interaction, when: str):
        await interaction.response.defer(ephemeral=False)

        # --- Permission & category checks ---
        error = await self._check_permissions_and_location(interaction)
        if error:
            await interaction.followup.send(error)
            return

        proposals = load_proposals()
        ch_id = str(interaction.channel.id)

        if ch_id not in proposals:
            await interaction.followup.send("❌ No active proposal found in this channel.")
            return

        proposal = proposals[ch_id]
        proposed_when = proposal.get("when")
        proposer_id = proposal.get("proposer_id")

        if proposed_when != when:
            await interaction.followup.send("⚠️ The entered date/time does not match the current proposal.")
            return

        if interaction.user.id == proposer_id:
            await interaction.followup.send("🚫 You cannot confirm your own proposal.")
            return

        proposer = interaction.guild.get_member(proposer_id)
        proposer_mention = proposer.mention if proposer else f"<@{proposer_id}>"

        # --- Extract rough team names from channel and map to real TEAM_INFO keys ---
        parts = interaction.channel.name.split("-vs-")
        raw_a = parts[0].split("-", 1)[-1].replace("-", " ").title()
        raw_b = parts[1].replace("-", " ").title()

        def resolve_team_name(raw):
            """Return the correct TEAM_INFO key based on fuzzy match."""
            for key in TEAM_INFO.keys():
                if raw.lower() in key.lower():
                    return key
            return raw  # fallback

        team_a = resolve_team_name(raw_a)
        team_b = resolve_team_name(raw_b)

        # --- Determine confirmer's role (Admin or Captain) ---
        if interaction.user.guild_permissions.administrator:
            role_label = "Admin"
        elif CAPTAINS_ROLE_ID and discord.utils.get(interaction.user.roles, id=CAPTAINS_ROLE_ID):
            role_label = "Captain"
        else:
            role_label = "Member"

        # --- Build public embed ---
        embed = discord.Embed(
            title="✅ Match Time Confirmed",
            description=(
                f"**{proposed_when}** was proposed by {proposer_mention} "
                f"and confirmed by {interaction.user.mention}."
            ),
            color=discord.Color.green()
        )
        embed.add_field(name="🏆 Matchup", value=f"**{team_a}** vs **{team_b}**", inline=False)
        embed.add_field(name="🕒 Time", value=f"{proposed_when} EST", inline=True)
        embed.set_footer(text=f"Confirmed by {interaction.user.display_name} ({role_label})")

        await interaction.followup.send(embed=embed, ephemeral=False)

        # --- Post to both #💥・scheduling and #scheduled-matches ---
        sched_channel = discord.utils.get(interaction.guild.text_channels, name=SCHED_RESULTS_CHANNEL)
        scheduled_matches_channel = discord.utils.get(interaction.guild.text_channels, name=SCHEDULED_MATCHES_CHANNEL)

        # Get correct role mentions
        role_a = discord.utils.get(interaction.guild.roles, name=team_a)
        role_b = discord.utils.get(interaction.guild.roles, name=team_b)
        team_a_mention = role_a.mention if role_a else f"@{team_a}"
        team_b_mention = role_b.mention if role_b else f"@{team_b}"

        # Get emoji from TEAM_INFO
        emoji_a_name = TEAM_INFO.get(team_a, {}).get("emoji", "")
        emoji_b_name = TEAM_INFO.get(team_b, {}).get("emoji", "")

        emoji_a_obj = discord.utils.get(interaction.guild.emojis, name=emoji_a_name)
        emoji_b_obj = discord.utils.get(interaction.guild.emojis, name=emoji_b_name)

        emoji_a_str = str(emoji_a_obj) if emoji_a_obj else f":{emoji_a_name}:"
        emoji_b_str = str(emoji_b_obj) if emoji_b_obj else f":{emoji_b_name}:"

        msg = f"{emoji_a_str} {team_a_mention} vs {team_b_mention} {emoji_b_str} — {proposed_when} EST"

        for channel in (sched_channel, scheduled_matches_channel):
            if channel:
                sent_message = await channel.send(msg)

                # 👇 Add reactions only to the scheduled-matches channel
                if channel.name == SCHEDULED_MATCHES_CHANNEL:
                    try:
                        await sent_message.add_reaction("🎙️")  # :microphone2:
                        await sent_message.add_reaction("🎥")  # :movie_camera:
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
