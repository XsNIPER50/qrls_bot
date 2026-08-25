import json
import os
from datetime import datetime
from typing import Optional

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from utils.runtime_data import atomic_json_dump
from utils.scheduling import format_schedule_datetime, parse_schedule_datetime, series_id_from_topic, week_from_channel_name
from utils.website_schedule import ScheduleAPIError, WebsiteScheduleClient

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))
SCHED_CATEGORY_ID = int(os.getenv("SCHED_CATEGORY_ID", 0))
PROPOSALS_FILE = os.path.join("data", "proposals.json")
SCHED_CATEGORY_NAME = "Scheduling Channel"

try:
    from utils.global_cooldown import check_cooldown
except Exception:
    async def check_cooldown(interaction: Interaction) -> bool:
        return True


def load_proposals() -> dict:
    if not os.path.exists(PROPOSALS_FILE):
        return {}
    with open(PROPOSALS_FILE, "r", encoding="utf-8") as file:
        try:
            return json.load(file)
        except json.JSONDecodeError:
            return {}


def save_proposals(data: dict) -> None:
    atomic_json_dump(PROPOSALS_FILE, data, indent=2)


def user_is_admin_or_captain(member: discord.Member) -> bool:
    return bool(
        member.guild_permissions.administrator
        or (ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID))
        or (CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID))
    )


def api_error_message(error: ScheduleAPIError) -> str:
    if error.is_conflict:
        return "⚠️ This matchup changed on the website. Run **/propose** again."
    return f"❌ The website could not save this proposal: {error.message}"


class ProposalApprovalView(discord.ui.View):
    def __init__(self, *, client, series, timezone_name, scheduled_at, display, author):
        super().__init__(timeout=60 * 60 * 24)
        self.client = client
        self.series = series
        self.timezone_name = timezone_name
        self.scheduled_at = scheduled_at
        self.display = display
        self.author = author

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message(
            "🚫 Only the user who started this proposal can use these buttons.", ephemeral=True
        )
        return False

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def approve(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            updated = await self.client.update_time(
                "propose",
                self.series["id"],
                self.scheduled_at,
                interaction.user.id,
                self.series["version"],
            )
        except ScheduleAPIError as error:
            await interaction.followup.send(api_error_message(error), ephemeral=True)
            return

        proposals = load_proposals()
        proposals[str(interaction.channel.id)] = {
            "dt_iso": self.scheduled_at.isoformat(),
            "display": self.display,
            "proposer_id": interaction.user.id,
            "series_id": self.series["id"],
            "version": updated["version"],
            "timezone": self.timezone_name,
            "team_one_name": self.series["team_one_name"],
            "team_two_name": self.series["team_two_name"],
        }
        save_proposals(proposals)

        allowed = discord.AllowedMentions(roles=True, users=True, everyone=False)
        role = interaction.guild.get_role(CAPTAINS_ROLE_ID) if interaction.guild and CAPTAINS_ROLE_ID else None
        await interaction.followup.send(
            content=f"{role.mention} — A match time has been proposed." if role else "@Captains — A match time has been proposed.",
            allowed_mentions=allowed,
        )
        embed = discord.Embed(
            title="📌 Proposed Match Time",
            description=f"**{interaction.user.mention}** proposed:\n**{self.display}**",
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, allowed_mentions=allowed)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("❎ Proposal cancelled.", ephemeral=True)
        self.stop()


class Propose(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.website = WebsiteScheduleClient()

    async def _check_permissions_and_location(self, interaction: Interaction) -> Optional[str]:
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return "❌ This command must be used in a text channel."
        category = interaction.channel.category
        if SCHED_CATEGORY_ID:
            if not category or category.id != SCHED_CATEGORY_ID:
                target = interaction.guild.get_channel(SCHED_CATEGORY_ID)
                target_name = target.name if target else "the configured Scheduling category"
                return f"❌ This command can only be used in **{target_name}**."
        elif not category or category.name != SCHED_CATEGORY_NAME:
            return f"❌ This command can only be used in the **{SCHED_CATEGORY_NAME}** category."
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return "❌ Could not determine your member information."
        if not user_is_admin_or_captain(member):
            return "🚫 Only Admins or Captains can use this command."
        return None

    @app_commands.command(name="propose", description="Propose a match time in this scheduling channel (Admins & Captains only).")
    @app_commands.describe(date="Date in M/D format (example: 1/12 or 12/3)", time="Time in ET (example: 8pm or 8:00pm)")
    async def propose(self, interaction: Interaction, date: str, time: str):
        if not await check_cooldown(interaction):
            return
        permission_error = await self._check_permissions_and_location(interaction)
        if permission_error:
            await interaction.response.send_message(permission_error, ephemeral=True)
            return

        series_id = series_id_from_topic(interaction.channel.topic)
        week_number = week_from_channel_name(interaction.channel.name)
        if not series_id or week_number is None:
            await interaction.response.send_message(
                "❌ This channel is not linked to a website series. Ask an admin to rerun **/startweek**.",
                ephemeral=True,
            )
            return

        try:
            schedule = await self.website.get_week(week_number)
            series = next((item for item in schedule["series"] if str(item.get("id", "")).lower() == series_id), None)
            if not series:
                raise ScheduleAPIError("The linked series was not found in this website week.")
            timezone_name = schedule["week"].get("timezone") or "America/New_York"
            scheduled_at, parse_error = parse_schedule_datetime(date, time, timezone_name)
        except (ScheduleAPIError, ValueError) as error:
            await interaction.response.send_message(f"❌ Could not load this matchup from the website: {error}", ephemeral=True)
            return
        if parse_error:
            await interaction.response.send_message(parse_error, ephemeral=True)
            return

        display = format_schedule_datetime(scheduled_at)
        await interaction.response.send_message(
            f"📝 You entered: **{display}**\nPlease confirm your proposal:",
            view=ProposalApprovalView(
                client=self.website,
                series=series,
                timezone_name=timezone_name,
                scheduled_at=scheduled_at,
                display=display,
                author=interaction.user,
            ),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Propose(bot))
