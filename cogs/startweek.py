import os
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from utils.schedule import SCHEDULE
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))


class StartWeek(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="startweek",
        description="Creates scheduling channels for the specified week number."
    )
    @app_commands.describe(
        week_number="Enter the week number (1–10) to create channels for."
    )
    async def start_week(self, interaction: Interaction, week_number: int):
        # ✅ Defer response to prevent timeout
        await interaction.response.defer(ephemeral=True)

        # --- Permission check (Admin only) ---
        member = interaction.user
        if not (
            member.guild_permissions.administrator
            or (ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID))
        ):
            await interaction.followup.send("🚫 You don’t have permission to use this command.", ephemeral=True)
            return

        # --- Validate the week number ---
        if week_number not in SCHEDULE:
            await interaction.followup.send(f"❌ No schedule found for week **{week_number}**.")
            return

        guild = interaction.guild
        category_name = "----Scheduling----"
        category = discord.utils.get(guild.categories, name=category_name)
        if not category:
            category = await guild.create_category(category_name)

        matches = SCHEDULE[week_number]
        created_channels = []

        # --- Locate Captains role using .env ID ---
        captains_role = discord.utils.get(guild.roles, id=CAPTAINS_ROLE_ID)

        # --- Create matchup channels ---
        for team_a, team_b in matches:
            channel_name = (
                f"week{week_number}-{team_a.lower().replace(' ', '-')}-vs-{team_b.lower().replace(' ', '-')}"
            )

            # Skip existing channels
            if discord.utils.get(category.text_channels, name=channel_name):
                continue

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False)
            }

            role_a = discord.utils.get(guild.roles, name=team_a)
            role_b = discord.utils.get(guild.roles, name=team_b)

            if role_a:
                overwrites[role_a] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            if role_b:
                overwrites[role_b] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            # ✅ Create the new scheduling channel
            new_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Week {week_number} matchup setup"
            )
            created_channels.append(new_channel.name)

            # --- Embed message setup ---
            embed = discord.Embed(
                title=f"📅 Week {week_number} Scheduling",
                description=f"This is your scheduling channel for **Week {week_number}**.",
                color=discord.Color.blue()
            )
            embed.add_field(name="🏆 Matchup", value=f"**{team_a}** vs **{team_b}**", inline=False)
            embed.set_footer(text="Please confirm your match time before the deadline.")

            # --- Ping captains properly (using AllowedMentions) ---
            allowed_mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)

            if captains_role:
                await new_channel.send(
                    content=f"{captains_role.mention} — This is your scheduling channel for Week {week_number}.",
                    allowed_mentions=allowed_mentions
                )
            else:
                await new_channel.send(
                    content=f"@Captains — This is your scheduling channel for Week {week_number}."
                )

            # --- Send the scheduling embed ---
            await new_channel.send(embed=embed)

        # --- Confirmation feedback ---
        if created_channels:
            formatted = "\n".join(f"• {c}" for c in created_channels)
            await interaction.followup.send(
                f"✅ Created {len(created_channels)} channel(s) for **Week {week_number}**:\n{formatted}"
            )
        else:
            await interaction.followup.send(f"ℹ️ All Week {week_number} channels already exist.")


async def setup(bot):
    await bot.add_cog(StartWeek(bot))
