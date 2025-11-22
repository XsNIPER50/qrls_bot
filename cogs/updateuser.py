import os
import csv
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from utils.global_cooldown import check_cooldown

# ✅ Load environment variables
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))

CSV_FILE = "data/salaries.csv"  # same file used by /salary


class UpdateUser(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="updateuser",
        description="(Admin only) Add or update a player's nickname, salary, and team."
    )
    @app_commands.describe(
        user="Mention the player to update",
        nickname="The player's nickname or display name",
        salary="The player's new salary (whole number only)",
        team="The team this player belongs to"
    )
    async def update_user(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        nickname: str,
        salary: int,
        team: str
    ):
        # --- Permission check (Admins only via role ID or permission) ---
        member = interaction.user
        if not (
            member.guild_permissions.administrator
            or (ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID))
        ):
            await interaction.response.send_message(
                "🚫 You don’t have permission to use this command.",
                ephemeral=True
            )
            return

        if not await check_cooldown(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)

            # If file doesn't exist yet, create it with headers
            if not os.path.exists(CSV_FILE):
                with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(
                        f,
                        fieldnames=["discord_id", "nickname", "salary", "team"]
                    )
                    writer.writeheader()

            discord_id = str(user.id)
            rows = []
            updated = False

            # --- Read and update existing rows ---
            with open(CSV_FILE, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["discord_id"] == discord_id:
                        row["nickname"] = nickname
                        row["salary"] = str(salary)
                        row["team"] = str(team)
                        updated = True
                    rows.append(row)

            # --- Add new record if not found ---
            if not updated:
                rows.append({
                    "discord_id": discord_id,
                    "nickname": nickname,
                    "salary": str(salary),
                    "team": str(team)
                })

            # --- Write back to CSV ---
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["discord_id", "nickname", "salary", "team"]
                )
                writer.writeheader()
                writer.writerows(rows)

            if updated:
                await interaction.followup.send(
                    f"✅ Updated **{nickname}** (<@{discord_id}>) — Salary: **${salary:,}**, Team: **{team}**."
                )
            else:
                await interaction.followup.send(
                    f"✅ Added **{nickname}** (<@{discord_id}>) — Salary: **${salary:,}**, Team: **{team}**."
                )

        except Exception as e:
            await interaction.followup.send(f"⚠️ Failed to update user: `{e}`", ephemeral=True)
            print("Error in /updateuser:", e)

    @update_user.error
    async def update_user_error(self, interaction: discord.Interaction, error):
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
        else:
            raise error


async def setup(bot):
    await bot.add_cog(UpdateUser(bot))
