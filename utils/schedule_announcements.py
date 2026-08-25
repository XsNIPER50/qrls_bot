"""Discord announcement delivery shared by slash commands and website events."""

from datetime import datetime

import discord

from utils.scheduling import format_schedule_datetime
from utils.team_info import TEAM_INFO

SCHED_RESULTS_CHANNEL = "💥・scheduling"
SCHEDULED_MATCHES_CHANNEL = "📜・scheduled-matches"


def member_mention(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.mention if member else f"<@{user_id}>"


async def deliver_proposed_announcement(
    channel: discord.TextChannel,
    *,
    actor_id: int,
    scheduled_at: datetime,
    captains_role_id: int,
) -> None:
    allowed = discord.AllowedMentions(roles=True, users=True, everyone=False)
    captains = channel.guild.get_role(captains_role_id) if captains_role_id else None
    await channel.send(
        content=f"{captains.mention} — A match time has been proposed." if captains else "@Captains — A match time has been proposed.",
        allowed_mentions=allowed,
    )
    display = format_schedule_datetime(scheduled_at)
    embed = discord.Embed(
        title="📌 Proposed Match Time",
        description=f"**{member_mention(channel.guild, actor_id)}** proposed:\n**{display}**",
        color=discord.Color.gold(),
    )
    await channel.send(embed=embed, allowed_mentions=allowed)


async def deliver_confirmed_announcement(
    channel: discord.TextChannel,
    *,
    actor_id: int,
    proposer_id: int | None,
    scheduled_at: datetime,
    team_a: str,
    team_b: str,
    captains_role_id: int,
    strict_delivery: bool = True,
) -> None:
    guild = channel.guild
    allowed = discord.AllowedMentions(roles=True, users=True, everyone=False)
    schedule_channel = discord.utils.get(guild.text_channels, name=SCHED_RESULTS_CHANNEL)
    matches_channel = discord.utils.get(guild.text_channels, name=SCHEDULED_MATCHES_CHANNEL)
    if strict_delivery and (schedule_channel is None or matches_channel is None):
        raise RuntimeError("A required schedule announcement channel was not found.")
    actor = guild.get_member(actor_id)
    actor_mention = actor.mention if actor else f"<@{actor_id}>"
    proposer_mention = member_mention(guild, proposer_id) if proposer_id else "Unknown proposer"
    display = format_schedule_datetime(scheduled_at)

    if actor and actor.guild_permissions.administrator:
        role_label = "Admin"
    elif actor and captains_role_id and discord.utils.get(actor.roles, id=captains_role_id):
        role_label = "Captain"
    else:
        role_label = "Member"
    actor_name = actor.display_name if actor else str(actor_id)

    embed = discord.Embed(
        title="✅ Match Time Confirmed",
        description=f"**{display}** was proposed by {proposer_mention} and confirmed by {actor_mention}.",
        color=discord.Color.green(),
    )
    embed.add_field(name="🏆 Matchup", value=f"**{team_a}** vs **{team_b}**", inline=False)
    embed.add_field(name="🕒 Time", value=display, inline=True)
    embed.set_footer(text=f"Confirmed by {actor_name} ({role_label})")

    captains = guild.get_role(captains_role_id) if captains_role_id else None
    await channel.send(
        content=f"{captains.mention} — A match time has been confirmed." if captains else "@Captains — A match time has been confirmed.",
        allowed_mentions=allowed,
    )
    await channel.send(embed=embed, allowed_mentions=allowed)

    role_a = discord.utils.get(guild.roles, name=team_a)
    role_b = discord.utils.get(guild.roles, name=team_b)
    mention_a = role_a.mention if role_a else f"@{team_a}"
    mention_b = role_b.mention if role_b else f"@{team_b}"
    emoji_a_name = TEAM_INFO.get(team_a, {}).get("emoji", "")
    emoji_b_name = TEAM_INFO.get(team_b, {}).get("emoji", "")
    emoji_a = discord.utils.get(guild.emojis, name=emoji_a_name)
    emoji_b = discord.utils.get(guild.emojis, name=emoji_b_name)
    emoji_a_text = str(emoji_a) if emoji_a else (f":{emoji_a_name}:" if emoji_a_name else "")
    emoji_b_text = str(emoji_b) if emoji_b else (f":{emoji_b_name}:" if emoji_b_name else "")
    message = f"{emoji_a_text} {mention_a} vs {mention_b} {emoji_b_text} — {display}"

    for destination in (schedule_channel, matches_channel):
        if destination is None:
            if strict_delivery:
                raise RuntimeError("A required schedule announcement channel was not found.")
            continue
        sent = await destination.send(message, allowed_mentions=allowed)
        if destination.name == SCHEDULED_MATCHES_CHANNEL:
            try:
                await sent.add_reaction("🎙️")
                await sent.add_reaction("🎥")
            except discord.HTTPException:
                if strict_delivery:
                    raise
