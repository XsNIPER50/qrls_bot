import asyncio
import logging
import os
import sys
from time import monotonic

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.append(BASE_DIR)

# --- Load environment variables ---
load_dotenv(os.path.join(BASE_DIR, ".env"))
TOKEN = os.getenv("DISCORD_TOKEN")


def env_int(name: str, default: int = 0) -> int:
    """Read an integer environment variable with a useful startup error."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


GUILD_ID = env_int("GUILD_ID")
ADMINS_ROLE_ID = env_int("ADMINS_ROLE_ID")
CAPTAINS_ROLE_ID = env_int("CAPTAINS_ROLE_ID")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("qrls.bot")

# --- Discord bot setup ---
THROTTLE_SECONDS = 8.0
_last_use_by_user: dict[int, float] = {}

def is_admin_user(interaction: discord.Interaction) -> bool:
    """Admins include anyone with Admin role ID or Administrator permission."""
    if not interaction.guild:
        return False
    if getattr(interaction.user, "guild_permissions", None) and interaction.user.guild_permissions.administrator:
        return True
    if ADMINS_ROLE_ID and discord.utils.get(interaction.user.roles, id=ADMINS_ROLE_ID):
        return True
    return False


class QRLSCommandTree(app_commands.CommandTree):
    """Command tree with the existing global per-user throttle."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin_user(interaction):
            return True

        uid = interaction.user.id
        now = monotonic()
        wait_for = THROTTLE_SECONDS - (now - _last_use_by_user.get(uid, 0.0))
        if wait_for > 0:
            message = (
                "⏳ You’re using commands too quickly! "
                f"Please wait **{wait_for:.1f} seconds**."
            )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

        _last_use_by_user[uid] = now
        return True


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=QRLSCommandTree)

# ================================================================
# 🤖 BOT READY EVENT
# ================================================================
@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info("Synced slash commands to guild %s", GUILD_ID)
    except Exception as e:
        logger.exception("Failed to sync commands: %s", e)

# ================================================================
# ⚠️ GLOBAL ERROR HANDLER
# ================================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingRole):
        try:
            await interaction.response.send_message(
                "🚫 You don’t have permission to use this command.",
                ephemeral=True
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                "🚫 You don’t have permission to use this command.",
                ephemeral=True
            )
        return

    try:
        await interaction.response.send_message(
            "⚠️ An unexpected error occurred while running this command.",
            ephemeral=True
        )
    except discord.InteractionResponded:
        await interaction.followup.send(
            "⚠️ An unexpected error occurred while running this command.",
            ephemeral=True
        )
    logger.error(
        "Command error: %r",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )

# ================================================================
# 🧩 MAIN ENTRY POINT
# ================================================================
async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required")
    if not GUILD_ID:
        raise RuntimeError("GUILD_ID is required and must be a non-zero integer")

    # Runtime paths in existing cogs are relative to the service directory.
    os.chdir(BASE_DIR)
    async with bot:
        for cog_name in [
            "cogs.startweek",
            "cogs.clearschedule",
            "cogs.salary",
            "cogs.updateuser",
            "cogs.profile",
            "cogs.teaminfo",
            "cogs.help",            # List available commands and a short description
            "cogs.propose",         # Propose Match Day and Time
            "cogs.confirm",         # Confirm proposed Match Day and Time
            "cogs.refresh",         # Refreshes the .csv to match the Google Sheet
            # "cogs.transactions"
            "cogs.add",             # Adds player into roster spot
            "cogs.drop",            # Removed player from roster spot
            "cogs.sub",             # Applies Team role for certain duration
            "cogs.trade",           # Adds opposing captain to chat and initiates the trade
            "cogs.waiverclaim",     # Adds waiver claims to be automated, adding/removing/and putting on a team
            "cogs.unretire",        
            "cogs.retire",          
            "cogs.settoken",        
            "cogs.token",           
            "cogs.sendmessage"

        ]:
            try:
                await bot.load_extension(cog_name)
                logger.info("Cog '%s' loaded successfully", cog_name.split(".")[-1])
            except Exception as e:
                logger.exception("Failed to load '%s': %s", cog_name, e)

        logger.info("Starting bot connection to Discord")
        await bot.start(TOKEN)

# ================================================================
# ▶️ RUN
# ================================================================
if __name__ == "__main__":
    asyncio.run(main())
