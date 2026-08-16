"""Rank ladder configuration and perma/temp/event role grants.

Also exposes the shared permission-check decorators (require_admin,
require_permission) that teams.py and setup.py build their own gating on —
they live here because they're built directly on services.ranks, which this
cog owns.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import view_util, voice
from ..services import grants as grants_service
from ..services import ranks as ranks_service
from .admin import check_rate_limit, log_incident, minimal_mode, note_audit, rate_limited

log = logging.getLogger(__name__)

EXPIRY_INTERVAL_S = 60


def is_guild_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id == member.guild.owner_id


async def load_ladder(db: aiosqlite.Connection, guild_id: int) -> list[ranks_service.RankEntry]:
    rows = await db.execute_fetchall(
        "SELECT role_id, position, name FROM ranks WHERE guild_id = ?", (guild_id,)
    )
    return [
        ranks_service.RankEntry(role_id=r["role_id"], position=r["position"], name=r["name"])
        for r in rows
    ]


async def load_permission_overrides(db: aiosqlite.Connection, guild_id: int) -> dict[str, int]:
    rows = await db.execute_fetchall(
        "SELECT permission, min_rank FROM permissions WHERE guild_id = ?", (guild_id,)
    )
    return {r["permission"]: r["min_rank"] for r in rows}


async def decline_admin_only(
    interaction: discord.Interaction, *, detail: str | None = None
) -> None:
    """Same denial copy + incident log as `require_admin`'s predicate, for
    button/modal callbacks that can't use an app_commands check."""
    member = interaction.user
    if interaction.guild is not None:
        tag = detail or (interaction.command.qualified_name if interaction.command else "?")
        await log_incident(
            interaction.client, interaction.guild.id, member.id, "permission_denied", detail=tag
        )
    await interaction.response.send_message(
        voice.decline("That's an admin-only order."), ephemeral=True
    )


def require_admin():
    """app_commands check: guild Administrator permission or the guild
    owner. Denials are ephemeral and logged as an incident."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if (
            interaction.guild is not None
            and isinstance(member, discord.Member)
            and is_guild_admin(member)
        ):
            return True
        await decline_admin_only(interaction)
        return False

    return app_commands.check(predicate)


async def member_has_permission(
    bot: commands.Bot, guild: discord.Guild, member: discord.Member, permission: str
) -> bool:
    """The rank/permission-override check itself, split out from
    `require_permission` so button/modal callbacks (a fresh interaction that
    may not be the original invoker) can re-check the same rule at click
    time instead of only at command-invocation time."""
    if is_guild_admin(member):
        return True
    assert bot.db is not None
    ladder = await load_ladder(bot.db, guild.id)
    overrides = await load_permission_overrides(bot.db, guild.id)
    member_rank = ranks_service.resolve_rank((r.id for r in member.roles), ladder)
    min_rank = ranks_service.get_min_rank(permission, overrides)
    return ranks_service.has_permission(member_rank, min_rank, is_guild_admin(member))


def require_permission(permission: str):
    """app_commands check gating on the guild's rank ladder + permission
    overrides. Guild admins always pass. Denials are ephemeral and logged."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return False

        allowed = await member_has_permission(interaction.client, guild, member, permission)
        if not allowed:
            await log_incident(
                interaction.client, guild.id, member.id, "permission_denied", detail=permission
            )
            await interaction.response.send_message(
                voice.decline(f"You'll need higher rank for `{permission}`."), ephemeral=True
            )
        return allowed

    return app_commands.check(predicate)


async def decline_missing_permission(interaction: discord.Interaction, permission: str) -> None:
    """Same denial copy + incident log as `require_permission`'s predicate,
    for button/modal callbacks that can't use an app_commands check."""
    guild = interaction.guild
    member = interaction.user
    assert guild is not None
    await log_incident(
        interaction.client, guild.id, member.id, "permission_denied", detail=permission
    )
    await interaction.response.send_message(
        voice.decline(f"You'll need higher rank for `{permission}`."), ephemeral=True
    )


class RevokeGrantView(view_util.ErrorHandledView):
    """Attached to a successful /rank grant reply — one-shot Revoke button
    for undoing the grant that was just made. Re-checks roles.manage at
    click time since a button press is a fresh interaction and may not
    come from whoever ran the grant."""

    def __init__(
        self,
        bot: commands.Bot,
        guild: discord.Guild,
        member_id: int,
        role_id: int,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.member_id = member_id
        self.role_id = role_id
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Revoke", style=discord.ButtonStyle.danger)
    async def revoke(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member_actor = interaction.user
        if not isinstance(member_actor, discord.Member):
            return
        if not await member_has_permission(self.bot, self.guild, member_actor, "roles.manage"):
            await decline_missing_permission(interaction, "roles.manage")
            return
        if not await check_rate_limit(interaction, "rank.revoke"):
            return
        member = self.guild.get_member(self.member_id)
        role = self.guild.get_role(self.role_id)
        if member is None or role is None:
            await interaction.response.send_message(
                voice.broken("That member or role no longer exists.", "Nothing to revoke."),
                ephemeral=True,
            )
            return
        await _do_revoke(self.bot, interaction, member, role)


async def _do_revoke(
    bot: commands.Bot, interaction: discord.Interaction, member: discord.Member, role: discord.Role
) -> None:
    """Shared by /rank's Revoke button and /admin rank-revoke. Strips the
    role in Discord BEFORE forgetting the grant — the other order loses the
    record whenever Discord refuses: the member keeps the role, nothing
    tracks it any more, and the expiry sweep can never reclaim it."""
    assert bot.db is not None and interaction.guild is not None
    try:
        await member.remove_roles(role, reason=f"Adjutant: rank revoke by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            voice.broken(
                "I can't remove that role.", "Check my role sits above it in the hierarchy."
            ),
            ephemeral=True,
        )
        return
    await grants_service.revoke_grant(
        bot.db, guild_id=interaction.guild.id, user_id=member.id, role_id=role.id
    )
    await interaction.response.send_message(
        f"Done. {role.mention} withdrawn from {member.mention}.", ephemeral=True
    )


class RolesCog(commands.Cog, name="RolesCog"):
    """/rank — bare shows the ladder; with member+role, grants (optionally
    timed). Ladder editing (ladder_add/ladder_remove below) has no direct
    slash command any more — it's kept as plain methods for the /adjutant
    hub's Ranks button to call once wired to setup.py."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_grants.start()

    async def cog_unload(self) -> None:
        self.expire_grants.cancel()

    # -- ladder (no longer directly slash-invocable — see class docstring) --

    async def ladder_add(
        self, interaction: discord.Interaction, role: discord.Role, position: int, name: str
    ) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute(
            "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET position = excluded.position, name = excluded.name",
            (interaction.guild_id, role.id, position, name),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Noted. **{name}** set at position {position}, backed by {role.mention}.",
            ephemeral=True,
        )

    async def ladder_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute(
            "DELETE FROM ranks WHERE guild_id = ? AND role_id = ?", (interaction.guild_id, role.id)
        )
        await self.bot.db.commit()
        if cursor.rowcount == 0:
            await interaction.response.send_message(
                voice.decline(f"{role.mention} isn't on the ladder."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Struck {role.mention} from the ladder.", ephemeral=True
        )

    async def _ladder_embed(self, guild: discord.Guild) -> discord.Embed | None:
        assert self.bot.db is not None
        ladder = await load_ladder(self.bot.db, guild.id)
        minimal = await minimal_mode(self.bot.db, guild.id)
        if not ladder:
            return None
        lines = "\n".join(
            f"{entry.position}. {entry.name} — <@&{entry.role_id}>"
            for entry in sorted(ladder, key=lambda e: -e.position)
        )
        return voice.embed("Rank Ladder", lines, minimal=minimal)

    # -- /rank: bare shows the ladder, member+role grants -------------------

    @app_commands.command(
        name="rank", description="Show the rank ladder, or grant a member a role."
    )
    @app_commands.describe(
        member="Who to grant a role to — omit to just view the ladder",
        role="The role to grant",
        duration="Optional duration, e.g. 2h, 3d, 1w — omit for permanent",
    )
    @app_commands.rename(duration="for")
    @rate_limited()
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        role: discord.Role | None = None,
        duration: str | None = None,
    ) -> None:
        assert self.bot.db is not None and interaction.guild is not None
        guild = interaction.guild

        if member is None and role is None:
            embed = await self._ladder_embed(guild)
            if embed is None:
                await interaction.response.send_message(
                    voice.broken(
                        "No rank ladder is configured.",
                        "An admin can set one up from `/adjutant` → Ranks, or run `/setup`.",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if member is None or role is None:
            await interaction.response.send_message(
                voice.decline(
                    "Give both a member and a role to grant — or neither, to just view the ladder."
                ),
                ephemeral=True,
            )
            return

        actor = interaction.user
        if not isinstance(actor, discord.Member):
            return
        if not await member_has_permission(self.bot, guild, actor, "roles.manage"):
            await decline_missing_permission(interaction, "roles.manage")
            return

        expires_at: str | None = None
        kind = "perma"
        if duration:
            try:
                expiry = grants_service.compute_expiry(
                    datetime.now(UTC).replace(tzinfo=None), duration
                )
            except ValueError:
                await interaction.response.send_message(
                    voice.decline(
                        f"Couldn't parse `{duration}` — try something like `2h`, `3d`, or `1w`."
                    ),
                    ephemeral=True,
                )
                return
            expires_at = grants_service.format_timestamp(expiry)
            kind = "temp"

        try:
            await member.add_roles(role, reason=f"Adjutant: rank grant by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                voice.broken(
                    "I can't assign that role.", "Check my role sits above it in the hierarchy."
                ),
                ephemeral=True,
            )
            return

        await grants_service.record_grant(
            self.bot.db,
            guild_id=guild.id,
            user_id=member.id,
            role_id=role.id,
            kind=kind,
            granted_by=interaction.user.id,
            expires_at=expires_at,
        )
        tail = f"until it expires (`{duration}`)" if kind == "temp" else "permanently"
        view = RevokeGrantView(self.bot, guild, member.id, role.id)
        await interaction.response.send_message(
            f"Done. {member.mention} now holds {role.mention}, {tail}.", view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def revoke(
        self, interaction: discord.Interaction, member: discord.Member, role: discord.Role
    ) -> None:
        """Kept for /admin rank-revoke — the raw fallback for when the
        Revoke button on a /rank reply isn't available any more."""
        await _do_revoke(self.bot, interaction, member, role)

    # -- background expiry --------------------------------------------------

    @tasks.loop(seconds=EXPIRY_INTERVAL_S)
    async def expire_grants(self) -> None:
        assert self.bot.db is not None
        due = await grants_service.due_expiries(self.bot.db, datetime.now(UTC).replace(tzinfo=None))
        for due_grant in due:
            guild = self.bot.get_guild(due_grant.guild_id)
            member = guild.get_member(due_grant.user_id) if guild is not None else None
            role = guild.get_role(due_grant.role_id) if guild is not None else None

            if guild is not None and member is not None and role is not None:
                try:
                    await member.remove_roles(role, reason="Adjutant: temporary rank expired")
                except discord.HTTPException:
                    # Keep the grant so the next tick tries again. Dropping it
                    # here would quietly turn a temporary rank into a permanent
                    # one the moment the bot's role sat too low for a minute.
                    log.warning(
                        "Could not remove expired role %s from %s in guild %s; will retry",
                        role.id,
                        member.id,
                        guild.id,
                    )
                    continue
                await note_audit(
                    self.bot,
                    due_grant.guild_id,
                    f"Temp role expired: <@&{due_grant.role_id}> removed from <@{due_grant.user_id}>.",
                )
            # Reached when the role was removed, or when the guild, member or
            # role is gone — in every case there's nothing left to reclaim.
            await grants_service.revoke_grant_by_id(self.bot.db, due_grant.id)

    @expire_grants.before_loop
    async def before_expire_grants(self) -> None:
        await self.bot.wait_until_ready()

    @expire_grants.error
    async def on_expire_grants_error(self, error: BaseException) -> None:
        # discord.ext.tasks stops a loop for good once an exception escapes
        # it. Left alone, one transient database error would mean temporary
        # ranks silently never expire again until the bot restarts. Pause a
        # cycle so a persistent fault can't spin, then carry on.
        log.exception("Expiry sweep failed; restarting it", exc_info=error)
        await asyncio.sleep(EXPIRY_INTERVAL_S)
        self.expire_grants.restart()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # already messaged + logged inside the failing check
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesCog(bot))
