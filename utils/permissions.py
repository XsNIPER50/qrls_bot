# utils/permissions.py
import os
from dotenv import load_dotenv
import discord

# ✅ Load .env
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))
CAPTAINS_ROLE_ID = int(os.getenv("CAPTAINS_ROLE_ID", 0))


async def has_allowed_role(interaction: discord.Interaction, allowed_roles: set[str]) -> bool:
    """
    Check if the user running the command has any of the allowed roles.
    Supports both role name checks and .env-based role IDs for Admins/Captains.
    """
    member = interaction.user

    # --- Always trust admin permission first ---
    if member.guild_permissions.administrator:
        return True

    # --- Check .env-based roles ---
    if ADMINS_ROLE_ID and discord.utils.get(member.roles, id=ADMINS_ROLE_ID):
        return True
    if CAPTAINS_ROLE_ID and discord.utils.get(member.roles, id=CAPTAINS_ROLE_ID):
        return True

    # --- Fallback: match by role name if provided in allowed_roles ---
    user_roles = {role.name for role in member.roles}
    if allowed_roles.intersection(user_roles):
        return True

    return False
