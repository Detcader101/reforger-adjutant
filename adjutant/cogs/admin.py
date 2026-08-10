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
from ..services import config as config_service
from ..services import ratelimit as ratelimit_service

log = logging.getLogger(__name__)

# Mirrors cogs/config.py's FEATURE_OPTIONS labels — duplicated rather than
# imported because cogs/config.py imports this module at ITS top, so a
# module-level import back the other way would be circular. Built off
# services/config.py's FEATURE_KEYS (Discord-free, safe to import here) so
# at least the *set* of features can't drift out of sync; only the display
# labels are hand-kept identical.
_ADMIN_FEATURE_LABELS = {"teams": "Teams", "events": "Events", "map": "Map", "serverlink": "Server Link"}
_ADMIN_FEATURE_CHOICES = [
    app_commands.Choice(name=_ADMIN_FEATURE_LABELS[key], value=key) for key in config_service.FEATURE_KEYS
]
_ADMIN_ON_OFF_CHOICES = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]

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


async def _check_permission(interaction: discord.Interaction, permission: str) -> bool:
    """Rank/permission-override recheck for /admin's raw fallbacks — same
    denial copy + incident log as roles.require_permission's app_commands
    check, just callable from a plain command body. Imports roles.py lazily:
    roles.py imports this module at ITS top, so a module-level import here
    would be a circular import depending on which cog loads first."""
    from .roles import decline_missing_permission, member_has_permission

    member = interaction.user
    guild = interaction.guild
    if guild is None or not isinstance(member, discord.Member):
        return False
    allowed = await member_has_permission(interaction.client, guild, member, permission)
    if not allowed:
        await decline_missing_permission(interaction, permission)
    return allowed


async def _check_admin(interaction: discord.Interaction) -> bool:
    """Same as `_check_permission` but for the Administrator-or-owner gate
    (roles.require_admin's underlying rule)."""
    from .roles import decline_admin_only, is_guild_admin

    member = interaction.user
    if interaction.guild is not None and isinstance(member, discord.Member) and is_guild_admin(member):
        return True
    await decline_admin_only(interaction)
    return False


def rate_limited():
    """app_commands check: one token bucket per (guild, user, command).
    Denials are ephemeral, logged as an incident, and mirrored to audit."""

    async def predicate(interaction: discord.Interaction) -> bool:
        command_name = interaction.command.qualified_name if interaction.command else "?"
        return await check_rate_limit(interaction, command_name)

    return app_commands.check(predicate)


async def check_rate_limit(interaction: discord.Interaction, key: str) -> bool:
    """The same token bucket as `rate_limited()`, callable directly from a
    button/modal callback. Those have no `interaction.command` to key off
    of the way an app_commands check does — a button press or modal submit
    isn't a command invocation — so callers pass an explicit, stable `key`
    per action (e.g. "map.mark") instead. Sends the same decline + logs the
    same incident on denial as the decorator form."""
    bucket_key = (interaction.guild_id, interaction.user.id, key)
    if _LIMITER.allow(bucket_key):
        return True
    if interaction.guild_id is not None:
        await log_incident(interaction.client, interaction.guild_id, interaction.user.id, "rate_limit", detail=key)
    await interaction.response.send_message(
        voice.decline("That's being used a touch too often. Give it a moment and try again."),
        ephemeral=True,
    )
    return False


class AdminCog(
    commands.GroupCog,
    group_name="admin",
    # Explicit for the same reason as SetupCog's: the docstring fallback is
    # capped at 100 characters by Discord, and exceeding it fails the entire
    # command upload, not just this group.
    group_description="Raw fallbacks for when buttons aren't working, plus the incidents ledger.",
):
    """/admin — raw fallbacks for when a Discord component (button/select/
    modal) isn't working, plus the incidents ledger. Every subcommand here
    forwards to the exact same plain method its button/panel equivalent
    calls elsewhere, so there is exactly one implementation of each piece
    of mutating logic — this cog only adds the entry point and, where the
    original command used a declarative check, the equivalent recheck.

    Gating is preserved command-by-command, not blanket admin-only: team-
    disband/rank-revoke keep their original rank-based checks (teams.manage
    / roles.manage), event-cancel/event-teardown keep their original
    inline organiser-or-staff / organiser-or-admin rules unchanged, and the
    config fallbacks + incidents keep the original Administrator-or-owner
    gate.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _get_cog(self, name: str):
        return self.bot.get_cog(name)

    async def _missing_cog(self, interaction: discord.Interaction, feature: str) -> None:
        await interaction.response.send_message(
            voice.broken(f"{feature} isn't loaded right now.", "Try again shortly, or flag an admin."),
            ephemeral=True,
        )

    # -- teams ----------------------------------------------------------

    @app_commands.command(name="team-disband", description="Raw fallback: disband a team without the panel button.")
    @app_commands.describe(
        name="Team name",
        confirm="Skip the button confirmation and disband immediately — for when components aren't working",
    )
    async def team_disband(self, interaction: discord.Interaction, name: str, confirm: bool = False) -> None:
        if not await _check_permission(interaction, "teams.manage"):
            return
        cog = self._get_cog("TeamsCog")
        if cog is None:
            await self._missing_cog(interaction, "Teams")
            return
        await cog.disband(interaction, name, confirm=confirm)

    # -- ranks ------------------------------------------------------------

    @app_commands.command(name="rank-revoke", description="Raw fallback: revoke a granted role without the Revoke button.")
    @rate_limited()
    async def rank_revoke(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        if not await _check_permission(interaction, "roles.manage"):
            return
        cog = self._get_cog("RolesCog")
        if cog is None:
            await self._missing_cog(interaction, "Ranks")
            return
        await cog.revoke(interaction, member, role)

    # -- events -------------------------------------------------------------

    @app_commands.command(name="event-cancel", description="Raw fallback: cancel an event without the manage panel.")
    @app_commands.describe(event_id="Event id, from /event")
    async def event_cancel(self, interaction: discord.Interaction, event_id: int) -> None:
        cog = self._get_cog("EventsCog")
        if cog is None:
            await self._missing_cog(interaction, "Events")
            return
        await cog.cancel(interaction, event_id=event_id)

    @app_commands.command(name="event-teardown", description="Raw fallback: tear down an event without the manage panel.")
    @app_commands.describe(event_id="Event id, from /event")
    async def event_teardown(self, interaction: discord.Interaction, event_id: int) -> None:
        cog = self._get_cog("EventsCog")
        if cog is None:
            await self._missing_cog(interaction, "Events")
            return
        await cog.teardown(interaction, event_id=event_id)

    # -- config -------------------------------------------------------------

    @app_commands.command(name="feature", description="Raw fallback: toggle a feature on or off.")
    @app_commands.describe(feature="Which feature", state="on or off")
    @app_commands.choices(feature=_ADMIN_FEATURE_CHOICES, state=_ADMIN_ON_OFF_CHOICES)
    @app_commands.default_permissions(administrator=True)
    @rate_limited()
    async def feature(
        self, interaction: discord.Interaction, feature: app_commands.Choice[str], state: app_commands.Choice[str]
    ) -> None:
        if not await _check_admin(interaction):
            return
        cog = self._get_cog("ConfigCog")
        if cog is None:
            await self._missing_cog(interaction, "Config")
            return
        await cog.feature(interaction, feature, state)

    @app_commands.command(name="audit-channel", description="Raw fallback: set or clear the audit log channel.")
    @app_commands.describe(channel="Channel to use for audit notes — omit to clear")
    @app_commands.default_permissions(administrator=True)
    @rate_limited()
    async def audit_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if not await _check_admin(interaction):
            return
        cog = self._get_cog("ConfigCog")
        if cog is None:
            await self._missing_cog(interaction, "Config")
            return
        await cog.audit_channel(interaction, channel)

    @app_commands.command(name="minimal", description="Raw fallback: toggle minimal mode.")
    @app_commands.describe(state="on or off")
    @app_commands.choices(state=_ADMIN_ON_OFF_CHOICES)
    @app_commands.default_permissions(administrator=True)
    @rate_limited()
    async def minimal(self, interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
        if not await _check_admin(interaction):
            return
        cog = self._get_cog("ConfigCog")
        if cog is None:
            await self._missing_cog(interaction, "Config")
            return
        await cog.minimal(interaction, state)

    @app_commands.command(name="permission", description="Raw fallback: set the minimum rank required for a bot permission.")
    @app_commands.describe(key="Permission key, e.g. teams.manage", min_rank="Minimum ladder position required")
    @app_commands.default_permissions(administrator=True)
    @rate_limited()
    async def permission(self, interaction: discord.Interaction, key: str, min_rank: int) -> None:
        if not await _check_admin(interaction):
            return
        cog = self._get_cog("ConfigCog")
        if cog is None:
            await self._missing_cog(interaction, "Config")
            return
        await cog.permission(interaction, key, min_rank)

    @app_commands.command(name="reset", description="Raw fallback: restore default permission thresholds and minimal mode.")
    @app_commands.default_permissions(administrator=True)
    async def reset(self, interaction: discord.Interaction) -> None:
        if not await _check_admin(interaction):
            return
        cog = self._get_cog("ConfigCog")
        if cog is None:
            await self._missing_cog(interaction, "Config")
            return
        await cog.reset(interaction)

    # -- incidents ------------------------------------------------------

    @app_commands.command(name="incidents", description="Show the last 20 logged incidents.")
    async def incidents(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return
        is_admin = member.guild_permissions.administrator or member.id == guild.owner_id
        if not is_admin:
            await log_incident(self.bot, guild.id, member.id, "permission_denied", detail="admin incidents")
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
