"""Incident logging, audit-channel notes, and rate limiting.

These are module-level helpers (not just Cog methods) because roles/teams/
setup all need them for their own app_commands.check decorators. Only this
module reaches into services.ratelimit — no other cog should build its own
limiter instance.
"""

from __future__ import annotations

import logging

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from ..services import ratelimit as ratelimit_service

log = logging.getLogger(__name__)

# 5-call burst, then one refill every 12s (~5/min steady state) per
# (guild, user, command). Deliberately generous — this guards against abuse
# and misclicks, not normal use.
_LIMITER = ratelimit_service.TokenBucketLimiter(capacity=5, refill_seconds=12.0)


async def fetchone(db: aiosqlite.Connection, query: str, params: tuple = ()) -> aiosqlite.Row | None:
    async with db.execute(query, params) as cur:
        return await cur.fetchone()


async def minimal_mode(db: aiosqlite.Connection, guild_id: int) -> bool:
    row = await fetchone(db, "SELECT minimal_mode FROM guilds WHERE guild_id = ?", (guild_id,))
    return bool(row["minimal_mode"]) if row else False


async def note_audit(bot: commands.Bot, guild_id: int, message: str) -> None:
    """Post a terse line to the guild's configured audit channel, if any.
    Never raises — audit notes are best-effort."""
    assert bot.db is not None
    row = await fetchone(bot.db, "SELECT audit_channel FROM guilds WHERE guild_id = ?", (guild_id,))
    if row is None or row["audit_channel"] is None:
        return
    channel = bot.get_channel(row["audit_channel"])
    if channel is None:
        return
    try:
        await channel.send(message)
    except discord.HTTPException:
        log.warning("Failed to post audit note to channel %s in guild %s", row["audit_channel"], guild_id)


async def log_incident(bot: commands.Bot, guild_id: int, user_id: int, kind: str, detail: str = "") -> None:
    """Record a permission-denial or rate-limit hit, and mirror it to the
    audit channel if one is configured."""
    assert bot.db is not None
    await bot.db.execute(
        "INSERT INTO incidents (guild_id, user_id, kind, detail) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, kind, detail),
    )
    await bot.db.commit()
    if kind == "permission_denied":
        await note_audit(bot, guild_id, f"Permission denied: <@{user_id}> tried `{detail}`.")
    elif kind == "rate_limit":
        await note_audit(bot, guild_id, f"Rate limit hit: <@{user_id}> on `{detail}`.")


def rate_limited():
    """app_commands check: one token bucket per (guild, user, command).
    Denials are ephemeral, logged as an incident, and mirrored to audit."""

    async def predicate(interaction: discord.Interaction) -> bool:
        command_name = interaction.command.qualified_name if interaction.command else "?"
        key = (interaction.guild_id, interaction.user.id, command_name)
        if _LIMITER.allow(key):
            return True
        if interaction.guild_id is not None:
            await log_incident(interaction.client, interaction.guild_id, interaction.user.id, "rate_limit", detail=command_name)
        await interaction.response.send_message(
            voice.decline("That's being used a touch too often. Give it a moment and try again."),
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


class AdminCog(commands.Cog, name="admin"):
    """Incident ledger. Rank/permission gating lives in roles.py; this cog
    just exposes what's been logged."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    incidents = app_commands.Group(name="incidents", description="Abuse and permission-denial log.")

    @incidents.command(name="recent", description="Show the last 20 logged incidents.")
    async def recent(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return
        is_admin = member.guild_permissions.administrator or member.id == guild.owner_id
        if not is_admin:
            await log_incident(self.bot, guild.id, member.id, "permission_denied", detail="incidents recent")
            await interaction.response.send_message(voice.decline("That's an admin-only ledger."), ephemeral=True)
            return

        assert self.bot.db is not None
        rows = await self.bot.db.execute_fetchall(
            "SELECT * FROM incidents WHERE guild_id = ? ORDER BY id DESC LIMIT 20", (guild.id,)
        )
        minimal = await minimal_mode(self.bot.db, guild.id)
        if not rows:
            await interaction.response.send_message(
                embed=voice.embed("Incidents", "Clean sheet — nothing logged.", minimal=minimal), ephemeral=True
            )
            return
        lines = "\n".join(f"`{r['at']}` **{r['kind']}** <@{r['user_id']}> {r['detail']}".strip() for r in rows)
        await interaction.response.send_message(
            embed=voice.embed("Recent Incidents", lines, minimal=minimal), ephemeral=True
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
