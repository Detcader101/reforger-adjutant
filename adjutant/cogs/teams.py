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
from .admin import check_rate_limit, fetchone, rate_limited
from .roles import decline_missing_permission, member_has_permission

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


async def _assign(bot: commands.Bot, interaction: discord.Interaction, member: discord.Member, team_name: str) -> None:
    guild = interaction.guild
    assert guild is not None and bot.db is not None
    row = await fetchone(bot.db, "SELECT role_id FROM teams WHERE guild_id = ? AND name = ?", (guild.id, team_name))
    if row is None:
        await interaction.response.send_message(voice.decline(f"No team called **{team_name}** on record."), ephemeral=True)
        return
    role = guild.get_role(row["role_id"])
    if role is None:
        await interaction.response.send_message(
            voice.broken(f"**{team_name}**'s role no longer exists.", "Disband and recreate the team."), ephemeral=True
        )
        return
    try:
        await member.add_roles(role, reason=f"Adjutant: team assign by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            voice.broken("I can't assign that role.", "Check my role sits above it in the hierarchy."), ephemeral=True
        )
        return
    await interaction.response.send_message(f"{member.mention} is now with **{team_name}**.", ephemeral=True)


async def _remove(bot: commands.Bot, interaction: discord.Interaction, member: discord.Member, team_name: str) -> None:
    guild = interaction.guild
    assert guild is not None and bot.db is not None
    row = await fetchone(bot.db, "SELECT role_id FROM teams WHERE guild_id = ? AND name = ?", (guild.id, team_name))
    if row is None:
        await interaction.response.send_message(voice.decline(f"No team called **{team_name}** on record."), ephemeral=True)
        return
    role = guild.get_role(row["role_id"])
    if role is None:
        await interaction.response.send_message(
            voice.broken(f"**{team_name}**'s role no longer exists.", "Disband and recreate the team."), ephemeral=True
        )
        return
    try:
        await member.remove_roles(role, reason=f"Adjutant: team remove by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            voice.broken("I can't remove that role.", "Check my role sits above it in the hierarchy."), ephemeral=True
        )
        return
    await interaction.response.send_message(f"{member.mention} is off **{team_name}**.", ephemeral=True)


async def _disband(bot: commands.Bot, interaction: discord.Interaction, name: str, confirm: bool = False) -> None:
    """The whole /team disband flow — shared by the panel's Disband button
    (via a fresh confirm view) and /admin team-disband (the raw fallback)."""
    guild = interaction.guild
    assert guild is not None and bot.db is not None
    team = await fetchone(bot.db, "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, name))
    if team is None:
        await interaction.response.send_message(voice.decline(f"No team called **{name}** on record."), ephemeral=True)
        return

    if confirm:
        try:
            await _disband_team(bot, guild, dict(team))
        except discord.Forbidden:
            await interaction.response.send_message(
                voice.broken("Couldn't remove everything.", "Check my permissions and tidy up the rest manually."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"**{name}** stood down. Role and channels removed.", ephemeral=True)
        return

    view = DisbandConfirmView(bot, dict(team))
    await interaction.response.send_message(
        f"Confirm: disband **{name}**? This removes its role, category, and channels — it cannot be undone.\n"
        "(Or rerun `/admin team-disband` with `confirm: True` to skip this button.)",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


class TeamPanelView(view_util.ErrorHandledView):
    """Bare-`/team` and just-created-team surface. A team Select scales past
    Discord's five-row component limit far better than one Assign/Disband
    button pair per team would (that caps out at five teams). Member
    picking uses a UserSelect rather than a typed name/mention, so there's
    no free-text parsing to get wrong. Every button re-checks teams.manage
    at click time since a click is a fresh interaction that may not come
    from whoever opened the panel."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, team_names: list[str],
                 *, selected: str | None = None, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None
        self.selected_team: str | None = selected
        self.selected_member: discord.Member | None = None
        options = [discord.SelectOption(label=n, default=(n == selected)) for n in team_names[:25]]
        self.team_select.options = options or [discord.SelectOption(label="No teams yet", value="__none__")]
        self.team_select.disabled = not options
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        have_team = self.selected_team is not None
        have_pair = have_team and self.selected_member is not None
        self.assign_button.disabled = not have_pair
        self.remove_button.disabled = not have_pair
        self.disband_button.disabled = not have_team
        self.assign_button.label = f"Assign to {self.selected_team}" if have_team else "Assign…"
        self.remove_button.label = f"Remove from {self.selected_team}" if have_team else "Remove…"
        self.disband_button.label = f"Disband {self.selected_team}" if have_team else "Disband…"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This panel belongs to whoever opened it."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(placeholder="Choose a team to manage", row=0)
    async def team_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        value = select.values[0]
        self.selected_team = None if value == "__none__" else value
        self._sync_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Member to assign/remove", row=1)
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        picked = select.values[0]
        self.selected_member = picked if isinstance(picked, discord.Member) else self.guild.get_member(picked.id)
        self._sync_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Assign…", style=discord.ButtonStyle.success, row=2)
    async def assign_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or self.selected_team is None or self.selected_member is None:
            return
        if not await member_has_permission(self.bot, self.guild, actor, "teams.manage"):
            await decline_missing_permission(interaction, "teams.manage")
            return
        if not await check_rate_limit(interaction, "team.assign"):
            return
        await _assign(self.bot, interaction, self.selected_member, self.selected_team)

    @discord.ui.button(label="Remove…", style=discord.ButtonStyle.secondary, row=2)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or self.selected_team is None or self.selected_member is None:
            return
        if not await member_has_permission(self.bot, self.guild, actor, "teams.manage"):
            await decline_missing_permission(interaction, "teams.manage")
            return
        if not await check_rate_limit(interaction, "team.remove"):
            return
        await _remove(self.bot, interaction, self.selected_member, self.selected_team)

    @discord.ui.button(label="Disband…", style=discord.ButtonStyle.danger, row=2)
    async def disband_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or self.selected_team is None:
            return
        if not await member_has_permission(self.bot, self.guild, actor, "teams.manage"):
            await decline_missing_permission(interaction, "teams.manage")
            return
        await _disband(self.bot, interaction, self.selected_team, confirm=False)


class TeamsCog(commands.Cog, name="TeamsCog"):
    """/team — bare shows a manage panel; with a name, creates a team."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _team_names(self, guild: discord.Guild) -> list[str]:
        assert self.bot.db is not None
        rows = await self.bot.db.execute_fetchall(
            "SELECT name FROM teams WHERE guild_id = ? ORDER BY name", (guild.id,)
        )
        return [r["name"] for r in rows]

    @app_commands.command(name="team", description="Manage teams, or create a new one by name.")
    @app_commands.describe(name="Team name to create — omit to open the manage panel")
    @rate_limited()
    async def team(self, interaction: discord.Interaction, name: str | None = None) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None

        if name is None:
            names = await self._team_names(guild)
            view = TeamPanelView(self.bot, guild, interaction.user.id, names)
            await interaction.response.send_message(
                embed=voice.embed("Teams", "\n".join(f"- {n}" for n in names) or "No teams yet — run `/team <name>` to stand one up."),
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return

        actor = interaction.user
        if not isinstance(actor, discord.Member):
            return
        if not await member_has_permission(self.bot, guild, actor, "teams.manage"):
            await decline_missing_permission(interaction, "teams.manage")
            return

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
            # would be invisible to /team and leak channels forever.
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

        names = await self._team_names(guild)
        view = TeamPanelView(self.bot, guild, interaction.user.id, names, selected=name)
        await interaction.followup.send(
            f"Stood up **{name}** — role, category, and channels are live.", view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def disband(self, interaction: discord.Interaction, name: str, confirm: bool = False) -> None:
        """Kept for /admin team-disband — the raw fallback for when the
        panel's Disband button isn't available."""
        await _disband(self.bot, interaction, name, confirm=confirm)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # already messaged + logged inside the failing check
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamsCog(bot))
