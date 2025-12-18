import os
import logging
import traceback

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from utils.schedule import SCHEDULE
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

# ✅ Basic logger (prints to console)
logger = logging.getLogger("qrls.startweek")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


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
        step = "START"
        try:
            logger.info("Command invoked: /startweek %s by %s (%s)",
                        week_number,
                        getattr(interaction.user, "name", "unknown"),
                        getattr(interaction.user, "id", "unknown"))

            # ✅ Defer response to prevent timeout
            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            # --- Validate guild context ---
            step = "GUILD_CHECK"
            guild = interaction.guild
            if guild is None:
                logger.warning("No guild found on interaction (DM?)")
                await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
                return

            # --- Permission check (Admin only) ---
            step = "PERMISSION_CHECK"
            member = interaction.user
            has_admin_role = bool(ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID))
            has_admin_perm = getattr(member.guild_permissions, "administrator", False)

            logger.info("Permission check: admin_perm=%s admin_role=%s (ADMINS_ROLE_ID=%s)",
                        has_admin_perm, has_admin_role, ADMINS_ROLE_ID)

            if not (has_admin_perm or has_admin_role):
                await interaction.followup.send("🚫 You don’t have permission to use this command.", ephemeral=True)
                return

            # --- Validate the week number ---
            step = "SCHEDULE_LOOKUP"
            if week_number not in SCHEDULE:
                logger.warning("Week %s not found in SCHEDULE keys=%s", week_number, list(SCHEDULE.keys()))
                await interaction.followup.send(f"❌ No schedule found for week **{week_number}**.", ephemeral=True)
                return

            matches = SCHEDULE[week_number]
            logger.info("Found %s match(es) for week %s", len(matches), week_number)

            # --- Find/Create category ---
            step = "CATEGORY_LOOKUP"
            category_name = "----Scheduling----"
            category = discord.utils.get(guild.categories, name=category_name)

            if category:
                logger.info("Using existing category '%s' (id=%s)", category_name, category.id)
            else:
                step = "CATEGORY_CREATE"
                logger.info("Category '%s' not found. Creating...", category_name)
                category = await guild.create_category(category_name)
                logger.info("Created category '%s' (id=%s)", category_name, category.id)

            created_channels = []

            # --- Locate Captains role using .env ID ---
            step = "CAPTAINS_ROLE_LOOKUP"
            captains_role = discord.utils.get(guild.roles, id=CAPTAINS_ROLE_ID)
            logger.info("Captains role lookup: CAPTAINS_ROLE_ID=%s found=%s",
                        CAPTAINS_ROLE_ID, bool(captains_role))

            # --- Create matchup channels ---
            for idx, (team_a, team_b) in enumerate(matches, start=1):
                step = "LOOP_MATCH"
                logger.info("Match %s/%s: %s vs %s", idx, len(matches), team_a, team_b)

                channel_name = (
                    f"week{week_number}-{team_a.lower().replace(' ', '-')}-vs-{team_b.lower().replace(' ', '-')}"
                )

                # Skip existing channels
                step = "CHECK_EXISTING_CHANNEL"
                if discord.utils.get(category.text_channels, name=channel_name):
                    logger.info("Channel exists, skipping: %s", channel_name)
                    continue

                step = "BUILD_OVERWRITES"
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False)
                }

                role_a = discord.utils.get(guild.roles, name=team_a)
                role_b = discord.utils.get(guild.roles, name=team_b)

                logger.info("Role lookup: '%s' found=%s | '%s' found=%s",
                            team_a, bool(role_a), team_b, bool(role_b))

                if role_a:
                    overwrites[role_a] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                if role_b:
                    overwrites[role_b] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                # ✅ Create the new scheduling channel
                step = "CREATE_CHANNEL"
                logger.info("Creating channel: %s", channel_name)
                new_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Week {week_number} matchup setup"
                )
                created_channels.append(new_channel.name)
                logger.info("Created channel: %s (id=%s)", new_channel.name, new_channel.id)

                # --- Embed message setup ---
                step = "BUILD_EMBED"
                embed = discord.Embed(
                    title=f"📅 Week {week_number} Scheduling",
                    description=f"This is your scheduling channel for **Week {week_number}**.",
                    color=discord.Color.blue()
                )
                embed.add_field(name="🏆 Matchup", value=f"**{team_a}** vs **{team_b}**", inline=False)
                embed.set_footer(text="Please confirm your match time before the deadline.")

                # --- Ping captains properly (using AllowedMentions) ---
                step = "SEND_CAPTAINS_PING"
                allowed_mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)

                if captains_role:
                    await new_channel.send(
                        content=f"{captains_role.mention} — This is your scheduling channel for Week {week_number}.",
                        allowed_mentions=allowed_mentions
                    )
                    logger.info("Sent captains ping in %s", new_channel.name)
                else:
                    await new_channel.send(
                        content=f"@Captains — This is your scheduling channel for Week {week_number}."
                    )
                    logger.warning("Captains role not found; sent plain text @Captains in %s", new_channel.name)

                # --- Send the scheduling embed ---
                step = "SEND_EMBED"
                await new_channel.send(embed=embed)
                logger.info("Sent embed in %s", new_channel.name)

            # --- Confirmation feedback ---
            step = "FINAL_RESPONSE"
            if created_channels:
                formatted = "\n".join(f"• {c}" for c in created_channels)
                await interaction.followup.send(
                    f"✅ Created {len(created_channels)} channel(s) for **Week {week_number}**:\n{formatted}",
                    ephemeral=True
                )
                logger.info("Completed: created %s channel(s) for week %s", len(created_channels), week_number)
            else:
                await interaction.followup.send(
                    f"ℹ️ All Week {week_number} channels already exist.",
                    ephemeral=True
                )
                logger.info("Completed: no new channels needed for week %s", week_number)

        except Exception as e:
            # Full traceback to console
            logger.error("ERROR at step=%s: %s", step, repr(e))
            traceback.print_exc()

            # Try to notify user (ephemeral)
            msg = f"❌ /startweek failed at step: **{step}** (check bot console for traceback)."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                # If even that fails, at least we logged the traceback.
                pass


async def setup(bot):
    await bot.add_cog(StartWeek(bot))
