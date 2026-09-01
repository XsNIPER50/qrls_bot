"""Background delivery of Discord role changes committed by the QRLS website."""

import asyncio
import logging
import os

import discord
from discord.ext import commands, tasks

from utils.website_roles import RoleSyncAPIError, WebsiteRolesClient

logger = logging.getLogger("qrls.role_sync")


def env_int(name: str) -> int:
    try:
        return int(os.getenv(name, "0"))
    except ValueError:
        return 0


def poll_seconds() -> float:
    try:
        return max(2.0, float(os.getenv("QRLS_ROLE_SYNC_POLL_SECONDS", "10")))
    except ValueError:
        return 10.0


def required_text(job: dict, key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Role-sync job is missing {key}.")
    return value.strip()


class RoleSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.website = WebsiteRolesClient()
        self.guild_id = env_int("GUILD_ID")
        self._poll_lock = asyncio.Lock()
        self._pending_completion: str | None = None

    async def cog_load(self) -> None:
        self.poll_role_sync.change_interval(seconds=poll_seconds())
        if not self.poll_role_sync.is_running():
            self.poll_role_sync.start()

    def cog_unload(self) -> None:
        self.poll_role_sync.cancel()

    async def apply(self, job: dict) -> None:
        if not self.guild_id:
            raise RuntimeError("GUILD_ID is missing or invalid.")
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise RuntimeError("Configured Discord guild is unavailable.")

        member_id = int(required_text(job, "discord_user_id"))
        member = guild.get_member(member_id)
        if member is None:
            member = await guild.fetch_member(member_id)

        remove_value = job.get("remove_role_id")
        add_value = job.get("add_role_id")
        remove_id = int(remove_value) if remove_value else None
        add_id = int(add_value) if add_value else None

        if remove_id:
            remove_role = guild.get_role(remove_id)
            if remove_role is None:
                raise RuntimeError(f"Discord role {remove_id} was not found.")
            if remove_role in member.roles:
                await member.remove_roles(remove_role, reason="QRLS website role-sync job")

        if add_id:
            add_role = guild.get_role(add_id)
            if add_role is None:
                raise RuntimeError(f"Discord role {add_id} was not found.")
            if add_role not in member.roles:
                await member.add_roles(add_role, reason="QRLS website role-sync job")

    async def _poll_once(self) -> None:
        if self._pending_completion:
            job_id = self._pending_completion
            await self.website.complete(job_id)
            self._pending_completion = None
            logger.info("Acknowledged Discord role-sync job %s", job_id)
            return

        job = await self.website.claim()
        if job is None:
            return
        job_id = required_text(job, "id")
        try:
            await self.apply(job)
        except Exception as error:
            safe_error = f"{type(error).__name__}: {error}"
            logger.exception("Discord role-sync job %s failed", job_id)
            try:
                await self.website.fail(job_id, safe_error)
                logger.info("Reported Discord role-sync job %s failure", job_id)
            except RoleSyncAPIError:
                logger.exception("Could not report Discord role-sync job %s failure", job_id)
            return

        self._pending_completion = job_id
        await self.website.complete(job_id)
        self._pending_completion = None
        logger.info("Applied and acknowledged Discord role-sync job %s", job_id)

    async def poll_once(self) -> None:
        if self._poll_lock.locked():
            return
        async with self._poll_lock:
            await self._poll_once()

    @tasks.loop(seconds=10.0)
    async def poll_role_sync(self) -> None:
        try:
            await self.poll_once()
        except Exception:
            logger.exception("Discord role-sync poll failed")

    @poll_role_sync.before_loop
    async def before_poll_role_sync(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleSync(bot))
