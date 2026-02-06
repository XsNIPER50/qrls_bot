import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils.global_cooldown import check_cooldown

# ✅ Load .env
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

# Access levels
ACCESS_EVERYONE = "everyone"
ACCESS_CAPTAIN = "captain"  # captains + admins
ACCESS_ADMIN = "admin"      # admins only

# 🔧 Central place to control who should see what
# Anything not listed here defaults to ACCESS_EVERYONE
COMMAND_ACCESS: dict[str, str] = {
    # Admin-only commands
    "startweek": ACCESS_ADMIN,
    "clearschedule": ACCESS_ADMIN,
    "refresh": ACCESS_ADMIN,
    "unretire": ACCESS_ADMIN,
    "updateuser": ACCESS_ADMIN,
    "retire": ACCESS_ADMIN,
    "settoken": ACCESS_ADMIN,
    "token":ACCESS_ADMIN,

    # Captain-only commands (admins can also see/use these):
    "add": ACCESS_CAPTAIN,
    "drop": ACCESS_CAPTAIN,
    "trade": ACCESS_CAPTAIN,
    "propose": ACCESS_CAPTAIN,
    "confirm": ACCESS_CAPTAIN,
    "waiverclaim": ACCESS_CAPTAIN,
    "sub": ACCESS_CAPTAIN,
    "propose": ACCESS_CAPTAIN,
    "confirm": ACCESS_CAPTAIN
}


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="help",
        description="Shows the commands available to you and what they do."
    )
    async def help(self, interaction: discord.Interaction):
        """Lists only the commands the user has permission to use."""
        if not await check_cooldown(interaction):
            return

        guild = interaction.guild
        user = interaction.user

        # If it's somehow not in a guild, just bail nicely
        if not guild or not isinstance(user, discord.Member):
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        # --- Determine user's permissions ---
        is_admin = (
            user.guild_permissions.administrator
            or (ADMINS_ROLE_ID and discord.utils.get(user.roles, id=ADMINS_ROLE_ID))
        )
        is_captain = bool(CAPTAINS_ROLE_ID and discord.utils.get(user.roles, id=CAPTAINS_ROLE_ID))

        embed = discord.Embed(
            title="📘 QRLS Bot Command Reference",
            color=discord.Color.blurple()
        )

        visible_commands: list[tuple[str, str]] = []

        for command in self.bot.tree.get_commands():
            # Only consider top-level slash commands
            if not isinstance(command, app_commands.Command):
                continue

            cmd_name = command.name

            # Decide access level from map (default: everyone)
            access = COMMAND_ACCESS.get(cmd_name, ACCESS_EVERYONE)

            # --- Apply visibility rules ---
            if access == ACCESS_ADMIN and not is_admin:
                continue
            if access == ACCESS_CAPTAIN and not (is_admin or is_captain):
                continue

            # --- Mark with icons ---
            if access == ACCESS_ADMIN:
                icon = "🔒 "
            elif access == ACCESS_CAPTAIN:
                icon = "⚓ "
            else:
                icon = ""

            display_name = f"{icon}/{cmd_name}"
            desc = command.description or "No description provided."
            visible_commands.append((display_name, desc))

        # --- Sort alphabetically for neatness ---
        visible_commands.sort(key=lambda x: x[0].lower())

        # --- Add commands to embed ---
        if visible_commands:
            for name, desc in visible_commands:
                embed.add_field(name=name, value=desc, inline=False)
        else:
            embed.description = "No commands available to you."

        # --- Add emoji legend ---
        legend = "🔒 = Admin-only | ⚓ = Captain-only | 🕓 = Cooldown (8s for non-admins)"
        embed.add_field(name="Legend", value=legend, inline=False)

        # --- Footer text ---
        if is_admin:
            embed.set_footer(text="You have access to all commands (including Admin-only and Captain-only).")
        elif is_captain:
            embed.set_footer(text="⚓ Captain access: Admin-only commands are hidden.")
        else:
            embed.set_footer(text="Only showing commands available to you.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
