import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.role_sync import RoleSync
from utils.website_roles import WebsiteRolesClient

JOB_ID = "123e4567-e89b-42d3-a456-426614174000"


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


class WebsiteRolesClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "QRLS_WEBSITE_BASE_URL": "https://qrls.example/",
            "QRLS_BOT_API_SECRET": "test-secret",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    async def test_claim_complete_and_fail_contract(self):
        job = {"id": JOB_ID, "discord_user_id": "123"}
        session = FakeSession([
            FakeResponse(200, {"job": job}),
            FakeResponse(200, {"ok": True}),
            FakeResponse(200, {"ok": True, "status": "pending"}),
        ])
        client = WebsiteRolesClient(session=session)

        self.assertEqual(await client.claim(), job)
        await client.complete(JOB_ID)
        await client.fail(JOB_ID, " Discord   failed\nretry ")

        self.assertEqual(session.requests[0][0:2], ("GET", "https://qrls.example/api/bot/roles"))
        self.assertEqual(session.requests[0][2]["params"], {"claim": "role_sync"})
        self.assertEqual(session.requests[0][2]["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(session.requests[1][2]["json"], {"action": "complete", "jobId": JOB_ID})
        self.assertEqual(session.requests[2][2]["json"], {
            "action": "fail", "jobId": JOB_ID, "error": "Discord failed retry",
        })


class FakeBot:
    def __init__(self, guild=None):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild

    async def wait_until_ready(self):
        await asyncio.Event().wait()


class FakeWebsite:
    def __init__(self, job):
        self.job = job
        self.claim = AsyncMock(side_effect=self._claim)
        self.complete = AsyncMock()
        self.fail = AsyncMock()

    async def _claim(self):
        value, self.job = self.job, None
        return value


class RoleSyncPollerTest(unittest.IsolatedAsyncioTestCase):
    def job(self):
        return {"id": JOB_ID, "discord_user_id": "123", "remove_role_id": "10", "add_role_id": "20"}

    async def test_applies_roles_before_completion(self):
        cog = RoleSync(FakeBot())
        cog.website = FakeWebsite(self.job())
        events = []

        async def apply(job):
            events.append("roles")

        async def complete(job_id):
            events.append(f"complete:{job_id}")

        cog.apply = apply
        cog.website.complete.side_effect = complete
        await cog.poll_once()

        self.assertEqual(events, ["roles", f"complete:{JOB_ID}"])
        cog.website.fail.assert_not_awaited()
        self.assertIsNone(cog._pending_completion)

    async def test_reports_role_failure_without_completing(self):
        cog = RoleSync(FakeBot())
        cog.website = FakeWebsite(self.job())
        cog.apply = AsyncMock(side_effect=RuntimeError("Discord role update failed"))

        await cog.poll_once()

        cog.website.complete.assert_not_awaited()
        cog.website.fail.assert_awaited_once()
        self.assertEqual(cog.website.fail.await_args.args[0], JOB_ID)
        self.assertIn("Discord role update failed", cog.website.fail.await_args.args[1])

    async def test_pending_completion_retries_without_reapplying(self):
        cog = RoleSync(FakeBot())
        cog.website = FakeWebsite(self.job())
        cog.apply = AsyncMock()
        cog._pending_completion = JOB_ID

        await cog.poll_once()

        cog.website.complete.assert_awaited_once_with(JOB_ID)
        cog.website.claim.assert_not_awaited()
        cog.apply.assert_not_awaited()

    async def test_overlapping_poll_is_skipped(self):
        cog = RoleSync(FakeBot())
        entered, release = asyncio.Event(), asyncio.Event()
        cog.website = FakeWebsite(None)

        async def claim():
            entered.set()
            await release.wait()
            return None

        cog.website.claim.side_effect = claim
        first = asyncio.create_task(cog.poll_once())
        await entered.wait()
        await cog.poll_once()
        release.set()
        await first

        cog.website.claim.assert_awaited_once()

    async def test_lifecycle_starts_once_and_cancels_before_ready(self):
        cog = RoleSync(FakeBot())
        await cog.cog_load()
        first_task = cog.poll_role_sync.get_task()
        await cog.cog_load()
        self.assertIs(cog.poll_role_sync.get_task(), first_task)
        cog.cog_unload()
        await asyncio.sleep(0)
        self.assertTrue(first_task.cancelled() or first_task.done())

    async def test_remove_then_add_uses_api_role_ids(self):
        removed, added = SimpleNamespace(id=10), SimpleNamespace(id=20)
        events = []

        class Member:
            roles = [removed]

            async def remove_roles(self, role, **kwargs):
                events.append(("remove", role.id))

            async def add_roles(self, role, **kwargs):
                events.append(("add", role.id))

        member = Member()
        guild = SimpleNamespace(
            get_member=lambda member_id: member,
            fetch_member=AsyncMock(),
            get_role=lambda role_id: {10: removed, 20: added}.get(role_id),
        )
        cog = RoleSync(FakeBot(guild))
        cog.guild_id = 999

        await cog.apply(self.job())

        self.assertEqual(events, [("remove", 10), ("add", 20)])
        guild.fetch_member.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
