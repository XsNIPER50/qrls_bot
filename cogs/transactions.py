# cogs/transaction.py
import os
import csv
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

# Optional: reuse cooldown helper
try:
    from utils.global_cooldown import check_cooldown
except Exception:
    async def check_cooldown(interaction: Interaction) -> bool:
        return True

load_dotenv()

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
TRANSACTIONS_CHANNEL_ID = int(os.getenv("TRANSACTIONS_CHANNEL_ID", 0))

CSV_FILE = "data/salaries.csv"


def is_admin_user(member: discord.Member) -> bool:
    """Admins by permission or by ADMINS_ROLE_ID in .env"""
    if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    return False


def _safe_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


class Transaction(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="transaction",
        description="(Admin) Move a player to a new team and log the transaction."
    )
    @app_commands.describe(
        discord_id="The player's Discord ID (numbers only).",
        team="The team name to move them to."
    )
    async def transaction(self, interaction: Interaction, discord_id: str, team: str):
        # Admin gate
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
            return

        if not await check_cooldown(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        # Basic validation
        discord_id = _safe_str(discord_id)
        new_team = _safe_str(team)
        if not discord_id.isdigit():
            await interaction.followup.send("❌ discord_id must be numbers only.", ephemeral=True)
            return
        if not new_team:
            await interaction.followup.send("❌ team cannot be blank.", ephemeral=True)
            return

        if not os.path.exists(CSV_FILE):
            await interaction.followup.send(f"❌ CSV file not found: `{CSV_FILE}`", ephemeral=True)
            return

        # Load CSV rows
        with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        if "discord_id" not in fieldnames or "team" not in fieldnames:
            await interaction.followup.send(
                "❌ salaries.csv must include headers at least: discord_id, team",
                ephemeral=True
            )
            return

        # Find player
        target_row = None
        for r in rows:
            if _safe_str(r.get("discord_id")) == discord_id:
                target_row = r
                break

        if not target_row:
            await interaction.followup.send(
                f"❌ No player found in salaries.csv with discord_id `{discord_id}`.",
                ephemeral=True
            )
            return

        old_team = _safe_str(target_row.get("team")) or "Unassigned"
        target_row["team"] = new_team

        # Write back (preserve existing columns)
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
        with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Determine player display name for message
        member_obj = interaction.guild.get_member(int(discord_id))
        if member_obj is None:
            try:
                member_obj = await interaction.guild.fetch_member(int(discord_id))
            except Exception:
                member_obj = None

        csv_nickname = _safe_str(target_row.get("nickname"))
        player_name = (
            member_obj.display_name if member_obj
            else (csv_nickname if csv_nickname else f"<@{discord_id}>")
        )

        # Send log message to channel ID from env
        log_channel = interaction.guild.get_channel(TRANSACTIONS_CHANNEL_ID) if TRANSACTIONS_CHANNEL_ID else None
        log_text = f"**{player_name}** has been added to **{new_team}** from **{old_team}**."

        if log_channel and isinstance(log_channel, discord.TextChannel):
            await log_channel.send(log_text)

        await interaction.followup.send(
            f"✅ Updated `{player_name}`: **{old_team} → {new_team}**" +
            ("" if log_channel else "\n⚠️ TRANSACTIONS_CHANNEL_ID not set or channel not found; no log was posted."),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Transaction(bot))
