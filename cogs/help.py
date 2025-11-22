import discord
from discord import app_commands
from discord.ext import commands
from utils.global_cooldown import check_cooldown
import os
from dotenv import load_dotenv

# ✅ Load .env
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Shows the commands available to you and what they do.")
    async def help(self, interaction: discord.Interaction):
        """Lists only the commands the user has permission to use."""
        if not await check_cooldown(interaction):
            return

        embed = discord.Embed(
            title="📘 QRLS Bot Command Reference",
            color=discord.Color.blurple()
        )

        # --- Determine user's permissions ---
        user = interaction.user
        guild = interaction.guild

        is_admin = (
            user.guild_permissions.administrator
            or (ADMINS_ROLE_ID and discord.utils.get(user.roles, id=ADMINS_ROLE_ID))
        )
        is_captain = CAPTAINS_ROLE_ID and discord.utils.get(user.roles, id=CAPTAINS_ROLE_ID)

        visible_commands = []

        for command in self.bot.tree.get_commands():
            # --- Detect Admin-only or Captain-only checks ---
            command_checks = getattr(command, "checks", [])
            check_strings = [getattr(check, "__qualname__", "") for check in command_checks]

            # Simple detection of which role a command is tied to
            is_admin_only = "has_role.<locals>.predicate" in str(command_checks) and "Admins" in str(command_checks)
            is_captain_only = "has_role.<locals>.predicate" in str(command_checks) and "Captains" in str(command_checks)

            # --- Apply visibility rules ---
            if is_admin_only and not is_admin:
                continue
            if is_captain_only and not (is_admin or is_captain):
                continue

            # --- Mark with icons ---
            if is_admin_only:
                icon = "🔒 "
            elif is_captain_only:
                icon = "⚓ "
            else:
                icon = ""

            name = f"{icon}/{command.name}"
            desc = command.description or "No description provided."
            visible_commands.append((name, desc))

        # --- Sort alphabetically for neatness ---
        visible_commands.sort(key=lambda x: x[0].lower())

        # --- Add commands to embed ---
        for name, desc in visible_commands:
            embed.add_field(name=name, value=desc, inline=False)

        # --- Add emoji legend ---
        legend = "🔒 = Admin-only | ⚓ = Captain-only | 🕓 = Cooldown (8s for non-admins)"
        embed.add_field(name="Legend", value=legend, inline=False)

        # --- Footer text ---
        if is_admin:
            embed.set_footer(text="You have access to all commands.")
        elif is_captain:
            embed.set_footer(text="⚓ Captain access: some admin commands hidden.")
        else:
            embed.set_footer(text="Only showing commands available to you.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
