import csv
import os
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

# Optional: global cooldown support
try:
    from utils.global_cooldown import check_cooldown
except Exception:
    async def check_cooldown(interaction: Interaction) -> bool:
        return True


CSV_FILE = "data/salaries.csv"


def user_is_admin_or_captain(member: discord.Member) -> bool:
    """Check if member is an Admin or Captain using .env role IDs."""
    if member.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    if CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID):
        return True
    return False


class Salary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="salary",
        description="Check your salary or another player's salary (Admins/Captains only for others)."
    )
    @app_commands.describe(
        member="Mention a player or enter their Discord ID to check their salary (Admins/Captains only)."
    )
    async def salary(
        self,
        interaction: Interaction,
        member: Optional[discord.Member] = None,
        discord_id: Optional[str] = None
    ):
        if not await check_cooldown(interaction):
            return

        # Determine target user
        target_id = None
        if member:
            target_id = str(member.id)
        elif discord_id:
            target_id = discord_id
        else:
            target_id = str(interaction.user.id)
            member = interaction.user

        # Permission check if viewing others
        if target_id != str(interaction.user.id):
            if not user_is_admin_or_captain(interaction.user):
                await interaction.response.send_message(
                    "🚫 You don’t have permission to view other players’ salaries.",
                    ephemeral=True
                )
                return

        # Check salary file
        if not os.path.exists(CSV_FILE):
            await interaction.response.send_message("❌ Salary data file not found.", ephemeral=True)
            return

        player_data = None
        with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["discord_id"] == target_id:
                    player_data = row
                    break

        if not player_data:
            await interaction.response.send_message(
                f"❌ No salary data found for <@{target_id}>.",
                ephemeral=True
            )
            return

        nickname = player_data.get("nickname", "Unknown")
        salary = player_data.get("salary", "0")
        team = player_data.get("team", "Unassigned")

        embed = discord.Embed(
            title="💰 Salary Information",
            description=f"**{nickname}** — *{team}*",
            color=discord.Color.gold()
        )
        embed.add_field(name="💼 Discord ID", value=f"`{target_id}`", inline=False)
        embed.add_field(name="💵 Salary", value=f"${salary}", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Salary(bot))
