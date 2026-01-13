import discord
from discord import app_commands, Interaction
from discord.ext import commands
import csv
import os
import logging
import traceback

from utils.team_info import TEAM_INFO
from utils.global_cooldown import check_cooldown

CSV_FILE = "data/salaries.csv"

DEFAULT_COLOR = 0x7289DA  # Discord blurple
DEFAULT_LOGO = "https://example.com/logos/default_team.png"  # fallback logo

logger = logging.getLogger("qrls.teaminfo")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


async def team_name_autocomplete(interaction: Interaction, current: str):
    """Shows up to 25 teams from TEAM_INFO whose name contains the typed text."""
    current_lower = (current or "").lower()
    choices = []

    for team in TEAM_INFO.keys():
        if current_lower in team.lower():
            choices.append(app_commands.Choice(name=team, value=team))
        if len(choices) >= 25:
            break

    if not current and not choices:
        for team in list(TEAM_INFO.keys())[:25]:
            choices.append(app_commands.Choice(name=team, value=team))

    return choices


async def _send(interaction: Interaction, content: str = None, *, embed: discord.Embed = None, ephemeral: bool = False):
    """
    Safely send a message whether the interaction was already responded to or not.
    Prevents 'InteractionResponded' errors (common when a cooldown helper responds/defers).
    """
    if interaction.response.is_done():
        return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    return await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)


def _safe_color(value) -> int:
    """
    TEAM_INFO color sometimes ends up as a string; normalize it to an int.
    Discord expects 0 <= color <= 0xFFFFFF.
    """
    try:
        if isinstance(value, int):
            c = value
        elif isinstance(value, str):
            v = value.strip().lower()
            if v.startswith("0x"):
                c = int(v, 16)
            else:
                c = int(v)
        else:
            return DEFAULT_COLOR

        if 0 <= c <= 0xFFFFFF:
            return c
        return DEFAULT_COLOR
    except Exception:
        return DEFAULT_COLOR


class TeamInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="teaminfo",
        description="Display information about a specific team."
    )
    @app_commands.describe(team_name="Select a team")
    @app_commands.autocomplete(team_name=team_name_autocomplete)
    async def teaminfo(self, interaction: discord.Interaction, team_name: str):
        step = "START"
        try:
            step = "COOLDOWN_CHECK"
            if not await check_cooldown(interaction):
                return

            step = "FILE_EXISTS"
            if not os.path.exists(CSV_FILE):
                await _send(interaction, "❌ Salary data file not found.", ephemeral=True)
                return

            step = "TEAM_LOOKUP"
            team_info = TEAM_INFO.get(team_name)
            if not team_info:
                await _send(interaction, f"❌ No data found for team **{team_name}**.", ephemeral=True)
                return

            step = "TEAM_FIELDS"
            color = _safe_color(team_info.get("color", DEFAULT_COLOR))
            logo = team_info.get("logo", DEFAULT_LOGO) or DEFAULT_LOGO

            step = "READ_CSV"
            players = []
            with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("team", "") or "").strip().lower() == team_name.lower():
                        nickname = row.get("nickname", "Unknown")
                        salary = row.get("salary", "0")
                        players.append(f"• **{nickname}** — {salary}")

            players_list = "\n".join(players) if players else "No players currently assigned to this team."

            step = "BUILD_EMBED"
            embed = discord.Embed(
                title=f"🏆 {team_name}",
                description=players_list,
                color=color
            )
            embed.set_thumbnail(url=logo)
            embed.set_author(name=team_name, icon_url=logo)
            embed.set_footer(text="QRLS Team Information")

            step = "SEND"
            await _send(interaction, embed=embed)

        except Exception as e:
            logger.error("ERROR in /teaminfo at step=%s: %r", step, e)
            traceback.print_exc()
            # If the interaction is already responded to, followup; otherwise response
            try:
                await _send(interaction, f"❌ /teaminfo failed at **{step}**. Check bot console.", ephemeral=True)
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(TeamInfo(bot))
