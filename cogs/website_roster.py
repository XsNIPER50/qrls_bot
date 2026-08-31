"""Supabase-authoritative /add, /drop, and /trade commands."""

import logging
import os
from typing import Any

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from utils.website_roster import RosterAPIError, WebsiteRosterClient

logger = logging.getLogger("qrls.website_roster")


def env_int(name: str) -> int:
    try:
        return int(os.getenv(name, "0"))
    except ValueError:
        return 0


class WebsiteRoster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.website = WebsiteRosterClient()
        self.captains_role_id = env_int("CAPTAINS_ROLE_ID")
        self.admins_role_id = env_int("ADMINS_ROLE_ID")
        self.transactions_category_id = env_int("TRANSACTIONS_CATEGORY_ID")
        self.pending_channel_id = env_int("PENDING_TRANSACTIONS_CHANNEL_ID")
        self.transactions_channel_id = env_int("TRANSACTIONS_CHANNEL_ID")

    def is_captain(self, member: discord.Member) -> bool:
        return bool(self.captains_role_id and discord.utils.get(member.roles, id=self.captains_role_id))

    def is_admin(self, member: discord.Member) -> bool:
        return bool(
            member.guild_permissions.administrator
            or (self.admins_role_id and discord.utils.get(member.roles, id=self.admins_role_id))
        )

    async def command_channel(self, interaction: Interaction) -> discord.TextChannel | None:
        channel = interaction.channel
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(base, discord.TextChannel):
            await interaction.followup.send("❌ This command must be used in a text channel.", ephemeral=True)
            return None
        if not self.transactions_category_id or base.category_id != self.transactions_category_id:
            await interaction.followup.send("🚫 This command can only be used in the Transactions category.", ephemeral=True)
            return None
        return base

    async def transaction_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(self.transactions_channel_id) if self.transactions_channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def apply_role_changes(self, guild: discord.Guild, changes: list[dict]) -> list[str]:
        issues = []
        for change in changes:
            try:
                member = guild.get_member(int(change["discordId"])) or await guild.fetch_member(int(change["discordId"]))
                remove_id, add_id = change.get("removeRoleId"), change.get("addRoleId")
                remove_role = guild.get_role(int(remove_id)) if remove_id else None
                add_role = guild.get_role(int(add_id)) if add_id else None
                if remove_id and remove_role is None:
                    issues.append(f"Role {remove_id} was not found for {member.mention}.")
                elif remove_role and remove_role in member.roles:
                    await member.remove_roles(remove_role, reason="QRLS website roster transaction committed")
                if add_id and add_role is None:
                    issues.append(f"Role {add_id} was not found for {member.mention}.")
                elif add_role and add_role not in member.roles:
                    await member.add_roles(add_role, reason="QRLS website roster transaction committed")
            except (KeyError, TypeError, ValueError, discord.HTTPException) as error:
                logger.exception("Discord role reconciliation failed after roster commit")
                issues.append(f"Could not update roles for player {change.get('discordId', 'unknown')}: {error}")
        return issues

    async def post_completed(self, guild: discord.Guild, result: dict) -> None:
        channel = await self.transaction_channel()
        if channel is None:
            raise RuntimeError("TRANSACTIONS_CHANNEL_ID does not resolve to a text channel.")
        summary = result.get("summary") or {}
        kind = summary.get("type")
        allowed = discord.AllowedMentions(roles=True, users=True, everyone=False)
        if kind in {"add", "drop"}:
            change = (result.get("changes") or [{}])[0]
            player = f"<@{change.get('discordId')}>"
            from_team, to_team = summary.get("fromTeam", "Unknown"), summary.get("toTeam", "Unknown")
            from_role = discord.utils.get(guild.roles, name=from_team)
            to_role = discord.utils.get(guild.roles, name=to_team)
            if kind == "add":
                team = to_role.mention if to_role else f"**{to_team}**"
                message = f"{team} adds {player} to their roster from Free Agency."
            else:
                team = from_role.mention if from_role else f"**{from_team}**"
                message = f"{team} drops {player} to **{to_team}**."
        elif kind == "trade":
            changes = result.get("changes") or []
            if len(changes) != 2:
                raise RuntimeError("Completed trade response is missing role changes.")
            team_one, team_two = summary.get("teamOne", "Unknown"), summary.get("teamTwo", "Unknown")
            role_one = discord.utils.get(guild.roles, name=team_one)
            role_two = discord.utils.get(guild.roles, name=team_two)
            one = role_one.mention if role_one else f"@{team_one}"
            two = role_two.mention if role_two else f"@{team_two}"
            message = f"{one} trades <@{changes[0]['discordId']}> to {two} for <@{changes[1]['discordId']}>"
        else:
            raise RuntimeError("Completed roster response is missing its transaction summary.")
        await channel.send(message, allowed_mentions=allowed)

    async def finish_commit(self, interaction: Interaction, result: dict) -> list[str]:
        changes = result.get("changes")
        if not isinstance(changes, list):
            raise RuntimeError("Website roster response is missing Discord role changes.")
        try:
            issues = await self.apply_role_changes(interaction.guild, changes)
        except Exception as error:
            logger.exception("Discord role reconciliation failed after roster commit")
            issues = [f"Discord role reconciliation failed: {error}"]
        try:
            await self.post_completed(interaction.guild, result)
        except Exception as error:
            logger.exception("Completed roster transaction could not be posted")
            issues.append(str(error))
        return issues

    async def immediate(self, interaction: Interaction, player: discord.Member, action: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not self.is_captain(interaction.user):
            await interaction.followup.send("🚫 Only captains can use this command.", ephemeral=True)
            return
        if await self.command_channel(interaction) is None:
            return
        if await self.transaction_channel() is None:
            await interaction.followup.send("❌ TRANSACTIONS_CHANNEL_ID is missing or invalid.", ephemeral=True)
            return
        try:
            result = await self.website.add_or_drop(
                action,
                actor_id=interaction.user.id,
                player_id=player.id,
                request_key=f"discord:{interaction.id}:{action}",
            )
        except RosterAPIError as error:
            await interaction.followup.send(f"❌ {error.message}", ephemeral=True)
            return
        issues = await self.finish_commit(interaction, result)
        summary = result["summary"]
        message = f"✅ {player.mention} moved from **{summary['fromTeam']}** to **{summary['toTeam']}**."
        if issues:
            message += "\n⚠️ Database committed, but Discord reconciliation needs attention:\n" + "\n".join(f"• {item}" for item in issues)
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="add", description="Immediately add a Free Agent to your team.")
    @app_commands.guild_only()
    async def add(self, interaction: Interaction, player1: discord.Member):
        await self.immediate(interaction, player1, "add")

    @app_commands.command(name="drop", description="Immediately drop a player from your team.")
    @app_commands.guild_only()
    async def drop(self, interaction: Interaction, player1: discord.Member):
        await self.immediate(interaction, player1, "drop")

    async def grant_channel_access(self, channel: discord.TextChannel, member: discord.Member) -> None:
        overwrite = channel.overwrites_for(member)
        overwrite.view_channel = True
        overwrite.read_message_history = True
        overwrite.send_messages = True
        overwrite.embed_links = True
        await channel.set_permissions(member, overwrite=overwrite, reason="QRLS trade approval access")

    class DeclineModal(discord.ui.Modal, title="Decline Trade"):
        reason = discord.ui.TextInput(label="Reason", min_length=3, max_length=1000, style=discord.TextStyle.paragraph)

        def __init__(self, view: "WebsiteRoster.TradeDecisionView"):
            super().__init__()
            self.decision_view = view

        async def on_submit(self, interaction: Interaction):
            await self.decision_view.decide(interaction, "decline", str(self.reason))

    class TradeDecisionView(discord.ui.View):
        def __init__(self, *, cog: "WebsiteRoster", stage: str, transaction_id: str, actor_id: int, origin_channel_id: int):
            super().__init__(timeout=48 * 60 * 60)
            self.cog, self.stage, self.transaction_id = cog, stage, transaction_id
            self.actor_id, self.origin_channel_id, self.decided = actor_id, origin_channel_id, False

        async def interaction_check(self, interaction: Interaction) -> bool:
            allowed = isinstance(interaction.user, discord.Member) and (
                interaction.user.id == self.actor_id if self.stage == "captain" else self.cog.is_admin(interaction.user)
            )
            if self.decided or not allowed:
                await interaction.response.send_message("🚫 You cannot decide this trade.", ephemeral=True)
                return False
            return True

        async def finalize(self, interaction: Interaction, content: str) -> None:
            self.decided = True
            for item in self.children:
                item.disabled = True
            if interaction.message:
                await interaction.message.edit(content=content, view=self)

        async def decide(self, interaction: Interaction, decision: str, reason: str = "") -> None:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            try:
                result = await self.cog.website.decide_trade(
                    self.stage,
                    actor_id=interaction.user.id,
                    transaction_id=self.transaction_id,
                    decision=decision,
                    reason=reason,
                    request_key=f"discord:{interaction.id}:trade-{self.stage}-{decision}",
                )
            except RosterAPIError as error:
                await interaction.followup.send(f"❌ {error.message}", ephemeral=True)
                return
            if decision == "decline":
                await self.finalize(interaction, f"🚫 Trade declined by {interaction.user.mention}: {reason}")
                await interaction.followup.send("🚫 Trade declined.", ephemeral=True)
                return
            if self.stage == "captain":
                pending = self.cog.bot.get_channel(self.cog.pending_channel_id)
                if not isinstance(pending, discord.TextChannel):
                    pending = self.cog.bot.get_channel(self.origin_channel_id)
                if not isinstance(pending, discord.TextChannel):
                    await self.finalize(interaction, "⚠️ Captain approved, but no QRLS Admin approval channel is available.")
                    await interaction.followup.send("⚠️ Trade is pending QRLS Admin approval, but no approval channel is available.", ephemeral=True)
                    return
                view = WebsiteRoster.TradeDecisionView(
                    cog=self.cog, stage="admin", transaction_id=self.transaction_id,
                    actor_id=0, origin_channel_id=self.origin_channel_id,
                )
                await pending.send(
                    content=f"<@&{self.cog.admins_role_id}> **Pending Trade Request**\nTransaction: `{self.transaction_id}`\nOpposing captain approved: {interaction.user.mention}",
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                    view=view,
                )
                await self.finalize(interaction, f"✅ Approved by {interaction.user.mention} — pending QRLS Admin approval.")
                await interaction.followup.send("✅ Approved. Sent to QRLS Admins.", ephemeral=True)
                return
            issues = await self.cog.finish_commit(interaction, result)
            origin = self.cog.bot.get_channel(self.origin_channel_id)
            if isinstance(origin, discord.TextChannel):
                suffix = "" if not issues else "\n⚠️ " + " | ".join(issues)
                await origin.send(f"✅ Trade approved by {interaction.user.mention}.{suffix}")
            await self.finalize(interaction, f"✅ Trade completed by {interaction.user.mention}.")
            await interaction.followup.send("✅ Trade committed and Discord roles reconciled." if not issues else "⚠️ Trade committed with Discord reconciliation issues.", ephemeral=True)

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
        async def approve(self, interaction: Interaction, button: discord.ui.Button):
            await self.decide(interaction, "approve")

        @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: Interaction, button: discord.ui.Button):
            await interaction.response.send_modal(WebsiteRoster.DeclineModal(self))

    @app_commands.command(name="trade", description="Request a trade requiring opposing captain and QRLS Admin approval.")
    @app_commands.guild_only()
    async def trade(self, interaction: Interaction, player1: discord.Member, player2: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not self.is_captain(interaction.user):
            await interaction.followup.send("🚫 Only captains can use this command.", ephemeral=True)
            return
        origin = await self.command_channel(interaction)
        if origin is None:
            return
        if not self.pending_channel_id or not self.admins_role_id or await self.transaction_channel() is None:
            await interaction.followup.send("❌ Trade approval or transaction channels are not configured.", ephemeral=True)
            return
        if player1.id == player2.id:
            await interaction.followup.send("🚫 You cannot trade a player for themselves.", ephemeral=True)
            return
        try:
            result = await self.website.create_trade(
                actor_id=interaction.user.id,
                own_player_id=player1.id,
                other_player_id=player2.id,
                request_key=f"discord:{interaction.id}:trade-create",
            )
        except RosterAPIError as error:
            await interaction.followup.send(f"❌ {error.message}", ephemeral=True)
            return
        transaction, summary = result["transaction"], result["summary"]
        opposing_id = int(transaction["opposing_captain_discord_user_id"])
        try:
            opposing = interaction.guild.get_member(opposing_id) or await interaction.guild.fetch_member(opposing_id)
            await self.grant_channel_access(origin, opposing)
        except discord.HTTPException as error:
            await interaction.followup.send(f"⚠️ Trade created in the database, but opposing captain access failed: {error}", ephemeral=True)
            return
        captains = interaction.guild.get_role(self.captains_role_id)
        await origin.send(
            f"{captains.mention if captains else '@Captains'} — A trade has been proposed and needs opposing captain approval.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
        embed = discord.Embed(
            title="🔁 Trade Proposed",
            description=(f"Requested by: {interaction.user.mention}\n\nTrade request:\n"
                         f"**{player1.mention}** (from **{summary['teamOne']}**) ↔ **{player2.mention}** (from **{summary['teamTwo']}**)\n\n"
                         f"Opposing captain to approve: {opposing.mention}"),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Only the opposing captain can approve/decline. QRLS Admin approval is required after that.")
        await origin.send(
            embed=embed,
            view=WebsiteRoster.TradeDecisionView(
                cog=self, stage="captain", transaction_id=transaction["id"],
                actor_id=opposing_id, origin_channel_id=origin.id,
            ),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
        await interaction.followup.send(f"✅ Trade created. Waiting on {opposing.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebsiteRoster(bot))
