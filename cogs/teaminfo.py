import discord
from discord import app_commands, Interaction
from discord.ext import commands
import csv
import os
from utils.team_info import TEAM_INFO
from utils.global_cooldown import check_cooldown

CSV_FILE = "data/salaries.csv"

DEFAULT_COLOR = 0x7289DA  # Discord blurple
DEFAULT_LOGO = "https://example.com/logos/default_team.png"  # fallback logo


async def team_name_autocomplete(
    interaction: Interaction,
    current: str,
):
    """
    Autocomplete callback for the team_name option.

    Shows up to 25 teams from TEAM_INFO whose name contains the typed text.
    """
    # Build list of matching team names
    current_lower = current.lower()
    choices = []

    for team in TEAM_INFO.keys():
        if current_lower in team.lower():
            choices.append(
                app_commands.Choice(name=team, value=team)
            )
        if len(choices) >= 25:  # Discord limit
            break

    # If user hasn't typed anything yet, just show first 25 teams
    if not current and not choices:
        for team in list(TEAM_INFO.keys())[:25]:
            choices.append(app_commands.Choice(name=team, value=team))

    return choices


class TeamInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="teaminfo",
        description="Display information about a specific team."
    )
    @app_commands.describe(team_name="Select a team")
    @app_commands.autocomplete(team_name=team_name_autocomplete)
    async def teaminfo(
        self,
        interaction: discord.Interaction,
        team_name: str
    ):
        if not await check_cooldown(interaction):
            return

        # ✅ Ensure salary file exists
        if not os.path.exists(CSV_FILE):
            await interaction.response.send_message(
                "❌ Salary data file not found.",
                ephemeral=True
            )
            return

        # --- Load team details ---
        team_info = TEAM_INFO.get(team_name, None)
        if not team_info:
            await interaction.response.send_message(
                f"❌ No data found for team **{team_name}**.",
                ephemeral=True
            )
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

        if not players:
            players_list = "No players currently assigned to this team."
        else:
            players_list = "\n".join(players)

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
