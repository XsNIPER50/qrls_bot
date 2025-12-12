import sys
import os
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import asyncio
from time import monotonic

# --- Load environment variables ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

# --- Discord bot setup ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================================================================
# 🕒 GLOBAL COOLDOWN (Admins bypass, applies to all slash commands)
# ================================================================
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


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """
    Intercepts all interactions. If it's a slash command,
    apply the cooldown for non-admin users before processing.
    """
    # Make sure it’s a slash command (and not autocomplete, button, etc.)
    if not interaction.command:
        return await bot.process_application_commands(interaction)

    # Skip admins entirely
    if is_admin_user(interaction):
        return await bot.process_application_commands(interaction)

    uid = interaction.user.id
    now = monotonic()
    last = _last_use_by_user.get(uid, 0.0)
    wait_for = THROTTLE_SECONDS - (now - last)

    if wait_for > 0:
        try:
            await interaction.response.send_message(
                f"⏳ You’re using commands too quickly! Please wait **{wait_for:.1f} seconds**.",
                ephemeral=True
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                f"⏳ You’re using commands too quickly! Please wait **{wait_for:.1f} seconds**.",
                ephemeral=True
            )
        return  # Don't run the command

    # Update cooldown and process command
    _last_use_by_user[uid] = now
    await bot.process_application_commands(interaction)

# ================================================================
# 🤖 BOT READY EVENT
# ================================================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"🔄 Synced slash commands to guild {GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Failed to sync commands: {e}")

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
    print(f"Command Error: {error!r}")

# ================================================================
# 🧩 MAIN ENTRY POINT
# ================================================================
async def main():
    async with bot:
        for cog_name in [
            "cogs.startweek",
            "cogs.clearschedule",
            "cogs.salary",
            "cogs.updateuser",
            "cogs.profile",
            "cogs.teaminfo",
            "cogs.help",
            "cogs.propose",
            "cogs.confirm",
            "cogs.refresh",
            "cogs.transactions"
        ]:
            try:
                await bot.load_extension(cog_name)
                print(f"✅ Cog '{cog_name.split('.')[-1]}' loaded successfully")
            except Exception as e:
                print(f"❌ Failed to load '{cog_name}': {e}")

        print("🚀 Starting bot connection to Discord...")
        await bot.start(TOKEN)

# ================================================================
# ▶️ RUN
# ================================================================
if __name__ == "__main__":
    asyncio.run(main())
