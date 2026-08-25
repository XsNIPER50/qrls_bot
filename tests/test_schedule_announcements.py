import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from utils.schedule_announcements import deliver_confirmed_announcement, deliver_proposed_announcement


class FakeRole:
    def __init__(self, role_id, name, mention):
        self.id, self.name, self.mention = role_id, name, mention


class FakeMember:
    def __init__(self, user_id, name, *, administrator=False, roles=None):
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.roles = roles or []


class FakeEmoji:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"<:{self.name}:1>"


class FakeMessage:
    def __init__(self):
        self.reactions = []

    async def add_reaction(self, reaction):
        self.reactions.append(reaction)


class FakeChannel:
    def __init__(self, name, guild):
        self.name, self.guild = name, guild
        self.sent = []

    async def send(self, content=None, **kwargs):
        message = FakeMessage()
        self.sent.append({"content": content, "message": message, **kwargs})
        return message


class FakeGuild:
    def __init__(self):
        self.captains = FakeRole(10, "Captains", "<@&10>")
        self.team_a = FakeRole(11, "Arctic Assassins", "<@&11>")
        self.team_b = FakeRole(12, "Clarity United", "<@&12>")
        self.roles = [self.captains, self.team_a, self.team_b]
        self.members = {
            101: FakeMember(101, "Website Admin", administrator=True),
            202: FakeMember(202, "Original Captain"),
        }
        self.emojis = [FakeEmoji("Arctic_Assassins"), FakeEmoji("Clarity_United")]
        self.text_channels = []

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_member(self, user_id):
        return self.members.get(user_id)


class ScheduleAnnouncementTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.origin = FakeChannel("week1-arctic-assassins-vs-clarity-united", self.guild)
        self.results = FakeChannel("💥・scheduling", self.guild)
        self.matches = FakeChannel("📜・scheduled-matches", self.guild)
        self.guild.text_channels = [self.origin, self.results, self.matches]
        self.when = datetime(2026, 9, 1, 20, 30, tzinfo=ZoneInfo("America/New_York"))

    async def test_proposed_delivery_reproduces_both_origin_messages(self):
        await deliver_proposed_announcement(
            self.origin, actor_id=101, scheduled_at=self.when, captains_role_id=10
        )
        self.assertEqual(len(self.origin.sent), 2)
        self.assertEqual(self.origin.sent[0]["content"], "<@&10> — A match time has been proposed.")
        embed = self.origin.sent[1]["embed"]
        self.assertEqual(embed.title, "📌 Proposed Match Time")
        self.assertIn("<@101>", embed.description)
        self.assertIn("9/1 8:30PM ET", embed.description)

    async def test_confirmed_delivery_reproduces_all_messages_and_reactions(self):
        await deliver_confirmed_announcement(
            self.origin,
            actor_id=101,
            proposer_id=202,
            scheduled_at=self.when,
            team_a="Arctic Assassins",
            team_b="Clarity United",
            captains_role_id=10,
        )
        self.assertEqual(len(self.origin.sent), 2)
        self.assertEqual(len(self.results.sent), 1)
        self.assertEqual(len(self.matches.sent), 1)
        embed = self.origin.sent[1]["embed"]
        self.assertIn("confirmed by <@101>", embed.description)
        self.assertEqual(embed.footer.text, "Confirmed by Website Admin (Admin)")
        self.assertIn("<@&11> vs <@&12>", self.results.sent[0]["content"])
        self.assertEqual(self.matches.sent[0]["message"].reactions, ["🎙️", "🎥"])


if __name__ == "__main__":
    unittest.main()
