"""Audit log helper.

Posts structured events to a guild's configured audit channel
(`guilds.audit_channel` in the DB — a channel id, set via `/setup`).
Callers that already have the id in hand should pass `channel_id`
directly; `channel_name` is a fallback lookup (emoji-prefix-tolerant,
via `channel_util`) for callers that only know a semantic name.

Two tiers: `post_event` (things staff should notice) and
`post_dump_event` (routine bot activity worth a trail but not
attention). Both currently land in the same configured channel — there's
only one `audit_channel` column per guild today — but callers should
still pick the right tier so a future per-tier channel split is a config
change, not a call-site rewrite.

If the target channel can't be resolved or posted to, the call no-ops —
audit logging is best-effort and must never break a user-facing flow.
"""

from __future__ import annotations

import logging

import discord

from . import channel_util

log = logging.getLogger(__name__)


def _resolve_channel(
    guild: discord.Guild,
    *,
    channel_id: int | None,
    channel_name: str | None,
) -> discord.TextChannel | None:
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None
    if channel_name is not None:
        return channel_util.find_text_channel(guild, channel_name)
    return None


async def post_event(
    guild: discord.Guild | None,
    *,
    title: str,
    colour: discord.Colour,
    fields: list[tuple[str, str, bool]] | None = None,
    description: str | None = None,
    channel_id: int | None = None,
    channel_name: str | None = None,
    dump: bool = False,
) -> None:
    """Best-effort post to the guild's audit channel. Pass `channel_id`
    (from `guilds.audit_channel`) when available — that's the normal
    path. `channel_name` is a fallback lookup for callers without a
    stored id. `dump=True` marks this as a routine/low-priority event
    (currently cosmetic — see module docstring); pass it via
    `post_dump_event` rather than setting it directly."""
    if guild is None:
        return
    if channel_id is None and channel_name is None:
        return
    channel = _resolve_channel(guild, channel_id=channel_id, channel_name=channel_name)
    if channel is None:
        return
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=discord.utils.utcnow(),
    )
    if dump:
        embed.set_footer(text="routine")
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(
            "audit log post failed in guild %s (channel_id=%s, name=%s): %s",
            guild.id,
            channel_id,
            channel_name,
            e,
        )


async def post_dump_event(guild: discord.Guild | None, **kwargs) -> None:
    """Convenience wrapper: routine/low-priority audit event."""
    await post_event(guild, dump=True, **kwargs)


async def notify_user_dm(
    member: discord.Member | None,
    *,
    title: str,
    description: str,
    colour: discord.Colour | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
) -> bool:
    """Best-effort DM to a user about an action that affects them (rank
    change, team assignment, event reminder, admin override, etc).

    Returns True if delivered, False if the user has DMs closed, has
    blocked the bot, or shares no mutual server. Failures are logged at
    info level — never raised — so the calling command always succeeds
    even when the DM can't be opened. Use the return value to surface
    "(DM'd)" / "(couldn't DM)" to whoever triggered the action.
    """
    if member is None:
        return False
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour or discord.Colour.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.info("DM notify failed for %s: %s", member.id, e)
        return False
