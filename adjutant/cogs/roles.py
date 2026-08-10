"""Rank ladder configuration and perma/temp/event role grants.

Also exposes the shared permission-check decorators (require_admin,
require_permission) that teams.py and setup.py build their own gating on —
they live here because they're built directly on services.ranks, which this
cog owns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import view_util, voice
from ..services import grants as grants_service
from ..services import ranks as ranks_service
from .admin import log_incident, minimal_mode, note_audit, rate_limited

log = logging.getLogger(__name__)


def is_guild_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id == member.guild.owner_id


async def load_ladder(db: aiosqlite.Connection, guild_id: int) -> list[ranks_service.RankEntry]:
    rows = await db.execute_fetchall(
        "SELECT role_id, position, name FROM ranks WHERE guild_id = ?", (guild_id,)
    )
    return [ranks_service.RankEntry(role_id=r["role_id"], position=r["position"], name=r["name"]) for r in rows]


async def load_permission_overrides(db: aiosqlite.Connection, guild_id: int) -> dict[str, int]:
    rows = await db.execute_fetchall(
        "SELECT permission, min_rank FROM permissions WHERE guild_id = ?", (guild_id,)
    )
    return {r["permission"]: r["min_rank"] for r in rows}


def require_admin():
    """app_commands check: guild Administrator permission or the guild
    owner. Denials are ephemeral and logged as an incident."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if interaction.guild is not None and isinstance(member, discord.Member) and is_guild_admin(member):
            return True
        if interaction.guild is not None:
            command_name = interaction.command.qualified_name if interaction.command else "?"
            await log_incident(interaction.client, interaction.guild.id, member.id, "permission_denied", detail=command_name)
        await interaction.response.send_message(voice.decline("That's an admin-only order."), ephemeral=True)
        return False

    return app_commands.check(predicate)


def require_permission(permission: str):
    """app_commands check gating on the guild's rank ladder + permission
    overrides. Guild admins always pass. Denials are ephemeral and logged."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            return False

        is_admin = is_guild_admin(member)
        if is_admin:
            return True

        assert interaction.client.db is not None
        db = interaction.client.db
        ladder = await load_ladder(db, guild.id)
        overrides = await load_permission_overrides(db, guild.id)
        member_rank = ranks_service.resolve_rank((r.id for r in member.roles), ladder)
        min_rank = ranks_service.get_min_rank(permission, overrides)
        allowed = ranks_service.has_permission(member_rank, min_rank, is_admin)

        if not allowed:
            await log_incident(interaction.client, guild.id, member.id, "permission_denied", detail=permission)
            await interaction.response.send_message(
                voice.decline(f"You'll need higher rank for `{permission}`."), ephemeral=True
            )
        return allowed

    return app_commands.check(predicate)


class RolesCog(commands.GroupCog, group_name="rank"):
    """/rank — ladder configuration and role grants."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_grants.start()

    async def cog_unload(self) -> None:
        self.expire_grants.cancel()

    # -- ladder ---------------------------------------------------------

    @app_commands.command(name="ladder-add", description="Add or update a rank on the ladder.")
    @app_commands.describe(
        role="Discord role backing this rank",
        position="Ladder position — higher is more senior",
        name="Display name for this rank",
    )
    @require_admin()
    async def ladder_add(self, interaction: discord.Interaction, role: discord.Role, position: int, name: str) -> None:
        assert self.bot.db is not None
        await self.bot.db.execute(
            "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET position = excluded.position, name = excluded.name",
            (interaction.guild_id, role.id, position, name),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Noted. **{name}** set at position {position}, backed by {role.mention}.", ephemeral=True
        )

    @app_commands.command(name="ladder-remove", description="Remove a rank from the ladder.")
    @require_admin()
    async def ladder_remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute(
            "DELETE FROM ranks WHERE guild_id = ? AND role_id = ?", (interaction.guild_id, role.id)
        )
        await self.bot.db.commit()
        if cursor.rowcount == 0:
            await interaction.response.send_message(voice.decline(f"{role.mention} isn't on the ladder."), ephemeral=True)
            return
        await interaction.response.send_message(f"Struck {role.mention} from the ladder.", ephemeral=True)

    @app_commands.command(name="ladder-show", description="Show the current rank ladder.")
    async def ladder_show(self, interaction: discord.Interaction) -> None:
        assert self.bot.db is not None and interaction.guild is not None
        ladder = await load_ladder(self.bot.db, interaction.guild.id)
        minimal = await minimal_mode(self.bot.db, interaction.guild.id)
        if not ladder:
            await interaction.response.send_message(
                voice.broken(
                    "No rank ladder is configured.",
                    "An admin can add one with `/rank ladder-add`, or run `/setup` for a default ladder.",
                ),
                ephemeral=True,
            )
            return
        lines = "\n".join(
            f"{entry.position}. {entry.name} — <@&{entry.role_id}>"
            for entry in sorted(ladder, key=lambda e: -e.position)
        )
        await interaction.response.send_message(embed=voice.embed("Rank Ladder", lines, minimal=minimal), ephemeral=True)

    # -- grants -----------------------------------------------------------

    @app_commands.command(name="grant", description="Grant a member a role, permanently or for a set duration.")
    @app_commands.describe(
        member="Who to grant the role to",
        role="The role to grant",
        duration="Optional duration, e.g. 2h, 3d, 1w — omit for permanent",
    )
    @require_permission("roles.manage")
    @rate_limited()
    async def grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        duration: str | None = None,
    ) -> None:
        assert self.bot.db is not None and interaction.guild is not None
        expires_at: str | None = None
        kind = "perma"
        if duration:
            try:
                expiry = grants_service.compute_expiry(datetime.now(timezone.utc).replace(tzinfo=None), duration)
            except ValueError:
                await interaction.response.send_message(
                    voice.decline(f"Couldn't parse `{duration}` — try something like `2h`, `3d`, or `1w`."),
                    ephemeral=True,
                )
                return
            expires_at = grants_service.format_timestamp(expiry)
            kind = "temp"

        try:
            await member.add_roles(role, reason=f"Adjutant: rank grant by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                voice.broken("I can't assign that role.", "Check my role sits above it in the hierarchy."),
                ephemeral=True,
            )
            return

        await grants_service.record_grant(
            self.bot.db,
            guild_id=interaction.guild.id,
            user_id=member.id,
            role_id=role.id,
            kind=kind,
            granted_by=interaction.user.id,
            expires_at=expires_at,
        )
        tail = f"until it expires (`{duration}`)" if kind == "temp" else "permanently"
        await interaction.response.send_message(f"Done. {member.mention} now holds {role.mention}, {tail}.", ephemeral=True)

    @app_commands.command(name="revoke", description="Revoke a granted role from a member.")
    @require_permission("roles.manage")
    @rate_limited()
    async def revoke(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        assert self.bot.db is not None and interaction.guild is not None
        await grants_service.revoke_grant(self.bot.db, guild_id=interaction.guild.id, user_id=member.id, role_id=role.id)
        try:
            await member.remove_roles(role, reason=f"Adjutant: rank revoke by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                voice.broken("I can't remove that role.", "Check my role sits above it in the hierarchy."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"Done. {role.mention} withdrawn from {member.mention}.", ephemeral=True)

    # -- background expiry --------------------------------------------------

    @tasks.loop(seconds=60)
    async def expire_grants(self) -> None:
        assert self.bot.db is not None
        due = await grants_service.due_expiries(self.bot.db, datetime.now(timezone.utc).replace(tzinfo=None))
        for due_grant in due:
            guild = self.bot.get_guild(due_grant.guild_id)
            if guild is not None:
                member = guild.get_member(due_grant.user_id)
                role = guild.get_role(due_grant.role_id)
                if member is not None and role is not None:
                    try:
                        await member.remove_roles(role, reason="Adjutant: temporary rank expired")
                    except discord.HTTPException:
                        log.warning("Failed to remove expired role %s from %s in guild %s", role.id, member.id, guild.id)
                await note_audit(self.bot, due_grant.guild_id, f"Temp role expired: <@&{due_grant.role_id}> removed from <@{due_grant.user_id}>.")
            await grants_service.revoke_grant_by_id(self.bot.db, due_grant.id)

    @expire_grants.before_loop
    async def before_expire_grants(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # already messaged + logged inside the failing check
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesCog(bot))
