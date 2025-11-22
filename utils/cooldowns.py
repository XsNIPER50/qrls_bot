import discord
from discord import app_commands
import os
from dotenv import load_dotenv

# ✅ Load .env
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))

# --- Custom cooldown class ---
class GlobalCooldown:
    def __init__(self, rate: int = 1, per: float = 8.0):
        self.rate = rate
        self.per = per

    def __call__(self, interaction: discord.Interaction):
        """Applies per-user cooldown, skipping Admins or users with Admin role ID."""
        # Skip cooldown for Admin-permissioned users or those with the Admin role ID
        if interaction.user.guild_permissions.administrator:
            return None
        if ADMINS_ROLE_ID and discord.utils.get(interaction.user.roles, id=ADMINS_ROLE_ID):
            return None

        # Apply standard per-user cooldown
        return app_commands.Cooldown(
            rate=self.rate,
            per=self.per,
            type=app_commands.CooldownType.user
        )

# Instantiate global cooldown (8 seconds per user)
global_cooldown = GlobalCooldown(rate=1, per=8.0)
