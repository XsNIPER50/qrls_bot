import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.website_roster import WebsiteRoster
from utils.website_roster import RosterAPIError, WebsiteRosterClient


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

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class WebsiteRosterClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "QRLS_WEBSITE_BASE_URL": "https://qrls.example/",
            "QRLS_BOT_API_SECRET": "test-secret",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    async def test_add_payload_uses_discord_ids_and_idempotency_key(self):
        session = FakeSession([FakeResponse(200, {"transaction": {"id": "tx", "status": "completed"}, "changes": [], "summary": {}})])
        await WebsiteRosterClient(session=session).add_or_drop(
            "add", actor_id=123456789012345, player_id=223456789012345, request_key="discord:555:add"
        )
        url, kwargs = session.requests[0]
        self.assertEqual(url, "https://qrls.example/api/bot/roster")
        self.assertEqual(kwargs["json"], {
            "action": "add",
            "actorDiscordId": "123456789012345",
            "playerDiscordId": "223456789012345",
            "requestKey": "discord:555:add",
        })
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret")

    async def test_trade_create_payload(self):
        session = FakeSession([FakeResponse(200, {"transaction": {"id": "tx", "status": "pending_captain"}})])
        await WebsiteRosterClient(session=session).create_trade(
            actor_id=123456789012345,
            own_player_id=223456789012345,
            other_player_id=323456789012345,
            request_key="discord:556:trade-create",
        )
        self.assertEqual(session.requests[0][1]["json"]["action"], "trade_create")
        self.assertEqual(session.requests[0][1]["json"]["ownPlayerDiscordId"], "223456789012345")
        self.assertEqual(session.requests[0][1]["json"]["otherPlayerDiscordId"], "323456789012345")

    async def test_trade_decisions_use_separate_authoritative_stages(self):
        session = FakeSession([
            FakeResponse(200, {"transaction": {"id": "tx", "status": "pending_admin"}}),
            FakeResponse(200, {"transaction": {"id": "tx", "status": "completed"}}),
        ])
        client = WebsiteRosterClient(session=session)
        await client.decide_trade(
            "captain", actor_id=423456789012345, transaction_id="tx", decision="approve",
            request_key="discord:557:trade-captain-approve",
        )
        await client.decide_trade(
            "admin", actor_id=523456789012345, transaction_id="tx", decision="approve",
            request_key="discord:558:trade-admin-approve",
        )
        self.assertEqual(session.requests[0][1]["json"]["action"], "trade_captain_decision")
        self.assertEqual(session.requests[1][1]["json"]["action"], "trade_admin_decision")

    async def test_api_errors_are_forwarded_without_credentials(self):
        session = FakeSession([FakeResponse(409, {"error": "That roster already has four active players."})])
        with self.assertRaises(RosterAPIError) as raised:
            await WebsiteRosterClient(session=session).add_or_drop(
                "add", actor_id=123456789012345, player_id=223456789012345, request_key="discord:559:add"
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertNotIn("test-secret", str(raised.exception))


class CommitOrderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_discord_roles_are_applied_before_completed_transaction_post(self):
        cog = WebsiteRoster(SimpleNamespace(get_channel=lambda _: None))
        events = []

        async def roles(guild, changes):
            events.append(("roles", changes))
            return []

        async def post(guild, result):
            events.append(("post", result["transaction"]["status"]))

        cog.apply_role_changes = roles
        cog.post_completed = post
        interaction = SimpleNamespace(guild=object())
        result = {"transaction": {"status": "completed"}, "changes": [{"discordId": "1"}], "summary": {}}
        self.assertEqual(await cog.finish_commit(interaction, result), [])
        self.assertEqual(events, [("roles", result["changes"]), ("post", "completed")])

    async def test_completed_transaction_is_posted_when_role_reconciliation_crashes(self):
        cog = WebsiteRoster(SimpleNamespace(get_channel=lambda _: None))
        events = []

        async def roles(guild, changes):
            raise RuntimeError("role update failed")

        async def post(guild, result):
            events.append("post")

        cog.apply_role_changes = roles
        cog.post_completed = post
        interaction = SimpleNamespace(guild=object())
        result = {"transaction": {"status": "completed"}, "changes": [], "summary": {}}
        issues = await cog.finish_commit(interaction, result)
        self.assertEqual(events, ["post"])
        self.assertIn("role update failed", issues[0])


if __name__ == "__main__":
    unittest.main()
