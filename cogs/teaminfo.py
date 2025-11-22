import os
import csv
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from utils.team_info import TEAM_INFO
from utils.global_cooldown import check_cooldown

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

CSV_FILE = "data/salaries.csv"

DEFAULT_COLOR = 0x7289DA  # Discord blurple
DEFAULT_LOGO = "https://example.com/logos/default_team.png"  # fallback logo


class TeamInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="teaminfo", description="Display information about a specific team.")
    @app_commands.describe(team_name="Enter the team's name (case-sensitive to CSV data)")
    async def teaminfo(self, interaction: discord.Interaction, team_name: str):
        if not await check_cooldown(interaction):
            return

        # ✅ Ensure salary file exists
        if not os.path.exists(CSV_FILE):
            await interaction.response.send_message("❌ Salary data file not found.", ephemeral=True)
            return

        # --- Load team details ---
        team_info = TEAM_INFO.get(team_name, None)
        if not team_info:
            await interaction.response.send_message(f"❌ No data found for team **{team_name}**.", ephemeral=True)
            return

        color = team_info.get("color", DEFAULT_COLOR)
        logo = team_info.get("logo", DEFAULT_LOGO)

        # --- Find all players in that team ---
        players = []
        with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("team", "").strip().lower() == team_name.lower():
                    nickname = row.get("nickname", "Unknown")
                    salary = row.get("salary", "0")
                    players.append(f"• **{nickname}** — {salary}")

        players_list = "\n".join(players) if players else "No players currently assigned to this team."

        # --- Build embed ---
        embed = discord.Embed(
            title=f"🏆 {team_name}",
            description=players_list,
            color=color
        )

        embed.set_thumbnail(url=logo)
        embed.set_author(name=team_name, icon_url=logo)
        embed.set_footer(text="QRLS Team Information")

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(TeamInfo(bot))
