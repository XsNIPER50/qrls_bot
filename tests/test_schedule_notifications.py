import asyncio
import unittest
from unittest.mock import AsyncMock

from cogs.schedule_notifications import ScheduleNotifications

NOTIFICATION_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeBot:
    async def wait_until_ready(self):
        await asyncio.Event().wait()


class FakeWebsite:
    def __init__(self, notification):
        self.notification = notification
        self.claim_notification = AsyncMock(side_effect=self._claim)
        self.complete_notification = AsyncMock()
        self.fail_notification = AsyncMock()

    async def _claim(self):
        value, self.notification = self.notification, None
        return value


class ScheduleNotificationPollerTest(unittest.IsolatedAsyncioTestCase):
    def notification(self):
        return {"id": NOTIFICATION_ID, "event_type": "proposed"}

    async def test_acknowledges_only_after_delivery(self):
        cog = ScheduleNotifications(FakeBot())
        cog.website = FakeWebsite(self.notification())
        events = []

        async def delivered(notification):
            events.append("delivered")

        async def completed(notification_id):
            events.append(f"completed:{notification_id}")

        cog.deliver = delivered
        cog.website.complete_notification.side_effect = completed
        await cog.poll_once()

        self.assertEqual(events, ["delivered", f"completed:{NOTIFICATION_ID}"])
        cog.website.fail_notification.assert_not_awaited()
        self.assertIsNone(cog._pending_completion)

    async def test_lifecycle_starts_once_and_cancels_before_ready(self):
        cog = ScheduleNotifications(FakeBot())
        await cog.cog_load()
        first_task = cog.poll_notifications.get_task()
        await cog.cog_load()
        self.assertIs(cog.poll_notifications.get_task(), first_task)
        self.assertTrue(cog.poll_notifications.is_running())

        cog.cog_unload()
        await asyncio.sleep(0)
        self.assertTrue(first_task.cancelled() or first_task.done())

    async def test_reports_delivery_failure_without_acknowledging(self):
        cog = ScheduleNotifications(FakeBot())
        cog.website = FakeWebsite(self.notification())
        cog.deliver = AsyncMock(side_effect=RuntimeError("Discord send failed"))

        await cog.poll_once()

        cog.website.complete_notification.assert_not_awaited()
        cog.website.fail_notification.assert_awaited_once()
        args = cog.website.fail_notification.await_args.args
        self.assertEqual(args[0], NOTIFICATION_ID)
        self.assertIn("Discord send failed", args[1])

    async def test_retries_pending_ack_without_claiming_or_redelivering(self):
        cog = ScheduleNotifications(FakeBot())
        cog.website = FakeWebsite(self.notification())
        cog.deliver = AsyncMock()
        cog._pending_completion = NOTIFICATION_ID

        await cog.poll_once()

        cog.website.complete_notification.assert_awaited_once_with(NOTIFICATION_ID)
        cog.website.claim_notification.assert_not_awaited()
        cog.deliver.assert_not_awaited()
        self.assertIsNone(cog._pending_completion)

    async def test_failed_ack_remains_pending(self):
        cog = ScheduleNotifications(FakeBot())
        cog.website = FakeWebsite(self.notification())
        cog.website.complete_notification.side_effect = RuntimeError("temporary API failure")
        cog._pending_completion = NOTIFICATION_ID

        with self.assertRaises(RuntimeError):
            await cog.poll_once()

        self.assertEqual(cog._pending_completion, NOTIFICATION_ID)
        cog.website.claim_notification.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
