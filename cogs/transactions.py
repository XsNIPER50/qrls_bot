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
        member="Pick the player to move.",
        team="Pick the team role to move them to."
    )
    async def transaction(self, interaction: Interaction, member: discord.Member, team: discord.Role):
        # Admin gate
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
            return

        if not await check_cooldown(interaction):
            return

        await interaction.response.defer(ephemeral=True)

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

        discord_id = str(member.id)
        new_team_name = team.name

        # Find player row
        target_row = None
        for r in rows:
            if _safe_str(r.get("discord_id")) == discord_id:
                target_row = r
                break

        if not target_row:
            await interaction.followup.send(
                f"❌ No player found in salaries.csv for {member.mention}.",
                ephemeral=True
            )
            return

        old_team_name = _safe_str(target_row.get("team")) or "Unassigned"

        # Update team in CSV
        target_row["team"] = new_team_name

        # Write back
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
        with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Resolve old team role (by name from CSV)
        old_team_role = discord.utils.get(interaction.guild.roles, name=old_team_name)

        # Build ping-safe message parts
        player_ping = member.mention
        new_team_ping = team.mention
        old_team_ping = old_team_role.mention if old_team_role else old_team_name

        log_text = f"{player_ping} has been added to {new_team_ping} from {old_team_ping}."

        # Send log message
        log_channel = interaction.guild.get_channel(TRANSACTIONS_CHANNEL_ID) if TRANSACTIONS_CHANNEL_ID else None
        if log_channel and isinstance(log_channel, discord.TextChannel):
            await log_channel.send(
                log_text,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
            )

        await interaction.followup.send(
            f"✅ Transaction complete: {member.display_name} — **{old_team_name} → {new_team_name}**",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Transaction(bot))
