import json
import os
from datetime import datetime
from typing import Optional

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from utils.runtime_data import atomic_json_dump
from utils.schedule_announcements import deliver_confirmed_announcement
from utils.scheduling import parse_schedule_datetime, series_id_from_topic
from utils.website_schedule import ScheduleAPIError, WebsiteScheduleClient

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))
SCHED_CATEGORY_ID = int(os.getenv("SCHED_CATEGORY_ID", 0))
PROPOSALS_FILE = os.path.join("data", "proposals.json")
SCHED_CATEGORY_NAME = "Scheduling Channel"


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


class Confirm(commands.Cog):
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

    @app_commands.command(name="confirm", description="Confirm a previously proposed match time (Admins & Captains only).")
    @app_commands.describe(date="Date in M/D format (example: 1/12 or 12/3)", time="Time in ET (example: 8pm or 8:00pm)")
    async def confirm(self, interaction: Interaction, date: str, time: str):
        await interaction.response.defer(ephemeral=False)
        permission_error = await self._check_permissions_and_location(interaction)
        if permission_error:
            await interaction.followup.send(permission_error)
            return

        proposals = load_proposals()
        channel_id = str(interaction.channel.id)
        proposal = proposals.get(channel_id)
        if not isinstance(proposal, dict):
            await interaction.followup.send("❌ No active proposal found in this channel.")
            return

        required = {"dt_iso", "proposer_id", "series_id", "version", "timezone", "team_one_name", "team_two_name"}
        if not required.issubset(proposal):
            await interaction.followup.send("⚠️ This is a legacy proposal. Please rerun **/propose** to sync it with the website.")
            return
        if series_id_from_topic(interaction.channel.topic) != str(proposal["series_id"]).lower():
            await interaction.followup.send("⚠️ This channel’s website series changed. Please rerun **/propose**.")
            return
        if interaction.user.id == proposal["proposer_id"]:
            await interaction.followup.send("🚫 You cannot confirm your own proposal.")
            return

        try:
            entered_at, parse_error = parse_schedule_datetime(date, time, proposal["timezone"])
        except ValueError as error:
            await interaction.followup.send(f"❌ Website timezone error: {error}")
            return
        if parse_error:
            await interaction.followup.send(parse_error)
            return
        try:
            proposed_at = datetime.fromisoformat(proposal["dt_iso"])
        except (TypeError, ValueError):
            await interaction.followup.send("⚠️ Proposal time data is corrupted. Please rerun **/propose**.")
            return
        if entered_at != proposed_at:
            await interaction.followup.send("⚠️ The date/time you entered does not match the current proposal.")
            return

        try:
            await self.website.update_time(
                "confirm",
                proposal["series_id"],
                proposed_at,
                interaction.user.id,
                proposal["version"],
            )
        except ScheduleAPIError as error:
            if error.is_conflict:
                await interaction.followup.send("⚠️ This matchup changed on the website. Please rerun **/propose**.")
            else:
                await interaction.followup.send(f"❌ The website could not confirm this match: {error.message}")
            return

        proposer_id = proposal["proposer_id"]
        team_a, team_b = proposal["team_one_name"], proposal["team_two_name"]
        await deliver_confirmed_announcement(
            interaction.channel,
            actor_id=interaction.user.id,
            proposer_id=proposer_id,
            scheduled_at=proposed_at,
            team_a=team_a,
            team_b=team_b,
            captains_role_id=CAPTAINS_ROLE_ID,
            strict_delivery=False,
        )

        del proposals[channel_id]
        save_proposals(proposals)


async def setup(bot):
    await bot.add_cog(Confirm(bot))
