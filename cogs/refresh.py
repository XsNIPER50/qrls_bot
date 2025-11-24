# cogs/refresh.py
import os
import csv
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

# Optional: global cooldown helper (admins bypass via your global gate in bot.py)
try:
    from utils.global_cooldown import check_cooldown
except Exception:
    async def check_cooldown(interaction: Interaction) -> bool:
        return True

load_dotenv()

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET", "").strip()

# 🔔 New: changelog channel ID
CHANGELOG_CHANNEL_ID = int(os.getenv("CHANGELOG_CHANNEL_ID", 0))

CSV_FILE = "data/salaries.csv"
REQUIRED_HEADERS = ["discord_id", "nickname", "salary", "team", "captain"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def is_admin_user(member: discord.Member) -> bool:
    """Admins by permission or by ADMINS_ROLE_ID in .env"""
    if member.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    return False


def get_gspread_client():
    if not SERVICE_JSON or not os.path.exists(SERVICE_JSON):
        raise FileNotFoundError(
            f"Service account JSON not found at path set in GOOGLE_SERVICE_ACCOUNT_JSON: {SERVICE_JSON}"
        )
    creds = Credentials.from_service_account_file(SERVICE_JSON, scopes=SCOPES)
    return gspread.authorize(creds)


def normalize_row(row: dict) -> dict:
    """Normalize types and whitespace; safely handle numeric salary values."""
    out = {}
    for k in REQUIRED_HEADERS:
        val = row.get(k, "")
        # Convert numbers to strings and strip safely
        if isinstance(val, (int, float)):
            val = str(val)
        elif val is None:
            val = ""
        else:
            val = str(val).strip()
        out[k] = val

    # Convert salary field to integer string
    try:
        out["salary"] = str(int(float(out["salary"])))
    except Exception:
        out["salary"] = out["salary"] or "0"

    # captain stays as-is (e.g., "TRUE"/"FALSE" from sheet)
    return out


class Refresh(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _log_to_changelog(self, interaction: Interaction, msg: str):
        """Send a log message to the configured changelog channel, if set."""
        if not CHANGELOG_CHANNEL_ID:
            return  # no channel configured

        # Try to get the channel from the bot cache
        channel = self.bot.get_channel(CHANGELOG_CHANNEL_ID)
        if channel is None and interaction.guild is not None:
            channel = interaction.guild.get_channel(CHANGELOG_CHANNEL_ID)

        if channel is None:
            return

        # Post the same message plus who ran it
        await channel.send(f"🧾 `/refresh` by {interaction.user.mention}:\n{msg}")

    @app_commands.command(
        name="refresh",
        description="(Admin) Pull from Google Sheet and overwrite data/salaries.csv."
    )
    @app_commands.describe(dry_run="Preview only (no file write). Defaults to True.")
    async def refresh(self, interaction: Interaction, dry_run: bool = True):
        # Admin gate
        if not is_admin_user(interaction.user):
            await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
            return

        if not await check_cooldown(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        # Validate env
        if not SHEET_ID:
            await interaction.followup.send("❌ GOOGLE_SHEET_ID is not set in your .env.", ephemeral=True)
            return
        if not SERVICE_JSON:
            await interaction.followup.send("❌ GOOGLE_SERVICE_ACCOUNT_JSON is not set in your .env.", ephemeral=True)
            return

        try:
            gc = get_gspread_client()
            sh = gc.open_by_key(SHEET_ID)
            ws = sh.worksheet(WORKSHEET_NAME) if WORKSHEET_NAME else sh.sheet1

            # Pull rows
            values = ws.get_all_records(default_blank="")
            if not values:
                await interaction.followup.send("⚠️ Sheet appears empty (no data rows).", ephemeral=True)
                return

            # Header check using the first dict's keys
            sheet_headers = list(values[0].keys())
            missing = [h for h in REQUIRED_HEADERS if h not in sheet_headers]
            extra = [h for h in sheet_headers if h not in REQUIRED_HEADERS]
            if missing:
                msg = (
                    f"❌ Missing required columns in sheet: {', '.join(missing)}\n"
                    f"Expected headers: {', '.join(REQUIRED_HEADERS)}"
                )
                await interaction.followup.send(msg, ephemeral=True)
                return

            # Normalize
            normalized = [normalize_row(r) for r in values]

            # Dry run
            if dry_run:
                msg = (
                    "🔎 **Dry run** complete.\n"
                    f"• Rows found: **{len(normalized)}**\n"
                    f"• Required headers OK ✅\n"
                    + (f"• Extra columns (ignored): {', '.join(extra)}" if extra else "• No extra columns.")
                )
                await interaction.followup.send(msg, ephemeral=True)
                await self._log_to_changelog(interaction, msg)
                return

            # Write CSV
            os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=REQUIRED_HEADERS)
                writer.writeheader()
                writer.writerows(normalized)

            msg = f"✅ Refreshed **{CSV_FILE}** with **{len(normalized)}** rows."
            await interaction.followup.send(msg, ephemeral=True)
            await self._log_to_changelog(interaction, msg)

        except gspread.exceptions.APIError as e:
            await interaction.followup.send(f"⚠️ Google API error: `{e}`", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Unexpected error: `{e}`", ephemeral=True)
            raise


async def setup(bot):
    await bot.add_cog(Refresh(bot))
