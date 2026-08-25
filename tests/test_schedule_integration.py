import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from utils.scheduling import parse_schedule_datetime, series_id_from_topic, topic_with_series_id, week_from_channel_name
from utils.website_schedule import ScheduleAPIError, WebsiteScheduleClient

SERIES_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class SchedulingHelpersTest(unittest.TestCase):
    def test_topic_preserves_text_and_replaces_series_marker(self):
        topic = topic_with_series_id("Keep this\nQRLS series: 00000000-0000-4000-8000-000000000000", SERIES_ID)
        self.assertEqual(topic, f"Keep this\nQRLS series: {SERIES_ID}")
        self.assertEqual(series_id_from_topic(topic), SERIES_ID)

    def test_week_is_read_from_channel_name(self):
        self.assertEqual(week_from_channel_name("week12-team-a-vs-team-b"), 12)
        self.assertIsNone(week_from_channel_name("other-channel"))

    def test_parser_uses_timezone_and_rolls_into_next_year(self):
        now = datetime(2026, 12, 28, 12, tzinfo=ZoneInfo("America/New_York"))
        value, error = parse_schedule_datetime("1/2", "8:30pm", "America/New_York", now=now)
        self.assertIsNone(error)
        self.assertEqual(value.isoformat(), "2027-01-02T20:30:00-05:00")


class WebsiteScheduleClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "QRLS_WEBSITE_BASE_URL": "https://qrls.example/",
            "QRLS_BOT_API_SECRET": "test-secret",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    async def test_get_week_uses_bearer_auth(self):
        session = FakeSession([FakeResponse(200, {"week": {"timezone": "America/New_York"}, "series": []})])
        result = await WebsiteScheduleClient(session=session).get_week(1)
        self.assertEqual(result["series"], [])
        method, url, kwargs = session.requests[0]
        self.assertEqual((method, url), ("GET", "https://qrls.example/api/bot/schedule"))
        self.assertEqual(kwargs["params"], {"week": "1"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret")

    async def test_update_time_sends_optimistic_version_and_offset(self):
        session = FakeSession([FakeResponse(200, {"series": {"id": SERIES_ID, "version": 8}})])
        when = datetime(2026, 9, 1, 20, 30, tzinfo=ZoneInfo("America/New_York"))
        result = await WebsiteScheduleClient(session=session).update_time("propose", SERIES_ID, when, 123456789012345, 7)
        self.assertEqual(result["version"], 8)
        self.assertEqual(session.requests[0][2]["json"], {
            "action": "propose",
            "seriesId": SERIES_ID,
            "scheduledAt": "2026-09-01T20:30:00-04:00",
            "actorDiscordId": "123456789012345",
            "expectedVersion": 7,
        })

    async def test_link_channel_uses_string_discord_id(self):
        session = FakeSession([FakeResponse(200, {"series": {"id": SERIES_ID, "version": 3}})])
        await WebsiteScheduleClient(session=session).link_channel(SERIES_ID, 123456789012345)
        self.assertEqual(session.requests[0][2]["json"], {
            "action": "link_channel",
            "seriesId": SERIES_ID,
            "channelId": "123456789012345",
        })

    async def test_conflict_is_identifiable(self):
        session = FakeSession([FakeResponse(409, {"error": "The series changed since it was loaded."})])
        with self.assertRaises(ScheduleAPIError) as raised:
            await WebsiteScheduleClient(session=session).update_time(
                "confirm", SERIES_ID,
                datetime(2026, 9, 1, 20, 30, tzinfo=ZoneInfo("America/New_York")),
                123456789012345, 7,
            )
        self.assertTrue(raised.exception.is_conflict)


if __name__ == "__main__":
    unittest.main()
