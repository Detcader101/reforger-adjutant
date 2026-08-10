"""Team units: a role plus a locked category (text + voice channel).

Leak prevention is structural, not just conventional: every response here is
ephemeral, and the only team ever named in a reply is the one the admin
explicitly passed as a command argument — nothing here enumerates or
cross-references other teams' rosters or channels.
"""

from __future__ import annotations

import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from .admin import fetchone, rate_limited
from .roles import require_permission

log = logging.getLogger(__name__)


async def _rollback(built: list) -> None:
    """Delete objects created during a failed team build, newest first.

    Best-effort by design: rollback runs because something already went
    wrong, so a failure to delete is logged and swallowed rather than
    replacing the original error the user needs to see.
    """
    for obj in reversed(built):
        try:
            await obj.delete(reason="Adjutant: rolling back a failed team create")
        except discord.HTTPException:
            log.warning("Rollback could not delete %r after a failed team create", obj)


async def build_team(
    guild: discord.Guild, name: str, reason: str
) -> tuple[discord.Role, discord.abc.GuildChannel, list]:
    """Create a team's role, locked category and channels.

    The counterpart to `_disband_team`, and deliberately importable: the
    self-test harness drives this exact function, so a fix here is a fix
    everywhere rather than something a duplicated copy can miss.

    Returns (role, category, built) where `built` is everything created, in
    creation order, for `_rollback` to undo. Raises on failure with `built`
    reported through the exception's `.built` attribute.
    """
    built: list = []
    try:
        role = await guild.create_role(name=f"Team {name}", reason=reason)
        built.append(role)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, send_messages=True
            ),
        }
        if guild.me is not None:
            # No manage_roles here: Discord rejects an overwrite granting that
            # bit unless the actor is a full Administrator, so including it
            # made team creation fail outright for a correctly-permissioned
            # bot. It was redundant anyway — guild-wide Manage Roles already
            # applies in channels the bot can see.
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, manage_channels=True, connect=True
            )
        category = await guild.create_category(name=name, overwrites=overwrites, reason=reason)
        built.append(category)
        built.append(await category.create_text_channel(name="chat"))
        built.append(await category.create_voice_channel(name="voice"))
    except (discord.Forbidden, discord.HTTPException) as error:
        error.built = built  # type: ignore[attr-defined]
        raise
    return role, category, built


async def _disband_team(bot: commands.Bot, guild: discord.Guild, team_row: dict) -> None:
    """Deletes a team's role/category/channels and its DB row.
    Raises discord.Forbidden if Discord-side deletion is blocked partway —
    callers decide how to report that."""
    assert bot.db is not None
    category = guild.get_channel(team_row["category_id"])
    role = guild.get_role(team_row["role_id"])
    if isinstance(category, discord.CategoryChannel):
        for channel in list(category.channels):
            await channel.delete(reason="Adjutant: team disbanded")
        await category.delete(reason="Adjutant: team disbanded")
    if role is not None:
        await role.delete(reason="Adjutant: team disbanded")

    await bot.db.execute("DELETE FROM teams WHERE id = ?", (team_row["id"],))
    await bot.db.commit()


class DisbandConfirmView(view_util.ErrorHandledView):
    """Button-driven confirmation for /team disband. Every button-driven
    flow keeps a slash-command fallback too — see disband(..., confirm=)."""

    def __init__(self, bot: commands.Bot, team_row: dict, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.team = team_row
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(content="Confirmation timed out. Nothing was changed.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Disband", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        assert guild is not None
        try:
            await _disband_team(self.bot, guild, self.team)
        except discord.Forbidden:
            await interaction.response.edit_message(
                content=voice.broken("Couldn't remove everything.", "Check my permissions and tidy up the rest manually."),
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"**{self.team['name']}** stood down. Role and channels removed.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Stood down the order. Nothing changed.", view=None)


class TeamsCog(commands.GroupCog, group_name="team"):
    """/team — create/disband/assign locked team units."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="create", description="Create a locked team: role + text/voice channels.")
    @app_commands.describe(name="Team name")
    @require_permission("teams.manage")
    @rate_limited()
    async def create(self, interaction: discord.Interaction, name: str) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None

        existing = await fetchone(self.bot.db, "SELECT id FROM teams WHERE guild_id = ? AND name = ?", (guild.id, name))
        if existing is not None:
            await interaction.response.send_message(voice.decline(f"A team called **{name}** already exists."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            role, category, built = await build_team(
                guild, name, reason=f"Adjutant: team create by {interaction.user}"
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            # Undo whatever was made before the failure, so a half-built team
            # is never left in the server for someone to clean up by hand.
            await _rollback(getattr(error, "built", []))
            if isinstance(error, discord.Forbidden):
                message = voice.broken(
                    "I lack permission to create roles or channels.",
                    "Check my role sits high enough and has Manage Roles and Manage Channels.",
                )
            else:
                message = voice.broken(
                    "Discord refused to create the team's role or channels.",
                    "Nothing was left behind. Check the server isn't at its channel or role limit, then try again.",
                )
            await interaction.followup.send(message, ephemeral=True)
            return

        try:
            await self.bot.db.execute(
                "INSERT INTO teams (guild_id, name, role_id, category_id) VALUES (?, ?, ?, ?)",
                (guild.id, name, role.id, category.id),
            )
            await self.bot.db.commit()
        except sqlite3.Error:
            # A team the bot can't track is worse than no team at all: it
            # would be invisible to /team disband and leak channels forever.
            log.exception("Recording team %r failed; removing what was built", name)
            await _rollback(built)
            await interaction.followup.send(
                voice.broken(
                    "I couldn't record the team, so I've removed what I'd built.",
                    "Nothing was left behind. Worth telling an admin if it happens again.",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"Stood up **{name}** — role, category, and channels are live.", ephemeral=True)

    @app_commands.command(name="disband", description="Disband a team: removes its role, category, and channels.")
    @app_commands.describe(
        name="Team name",
        confirm="Skip the button confirmation and disband immediately — for when components aren't working",
    )
    @require_permission("teams.manage")
    async def disband(self, interaction: discord.Interaction, name: str, confirm: bool = False) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        team = await fetchone(self.bot.db, "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, name))
        if team is None:
            await interaction.response.send_message(voice.decline(f"No team called **{name}** on record."), ephemeral=True)
            return

        if confirm:
            try:
                await _disband_team(self.bot, guild, dict(team))
            except discord.Forbidden:
                await interaction.response.send_message(
                    voice.broken("Couldn't remove everything.", "Check my permissions and tidy up the rest manually."),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(f"**{name}** stood down. Role and channels removed.", ephemeral=True)
            return

        view = DisbandConfirmView(self.bot, dict(team))
        await interaction.response.send_message(
            f"Confirm: disband **{name}**? This removes its role, category, and channels — it cannot be undone.\n"
            "(Or rerun with `confirm: True` to skip this button.)",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @app_commands.command(name="assign", description="Add a member to a team.")
    @app_commands.describe(member="Member to assign", team="Team name")
    @require_permission("teams.manage")
    @rate_limited()
    async def assign(self, interaction: discord.Interaction, member: discord.Member, team: str) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        row = await fetchone(self.bot.db, "SELECT role_id FROM teams WHERE guild_id = ? AND name = ?", (guild.id, team))
        if row is None:
            await interaction.response.send_message(voice.decline(f"No team called **{team}** on record."), ephemeral=True)
            return
        role = guild.get_role(row["role_id"])
        if role is None:
            await interaction.response.send_message(
                voice.broken(f"**{team}**'s role no longer exists.", "Disband and recreate the team."), ephemeral=True
            )
            return
        try:
            await member.add_roles(role, reason=f"Adjutant: team assign by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                voice.broken("I can't assign that role.", "Check my role sits above it in the hierarchy."), ephemeral=True
            )
            return
        await interaction.response.send_message(f"{member.mention} is now with **{team}**.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a member from a team.")
    @app_commands.describe(member="Member to remove", team="Team name")
    @require_permission("teams.manage")
    @rate_limited()
    async def remove(self, interaction: discord.Interaction, member: discord.Member, team: str) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        row = await fetchone(self.bot.db, "SELECT role_id FROM teams WHERE guild_id = ? AND name = ?", (guild.id, team))
        if row is None:
            await interaction.response.send_message(voice.decline(f"No team called **{team}** on record."), ephemeral=True)
            return
        role = guild.get_role(row["role_id"])
        if role is None:
            await interaction.response.send_message(
                voice.broken(f"**{team}**'s role no longer exists.", "Disband and recreate the team."), ephemeral=True
            )
            return
        try:
            await member.remove_roles(role, reason=f"Adjutant: team remove by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                voice.broken("I can't remove that role.", "Check my role sits above it in the hierarchy."), ephemeral=True
            )
            return
        await interaction.response.send_message(f"{member.mention} is off **{team}**.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # already messaged + logged inside the failing check
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamsCog(bot))
