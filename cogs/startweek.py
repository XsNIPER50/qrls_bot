import os
import csv
import logging
import traceback

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from utils.schedule import SCHEDULE
from dotenv import load_dotenv

load_dotenv()

ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

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
    @app_commands.guild_only()
    async def start_week(self, interaction: Interaction, week_number: int):
        step = "START"
        try:
            logger.info("Invoked /startweek %s by user_id=%s", week_number, getattr(interaction.user, "id", None))

            step = "DEFER"
            await interaction.response.defer(ephemeral=True)

            step = "GUILD_CHECK"
            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
                return

            # ---- Make sure we have a Member object (not just a User) ----
            step = "FETCH_MEMBER"
            member = interaction.user
            if not isinstance(member, discord.Member):
                try:
                    member = await guild.fetch_member(interaction.user.id)
                    logger.info("Fetched member from API: %s (%s)", member.name, member.id)
                except discord.NotFound:
                    logger.error("fetch_member: user not found in guild")
                    await interaction.followup.send("❌ Could not find you as a server member.", ephemeral=True)
                    return
                except discord.Forbidden:
                    logger.error("fetch_member: missing permissions (Guild Members intent or perms)")
                    await interaction.followup.send(
                        "❌ Bot cannot fetch members (check Guild Members intent + permissions).",
                        ephemeral=True
                    )
                    return

            # ---- Permission check (Admin only) ----
            step = "PERMISSION_CHECK"
            has_admin_perm = getattr(member.guild_permissions, "administrator", False)
            has_admin_role = bool(ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID))

            logger.info("Perm check: admin_perm=%s admin_role=%s ADMINS_ROLE_ID=%s",
                        has_admin_perm, has_admin_role, ADMINS_ROLE_ID)

            if not (has_admin_perm or has_admin_role):
                await interaction.followup.send("🚫 You don’t have permission to use this command.", ephemeral=True)
                return

            # ---- Validate week number ----
            step = "SCHEDULE_LOOKUP"
            if week_number not in SCHEDULE:
                logger.warning("Week %s not in SCHEDULE. Keys=%s", week_number, list(SCHEDULE.keys()))
                await interaction.followup.send(f"❌ No schedule found for week **{week_number}**.", ephemeral=True)
                return

            matches = SCHEDULE[week_number]
            logger.info("Matches for week %s: %s", week_number, len(matches))

            # ---- Find/Create category ----
            step = "CATEGORY_LOOKUP"
            category_name = "╭────Scheduling────╮"
            category = discord.utils.get(guild.categories, name=category_name)
            if not category:
                step = "CATEGORY_CREATE"
                logger.info("Creating category: %s", category_name)
                category = await guild.create_category(category_name)
            logger.info("Using category id=%s name=%s", category.id, category.name)

            # ---- Captains & Streamer roles lookup (safe) ----
            step = "ROLES_LOOKUP"
            logger.info("CAPTAINS_ROLE_ID=%s", CAPTAINS_ROLE_ID)
            captains_role = guild.get_role(CAPTAINS_ROLE_ID) if CAPTAINS_ROLE_ID else None
            logger.info("Captains role found=%s", bool(captains_role))

            # Streamer role by name (no pings, just perms)
            streamer_role = discord.utils.get(guild.roles, name="Streamer")
            logger.info("Streamer role found=%s", bool(streamer_role))

            created_channels = []

            for idx, (team_a, team_b) in enumerate(matches, start=1):
                step = "BUILD_CHANNEL_NAME"
                channel_name = (
                    f"week{week_number}-{team_a.lower().replace(' ', '-')}-vs-{team_b.lower().replace(' ', '-')}"
                )

                step = "CHECK_EXISTING_CHANNEL"
                if discord.utils.get(category.text_channels, name=channel_name):
                    logger.info("Exists, skipping: %s", channel_name)
                    continue

                step = "BUILD_OVERWRITES"
                overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}

                role_a = discord.utils.get(guild.roles, name=team_a)
                role_b = discord.utils.get(guild.roles, name=team_b)

                logger.info("Team roles: %s=%s | %s=%s", team_a, bool(role_a), team_b, bool(role_b))

                if role_a:
                    overwrites[role_a] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                if role_b:
                    overwrites[role_b] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                # Streamer role read/send access in every scheduling channel
                if streamer_role:
                    overwrites[streamer_role] = discord.PermissionOverwrite(
                        read_messages=True,
                        send_messages=True
                    )

                step = "CREATE_CHANNEL"
                logger.info("Creating channel: %s", channel_name)
                new_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Week {week_number} matchup setup"
                )
                created_channels.append(new_channel.name)

                # ---- First message: ping captains + BOTH teams ----
                step = "SEND_PING"
                allowed_mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)

                team_a_mention = role_a.mention if role_a else f"@{team_a}"
                team_b_mention = role_b.mention if role_b else f"@{team_b}"

                if captains_role:
                    await new_channel.send(
                        content=(
                            f"{captains_role.mention} — {team_a_mention} vs {team_b_mention} — "
                            f"This is your scheduling channel for Week {week_number}."
                        ),
                        allowed_mentions=allowed_mentions
                    )
                else:
                    await new_channel.send(
                        content=(
                            f"@Captains — {team_a_mention} vs {team_b_mention} — "
                            f"This is your scheduling channel for Week {week_number}."
                        ),
                        allowed_mentions=allowed_mentions
                    )

                step = "SEND_EMBED"
                embed_description = (
                    "This is your scheduling channel for round 1 of the preseason tournament."
                    if week_number in (21, 22, 23, 24)
                    else f"This is your scheduling channel for **Week {week_number}**."
                )

                embed = discord.Embed(
                    title=f"📅 Week {week_number} Scheduling",
                    description=embed_description,
                    color=discord.Color.blue()
                )
                embed.add_field(name="🏆 Matchup", value=f"**{team_a}** vs **{team_b}**", inline=False)
                embed.set_footer(
                    text=(
                        "Please confirm your match time before the deadline. "
                        "Please use /propose to propose a time and /confirm to confirm the proposed time."
                    )
                )
                await new_channel.send(embed=embed)

            step = "FINAL_RESPONSE"
            if created_channels:
                formatted = "\n".join(f"• {c}" for c in created_channels)
                await interaction.followup.send(
                    f"✅ Created {len(created_channels)} channel(s) for **Week {week_number}**:\n{formatted}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"ℹ️ All Week {week_number} channels already exist.",
                    ephemeral=True
                )

        except Exception as e:
            logger.error("ERROR at step=%s: %r", step, e)
            traceback.print_exc()
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        f"❌ /startweek failed at step: **{step}** (check bot console for traceback).",
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        f"❌ /startweek failed at step: **{step}** (check bot console for traceback).",
                        ephemeral=True
                    )
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(StartWeek(bot))
