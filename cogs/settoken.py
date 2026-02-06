import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("qrls.settoken")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

DATA_DIR = "data"
TOKEN_STORE_FILE = os.path.join(DATA_DIR, "token_store.json")

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))  # your standard admin role


def ensure_data_dir() -> None:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)


def get_expiry_minutes() -> int:
    """
    Read TOKEN_EXPIRY_MINUTES from .env.
    Defaults to 60 minutes if missing/invalid.
    """
    raw = os.getenv("TOKEN_EXPIRY_MINUTES", "60")
    try:
        minutes = int(raw)
        if minutes <= 0:
            raise ValueError
        return minutes
    except ValueError:
        logger.warning(
            "Invalid TOKEN_EXPIRY_MINUTES=%r, defaulting to 60 minutes", raw
        )
        return 60


def save_token_store(data: Dict[str, Any]) -> None:
    """
    Save token + metadata to JSON file.
    """
    ensure_data_dir()
    try:
        with open(TOKEN_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Failed to write token store: %s", e, exc_info=True)


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


class SetToken(commands.Cog):
    """
    /settoken  - store a temporary token with expiration
    """

    def __init__(self, bot: commands.Bot | commands.AutoShardedBot):
        self.bot = bot

    @app_commands.command(
        name="settoken",
        description="Store a temporary token that expires automatically.",
    )
    @app_commands.describe(
        token="The Discord bot token to store temporarily."
    )
    async def settoken(self, interaction: Interaction, token: str):
        # Must be in a guild and user must be a Member
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Permission check
        if not user_is_admin(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # Compute expiration
        expiry_minutes = get_expiry_minutes()
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(minutes=expiry_minutes)

        # Build metadata
        data: Dict[str, Any] = {
            "token": token,
            "set_by_id": interaction.user.id,
            "set_by_name": str(interaction.user),
            "set_at": now_utc.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        save_token_store(data)

        logger.info(
            "Token set by %s (%s), expires at %s",
            interaction.user,
            interaction.user.id,
            expires_at.isoformat(),
        )

        # Respond (ephemeral so only the setter sees it)
        await interaction.response.send_message(
            (
                "✅ Token has been stored.\n\n"
                f"• **Set by:** {interaction.user.mention}\n"
                f"• **Set at (UTC):** <t:{int(now_utc.timestamp())}:F>\n"
                f"• **Expires (UTC):** <t:{int(expires_at.timestamp())}:F>\n"
                f"• **Expires in:** {expiry_minutes} minutes"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot | commands.AutoShardedBot):
    await bot.add_cog(SetToken(bot))
