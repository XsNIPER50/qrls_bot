"""Background delivery of schedule announcements created by website admins."""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from utils.schedule_announcements import deliver_confirmed_announcement, deliver_proposed_announcement
from utils.website_schedule import ScheduleAPIError, WebsiteScheduleClient

CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))
logger = logging.getLogger("qrls.schedule_notifications")


def poll_seconds() -> float:
    try:
        return max(2.0, float(os.getenv("QRLS_SCHEDULE_POLL_SECONDS", "10")))
    except ValueError:
        return 10.0


def required_text(notification: dict, key: str) -> str:
    value = notification.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Notification is missing {key}.")
    return value.strip()


class ScheduleNotifications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.website = WebsiteScheduleClient()
        self._pending_completion: str | None = None

    async def cog_load(self) -> None:
        self.poll_notifications.change_interval(seconds=poll_seconds())
        if not self.poll_notifications.is_running():
            self.poll_notifications.start()

    def cog_unload(self) -> None:
        # Cancellation also shuts down cleanly while before_loop is waiting for
        # Discord readiness. Any in-flight claim is recovered by the API lease.
        self.poll_notifications.cancel()

    async def _channel(self, channel_id: int) -> discord.TextChannel:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Linked Discord channel is not a text channel.")
        return channel

    async def deliver(self, notification: dict) -> None:
        event_type = required_text(notification, "event_type")
        actor_id = int(required_text(notification, "actor_discord_user_id"))
        channel_id = int(required_text(notification, "discord_channel_id"))
        scheduled_at = datetime.fromisoformat(required_text(notification, "scheduled_at").replace("Z", "+00:00"))
        timezone = ZoneInfo(required_text(notification, "timezone"))
        scheduled_at = scheduled_at.astimezone(timezone)
        channel = await self._channel(channel_id)

        if event_type == "proposed":
            await deliver_proposed_announcement(
                channel,
                actor_id=actor_id,
                scheduled_at=scheduled_at,
                captains_role_id=CAPTAINS_ROLE_ID,
            )
            return
        if event_type == "confirmed":
            proposer_value = notification.get("proposer_discord_user_id")
            proposer_id = int(proposer_value) if proposer_value else None
            await deliver_confirmed_announcement(
                channel,
                actor_id=actor_id,
                proposer_id=proposer_id,
                scheduled_at=scheduled_at,
                team_a=required_text(notification, "team_one_name"),
                team_b=required_text(notification, "team_two_name"),
                captains_role_id=CAPTAINS_ROLE_ID,
            )
            return
        raise ValueError(f"Unsupported schedule notification event: {event_type!r}.")

    async def poll_once(self) -> None:
        # If Discord delivery succeeded but acknowledgement failed, retry only the
        # acknowledgement so the same process does not duplicate announcements.
        if self._pending_completion:
            await self.website.complete_notification(self._pending_completion)
            logger.info("Acknowledged schedule notification %s", self._pending_completion)
            self._pending_completion = None
            return

        notification = await self.website.claim_notification()
        if notification is None:
            return
        notification_id = required_text(notification, "id")
        try:
            await self.deliver(notification)
        except Exception as error:
            safe_error = f"{type(error).__name__}: {error}"
            logger.exception("Schedule notification %s delivery failed", notification_id)
            try:
                await self.website.fail_notification(notification_id, safe_error)
            except ScheduleAPIError:
                logger.exception("Could not report schedule notification %s failure", notification_id)
            return

        self._pending_completion = notification_id
        await self.website.complete_notification(notification_id)
        logger.info("Delivered and acknowledged schedule notification %s", notification_id)
        self._pending_completion = None

    @tasks.loop(seconds=10.0)
    async def poll_notifications(self) -> None:
        try:
            await self.poll_once()
        except Exception:
            # Never let a transient API failure terminate the background loop.
            logger.exception("Schedule notification poll failed")

    @poll_notifications.before_loop
    async def before_poll_notifications(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleNotifications(bot))
