import os
import csv
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.team_info import TEAM_INFO


def _get_env_int(name: str) -> Optional[int]:
    v = os.getenv(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


class Transactions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.salaries_path = os.path.join("data", "salaries.csv")
        self.headers = ["discord_id", "nickname", "salary", "team", "captain"]

    def _load_rows(self) -> list[dict]:
        with open(self.salaries_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _write_rows(self, rows: list[dict]) -> None:
        with open(self.salaries_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.headers)
            writer.writeheader()
            writer.writerows(rows)

    def _find_row(self, rows: list[dict], user_id: int) -> Optional[dict]:
        target = str(user_id).strip()
        for r in rows:
            if str(r.get("discord_id", "")).strip() == target:
                return r
        return None

    def _has_role_id(self, member: discord.Member, role_id: int) -> bool:
        return any(role.id == role_id for role in member.roles)

    def _get_team_role_id(self, team_name: str) -> Optional[int]:
        info = TEAM_INFO.get(team_name)
        if not isinstance(info, dict):
            return None
        role_id = info.get("id")
        if isinstance(role_id, int):
            return role_id
        # Sometimes people accidentally leave it blank (None) or as a string
        if isinstance(role_id, str) and role_id.isdigit():
            return int(role_id)
        return None

    async def _send_transaction_log(
        self,
        guild: discord.Guild,
        captain_team: str,
        drop_player: discord.Member,
        add_player: discord.Member
    ):
        channel_id = _get_env_int("TRANSACTIONS_CHANNEL_ID")
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        role_id = self._get_team_role_id(captain_team)
        if not role_id:
            # Hard stop behavior requested: we want logs to ALWAYS ping roles
            await channel.send(
                f"⚠️ Transaction completed but could not ping team role: "
                f"`{captain_team}` has no valid `id` in TEAM_INFO."
            )
            return

        team_mention = f"<@&{role_id}>"
        await channel.send(
            f"{team_mention} drop {drop_player.mention} to 2-Day Waivers and pick up {add_player.mention}"
        )

    @app_commands.command(
        name="transactions",
        description="Drop a player to Free Agent and add another player to your team."
    )
    @app_commands.guild_only()
    async def transactions(
        self,
        interaction: discord.Interaction,
        drop_player: discord.Member,
        add_player: discord.Member,
    ):
        # ---- Permission: Captains only ----
        captains_role_id = _get_env_int("CAPTAINS_ROLE_ID")
        if not captains_role_id:
            return await interaction.response.send_message(
                "CAPTAINS_ROLE_ID is not set correctly in `.env`.",
                ephemeral=True
            )

        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "This command must be used in a server.",
                ephemeral=True
            )

        if not self._has_role_id(interaction.user, captains_role_id):
            return await interaction.response.send_message(
                "Only captains can run this command.",
                ephemeral=True
            )

        # ---- Restrict: Transactions category only ----
        transactions_category_id = _get_env_int("TRANSACTIONS_CATEGORY_ID")
        if not transactions_category_id:
            return await interaction.response.send_message(
                "TRANSACTIONS_CATEGORY_ID is not set correctly in `.env`.",
                ephemeral=True
            )

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(
                "This command must be run in a text channel.",
                ephemeral=True
            )

        base_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(base_channel, discord.TextChannel) or base_channel.category_id != transactions_category_id:
            return await interaction.response.send_message(
                "This command can only be used in the Transactions category.",
                ephemeral=True
            )

        # ---- Sanity check ----
        if drop_player.id == add_player.id:
            return await interaction.response.send_message(
                "STOP: Drop player and add player cannot be the same person.",
                ephemeral=True
            )

        # ---- Load salaries.csv ----
        try:
            rows = self._load_rows()
        except FileNotFoundError:
            return await interaction.response.send_message(
                f"Could not find `{self.salaries_path}`.",
                ephemeral=True
            )

        # ---- Captain must exist in salaries.csv to determine team ----
        captain_row = self._find_row(rows, interaction.user.id)
        if not captain_row:
            return await interaction.response.send_message(
                "STOP: You (captain) are not found in salaries.csv. Cannot determine your team.",
                ephemeral=True
            )

        captain_team = str(captain_row.get("team", "")).strip()
        if not captain_team:
            return await interaction.response.send_message(
                "STOP: Your team is blank in salaries.csv. Cannot proceed.",
                ephemeral=True
            )

        # If you truly want "always ping roles", enforce team exists in TEAM_INFO now
        captain_role_id = self._get_team_role_id(captain_team)
        if not captain_role_id:
            return await interaction.response.send_message(
                f"STOP: Your team `{captain_team}` does not have a valid role `id` in utils/team_info.py.",
                ephemeral=True
            )

        # ---- Both players must exist in salaries.csv ----
        drop_row = self._find_row(rows, drop_player.id)
        if not drop_row:
            return await interaction.response.send_message(
                f"STOP: `{drop_player.display_name}` is not within salaries.csv.",
                ephemeral=True
            )

        add_row = self._find_row(rows, add_player.id)
        if not add_row:
            return await interaction.response.send_message(
                f"STOP: `{add_player.display_name}` is not within salaries.csv.",
                ephemeral=True
            )

        # ---- Guardrails + enforcement ----
        drop_team_current = str(drop_row.get("team", "")).strip()

        # Guardrail: can't drop a Free Agent
        if drop_team_current == "Free Agent":
            return await interaction.response.send_message(
                f"STOP: `{drop_player.display_name}` is already a **Free Agent** in salaries.csv.",
                ephemeral=True
            )

        # Enforce: drop must be from captain's team
        if drop_team_current != captain_team:
            return await interaction.response.send_message(
                f"STOP: `{drop_player.display_name}` is not on your team in salaries.csv "
                f"(they are on `{drop_team_current}`; your team is `{captain_team}`).",
                ephemeral=True
            )

        add_team_current = str(add_row.get("team", "")).strip()

        # Guardrail: can't add someone already on your team
        if add_team_current == captain_team:
            return await interaction.response.send_message(
                f"STOP: `{add_player.display_name}` is already on **{captain_team}** in salaries.csv.",
                ephemeral=True
            )

        # ---- Apply changes ----
        drop_row["team"] = "Free Agent"
        add_row["team"] = captain_team

        # ---- Save ----
        self._write_rows(rows)

        # ---- Log transaction ----
        if interaction.guild:
            await self._send_transaction_log(
                interaction.guild,
                captain_team,
                drop_player,
                add_player
            )

        # ---- Confirm ----
        add_from_team = add_team_current or "Unknown"
        await interaction.response.send_message(
            (
                f"✅ Transaction complete for **{captain_team}**\n"
                f"• Dropped: {drop_player.mention} → **Free Agent**\n"
                f"• Added: {add_player.mention} (**{add_from_team}** → **{captain_team}**)"
            ),
            ephemeral=False
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Transactions(bot))
