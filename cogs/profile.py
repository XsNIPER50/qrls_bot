import discord
from discord import app_commands
from discord.ext import commands
import csv
import os
from typing import Optional
from dotenv import load_dotenv

from utils.team_info import TEAM_INFO  # ✅ centralized team data
from utils.global_cooldown import check_cooldown

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

CSV_FILE = "data/salaries.csv"

DEFAULT_COLOR = 0x7289DA  # Discord blurple
DEFAULT_LOGO = "https://example.com/logos/default_team.png"  # fallback


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="profile",
        description="View your profile or another player's card (Admins only)."
    )
    @app_commands.describe(member="Mention a player to view their profile (Admin only)")
    async def profile(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        if not await check_cooldown(interaction):
            return

        # --- If no player given, show caller’s own profile ---
        if member is None:
            member = interaction.user
        else:
            # --- Only Admins (by role ID or permissions) can view other players’ profiles ---
            if not (
                interaction.user.guild_permissions.administrator
                or (ADMINS_ROLE_ID and discord.utils.get(interaction.user.roles, id=ADMINS_ROLE_ID))
            ):
                await interaction.response.send_message(
                    "🚫 You don’t have permission to view other players’ profiles.",
                    ephemeral=True
                )
                return

        # --- Load salary file ---
        if not os.path.exists(CSV_FILE):
            await interaction.response.send_message("❌ Salary data file not found.", ephemeral=True)
            return

        player_data = None
        with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["discord_id"] == str(member.id):
                    player_data = row
                    break

        if not player_data:
            await interaction.response.send_message(f"❌ No data found for {member.mention}.", ephemeral=True)
            return

        nickname = player_data.get("nickname", member.display_name)
        salary = player_data.get("salary", "0")
        team = player_data.get("team", "Unassigned")

        # --- Pull color & logo from TEAM_INFO ---
        team_info = TEAM_INFO.get(team, {})
        color = team_info.get("color", DEFAULT_COLOR)
        logo = team_info.get("logo", DEFAULT_LOGO)

        # --- Build the embed ---
        embed = discord.Embed(
            title=f"{nickname}'s Player Profile",
            description=f"**Team:** {team}",
            color=color
        )

        embed.set_thumbnail(url=logo)
        embed.set_author(name=team, icon_url=logo)
        embed.add_field(name="💼 Discord ID", value=f"`{member.id}`", inline=False)
        embed.add_field(name="🎮 Nickname", value=nickname, inline=True)
        embed.add_field(name="💰 Salary", value=f"{salary}", inline=True)
        embed.set_footer(text="League Player Profile", icon_url=member.display_avatar.url)

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Profile(bot))
