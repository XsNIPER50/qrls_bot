import os
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from typing import Optional
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))


class ConfirmClearView(discord.ui.View):
    def __init__(self, week_number: Optional[int], category: discord.CategoryChannel, user: discord.Member):
        super().__init__(timeout=60) # 1hr
        self.week_number = week_number
        self.category = category
        self.user = user
        self.value = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🚫 This confirmation isn’t for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        deleted = 0
        reason = (
            f"Clearing week {self.week_number} scheduling channels"
            if self.week_number else "Clearing all scheduling channels"
        )

        for channel in list(self.category.text_channels):
            if self.week_number:
                if channel.name.startswith(f"week{self.week_number}-"):
                    await channel.delete(reason=reason)
                    deleted += 1
            else:
                await channel.delete(reason=reason)
                deleted += 1

        if deleted > 0:
            await interaction.followup.send(
                f"✅ Deleted **{deleted}** channel(s){' for week ' + str(self.week_number) if self.week_number else ''}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ No week {self.week_number} channels found." if self.week_number else "❌ No channels found to delete.",
                ephemeral=True
            )

        self.value = True
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("🚫 Deletion cancelled.", ephemeral=True)
        self.value = False
        self.stop()


class ClearSchedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="clearschedule",
        description="Deletes scheduling channels. Optionally specify a week number to delete only that week's channels."
    )
    @app_commands.describe(week_number="Optional week number (e.g., 1) to delete only those channels.")
    async def clear_schedule(self, interaction: Interaction, week_number: Optional[int] = None):
        # ✅ Check for Admin or Captain role before continuing
        member = interaction.user
        has_permission = False

        if member.guild_permissions.administrator:
            has_permission = True
        elif ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
            has_permission = True
        elif CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID):
            has_permission = True

        if not has_permission:
            await interaction.response.send_message(
                "🚫 You don’t have permission to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        category = discord.utils.get(guild.categories, name="╭────Scheduling────╮")

        if not category:
            await interaction.followup.send("❌ No 'Scheduling Channel' category found.", ephemeral=True)
            return

        # Create confirmation view
        view = ConfirmClearView(week_number, category, interaction.user)
        message_text = (
            f"⚠️ Are you sure you want to delete **all Week {week_number}** scheduling channels?"
            if week_number
            else "⚠️ Are you sure you want to delete **all scheduling channels**?"
        )

        await interaction.followup.send(message_text, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ClearSchedule(bot))
