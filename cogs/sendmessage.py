import os
import logging

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("qrls.sendmessage")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CHANGELOG_CHANNEL_ID = int(os.getenv("CHANGELOG_CHANNEL_ID", 0))


def user_is_admin(member: discord.Member) -> bool:
    """
    Standard QRLS admin check:
    - Administrator permission
    OR
    - Has ADMINS_ROLE_ID
    """
    if member.guild_permissions.administrator:
        return True

    if ADMINS_ROLE_ID and any(role.id == ADMINS_ROLE_ID for role in member.roles):
        return True

    return False


class SendMessage(commands.Cog):
    """
    /sendmessage - send a message as the bot to a specified channel (admins only)
    """

    def __init__(self, bot: commands.Bot | commands.AutoShardedBot):
        self.bot = bot

    @app_commands.command(
        name="sendmessage",
        description="Send a message as the bot to a specified channel (admins only).",
    )
    @app_commands.describe(
        channel_id="The ID of the channel to send the message to",
        message="The message to send",
    )
    async def sendmessage(
        self,
        interaction: Interaction,
        channel_id: str,
        message: str,
    ):
        # Must be in a guild
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Permission check - ONLY admins
        if not user_is_admin(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        # Validate channel ID
        try:
            channel_id_int = int(channel_id)
        except ValueError:
            await interaction.response.send_message(
                "Invalid channel ID provided.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(channel_id_int)

        if channel is None:
            await interaction.response.send_message(
                "I could not find a channel with that ID.",
                ephemeral=True,
            )
            return

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "That channel is not a text channel.",
                ephemeral=True,
            )
            return

        # Send the message as the bot
        try:
            await channel.send(message)
        except Exception as e:
            logger.error("Failed to send message: %s", e, exc_info=True)
            await interaction.response.send_message(
                "Failed to send the message. Check bot permissions.",
                ephemeral=True,
            )
            return

        logger.info(
            "Message sent by %s (%s) to #%s (%s)",
            interaction.user,
            interaction.user.id,
            channel.name,
            channel.id,
        )

        # Log to CHANGELOG_CHANNEL_ID, if configured
        if CHANGELOG_CHANNEL_ID:
            changelog_channel = self.bot.get_channel(CHANGELOG_CHANNEL_ID)
        else:
            changelog_channel = None

        if isinstance(changelog_channel, discord.TextChannel):
            try:
                # Avoid hitting the 2000-char limit; trim if needed
                content_preview = message
                max_len = 1800  # leave room for surrounding text
                if len(content_preview) > max_len:
                    content_preview = content_preview[:max_len] + "... [truncated]"

                log_msg = (
                    "📝 **/sendmessage used**\n"
                    f"• **By:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                    f"• **Target channel:** {channel.mention} (`{channel.id}`)\n"
                    "• **Content:**\n"
                    f"```txt\n{content_preview}\n```"
                )

                await changelog_channel.send(log_msg)
            except Exception as e:
                logger.error(
                    "Failed to send /sendmessage log to CHANGELOG_CHANNEL_ID: %s",
                    e,
                    exc_info=True,
                )

        # Confirmation (ephemeral)
        await interaction.response.send_message(
            (
                "✅ Message sent successfully.\n\n"
                f"• **Channel:** {channel.mention}\n"
                f"• **Content:**\n"
                f"```txt\n{message}\n```"
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot | commands.AutoShardedBot):
    await bot.add_cog(SendMessage(bot))
