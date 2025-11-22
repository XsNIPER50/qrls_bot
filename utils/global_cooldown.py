import time
import discord
import os
from dotenv import load_dotenv

# ✅ Load .env
load_dotenv()
ADMINS_ROLE_ID = int(os.getenv("ADMINS_ROLE_ID", 0))

THROTTLE_SECONDS = 8.0
_last_use_by_user: dict[int, float] = {}


def is_admin_user(user: discord.Member) -> bool:
    """Checks both for Admin role ID (.env) and Administrator permission."""
    if not user.guild:
        return False

    # ✅ Check for Administrator permission
    if getattr(user, "guild_permissions", None) and user.guild_permissions.administrator:
        return True

    # ✅ Check for Admin role by ID
    if ADMINS_ROLE_ID and discord.utils.get(getattr(user, "roles", []), id=ADMINS_ROLE_ID):
        return True

    return False


async def check_cooldown(interaction: discord.Interaction) -> bool:
    """Called by each command to enforce cooldown per user."""
    if is_admin_user(interaction.user):
        return True  # Admins bypass cooldown

    uid = interaction.user.id
    now = time.monotonic()
    last = _last_use_by_user.get(uid, 0.0)

    if now - last < THROTTLE_SECONDS:
        wait_for = THROTTLE_SECONDS - (now - last)
        await interaction.response.send_message(
            f"⏳ You’re using commands too quickly! Please wait **{wait_for:.1f} seconds**.",
            ephemeral=True
        )
        return False  # stop execution

    _last_use_by_user[uid] = now
    return True
