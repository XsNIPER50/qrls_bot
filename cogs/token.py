import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("qrls.token")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

DATA_DIR = "data"
TOKEN_STORE_FILE = os.path.join(DATA_DIR, "token_store.json")

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))  # your standard admin role


def load_token_store() -> Optional[Dict[str, Any]]:
    """
    Load token + metadata from JSON file.
    Returns None if file missing or unreadable.
    """
    if not os.path.exists(TOKEN_STORE_FILE):
        return None

    try:
        with open(TOKEN_STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to read token store: %s", e, exc_info=True)
        return None


def user_is_admin(member: discord.Member) -> bool:
    """
    Standard QRLS-style admin check:
    - guild administrator perms
    OR
    - has ADMINS_ROLE_ID
    """
    if member.guild_permissions.administrator:
        return True

    if ADMINS_ROLE_ID and any(role.id == ADMINS_ROLE_ID for role in member.roles):
        return True

    return False


class Token(commands.Cog):
    """
    /token  - retrieve stored token if not expired
    """

    def __init__(self, bot: commands.Bot | commands.AutoShardedBot):
        self.bot = bot

    @app_commands.command(
        name="token",
        description="Retrieve the currently stored token if it has not expired.",
    )
    async def token_cmd(self, interaction: Interaction):
        # Must be in a guild and user must be a Member
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Permission check (same as /settoken)
        if not user_is_admin(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        store = load_token_store()
        if not store:
            await interaction.response.send_message(
                "ℹ️ There is currently **no token** stored. Use `/settoken` first.",
                ephemeral=True,
            )
            return

        token_value = store.get("token")
        expires_at_raw = store.get("expires_at")
        set_by_id = store.get("set_by_id")
        set_by_name = store.get("set_by_name")
        set_at_raw = store.get("set_at")

        # Validate stored data
        if not token_value or not expires_at_raw:
            await interaction.response.send_message(
                "⚠️ The stored token data is invalid or incomplete. "
                "Please set a new token with `/settoken`.",
                ephemeral=True,
            )
            return

        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except Exception:
            await interaction.response.send_message(
                "⚠️ Could not parse the stored expiration time. "
                "Please set a new token with `/settoken`.",
                ephemeral=True,
            )
            return

        now_utc = datetime.now(timezone.utc)
        if now_utc >= expires_at:
            logger.info(
                "Token has expired at %s; requested by %s (%s)",
                expires_at.isoformat(),
                interaction.user,
                interaction.user.id,
            )
            await interaction.response.send_message(
                (
                    "⏰ The stored token has **expired**.\n\n"
                    f"• **Expired at (UTC):** <t:{int(expires_at.timestamp())}:F>\n"
                    "Please set a new token with `/settoken`."
                ),
                ephemeral=True,
            )
            return

        # Token is valid; show only to the caller
        set_at_str = ""
        try:
            if set_at_raw:
                set_at_dt = datetime.fromisoformat(set_at_raw)
                set_at_str = f"<t:{int(set_at_dt.timestamp())}:F>"
        except Exception:
            set_at_str = set_at_raw or "Unknown"

        expires_str = f"<t:{int(expires_at.timestamp())}:F>"

        set_by_display = set_by_name or "Unknown"
        if set_by_id:
            set_by_display = (
                f"<@{set_by_id}> ({set_by_name})"
                if set_by_name
                else f"<@{set_by_id}>"
            )

        await interaction.response.send_message(
            (
                "🔐 **Current token:**\n"
                f"```txt\n{token_value}\n```\n"
                "Metadata:\n"
                f"• **Set by:** {set_by_display}\n"
                f"• **Set at (UTC):** {set_at_str}\n"
                f"• **Expires (UTC):** {expires_str}"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot | commands.AutoShardedBot):
    await bot.add_cog(Token(bot))
