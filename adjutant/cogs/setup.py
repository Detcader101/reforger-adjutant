"""Guided setup wizard, config display, and guarded teardown.

Note on command shape: Discord doesn't allow a command name to be both
directly invocable and the parent of subcommands, so the brief's "/setup"
(wizard) + "/setup show" (status) becomes the group "/setup" with
subcommands "run" and "show" here. /teardown stays a flat top-level command.
"""

from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from .admin import fetchone
from .roles import load_ladder, require_admin

log = logging.getLogger(__name__)

# Default rank ladder offered by the wizard: position -> display name.
DEFAULT_LADDER = (
    (0, "Recruit"),
    (1, "Private"),
    (2, "NCO"),
    (3, "Officer"),
    (4, "Command"),
)

FEATURE_OPTIONS = [
    discord.SelectOption(label="Teams", value="teams", description="Locked team roles + channels"),
    discord.SelectOption(label="Events", value="events", description="Ops/events with signup and reminders"),
    discord.SelectOption(label="Map", value="map", description="Rendered map messages with markers"),
    discord.SelectOption(label="Server Link", value="serverlink", description="Game-server status integration"),
]
_VALID_FEATURES = {option.value for option in FEATURE_OPTIONS}


async def _save_guild_config(
    bot: commands.Bot, guild_id: int, *, minimal_mode: bool, audit_channel_id: int | None, features: set[str]
) -> None:
    assert bot.db is not None
    features_json = json.dumps({feature: True for feature in features})
    await bot.db.execute(
        "INSERT INTO guilds (guild_id, minimal_mode, audit_channel, features) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET minimal_mode = excluded.minimal_mode, "
        "audit_channel = excluded.audit_channel, features = excluded.features",
        (guild_id, int(minimal_mode), audit_channel_id, features_json),
    )
    await bot.db.commit()


async def _create_default_ladder(bot: commands.Bot, guild: discord.Guild) -> list[discord.Role]:
    assert bot.db is not None
    created: list[discord.Role] = []
    for position, name in DEFAULT_LADDER:
        try:
            role = await guild.create_role(name=name, reason="Adjutant: default rank ladder from /setup run")
        except discord.Forbidden:
            log.warning("Missing permission to create default ladder role %s in guild %s", name, guild.id)
            break
        await bot.db.execute(
            "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET position = excluded.position, name = excluded.name, bot_created = 1",
            (guild.id, role.id, position, name),
        )
        created.append(role)
    await bot.db.commit()
    return created


class SetupView(view_util.ErrorHandledView):
    """Stateful wizard: builds up guild config across several interactions
    on one ephemeral message, then writes it all on Finish."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.invoker_id = invoker_id
        self.features: set[str] = set()
        self.audit_channel_id: int | None = None
        self.minimal_mode = False
        self.create_default_ladder = False
        self.message: discord.Message | None = None
        self._sync_labels()

    def _sync_labels(self) -> None:
        self.minimal_toggle.label = f"Minimal mode: {'ON' if self.minimal_mode else 'OFF'}"
        self.ladder_toggle.label = f"Default ladder: {'YES' if self.create_default_ladder else 'NO'}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This wizard belongs to whoever ran `/setup run`."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(content="Setup timed out. Run `/setup run` again when ready.", embed=None, view=None)
            except discord.HTTPException:
                pass

    def summary_embed(self) -> discord.Embed:
        features = ", ".join(sorted(self.features)) or "none selected"
        audit = f"<#{self.audit_channel_id}>" if self.audit_channel_id else "not set"
        lines = (
            f"**Features:** {features}\n"
            f"**Audit channel:** {audit}\n"
            f"**Minimal mode:** {'on' if self.minimal_mode else 'off'}\n"
            f"**Default rank ladder:** {'yes' if self.create_default_ladder else 'no'}"
        )
        return voice.embed("Setup", lines, minimal=self.minimal_mode)

    @discord.ui.select(placeholder="Choose features to enable", min_values=0, max_values=len(FEATURE_OPTIONS), options=FEATURE_OPTIONS)
    async def feature_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.features = set(select.values)
        await interaction.response.edit_message(embed=self.summary_embed())

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Audit log channel (optional)",
                        channel_types=[discord.ChannelType.text], min_values=0, max_values=1)
    async def audit_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        self.audit_channel_id = select.values[0].id if select.values else None
        await interaction.response.edit_message(embed=self.summary_embed())

    @discord.ui.button(label="Minimal mode: OFF", style=discord.ButtonStyle.secondary, row=2)
    async def minimal_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.minimal_mode = not self.minimal_mode
        self._sync_labels()
        await interaction.response.edit_message(embed=self.summary_embed(), view=self)

    @discord.ui.button(label="Default ladder: NO", style=discord.ButtonStyle.secondary, row=2)
    async def ladder_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.create_default_ladder = not self.create_default_ladder
        self._sync_labels()
        await interaction.response.edit_message(embed=self.summary_embed(), view=self)

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.success, row=3)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _save_guild_config(
            self.bot, self.guild.id,
            minimal_mode=self.minimal_mode, audit_channel_id=self.audit_channel_id, features=self.features,
        )

        created_ranks: list[discord.Role] = []
        if self.create_default_ladder:
            created_ranks = await _create_default_ladder(self.bot, self.guild)

        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self.stop()
        note = f" Default ladder created: {', '.join(r.name for r in created_ranks)}." if created_ranks else ""
        await interaction.response.edit_message(content=f"Configuration saved.{note}", embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Setup cancelled. Nothing was changed.", embed=None, view=None)


class SetupCog(commands.GroupCog, group_name="setup"):
    """/setup run — guided wizard. /setup show — current config."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="run", description="Guided setup wizard: pick features, audit channel, and options.")
    @require_admin()
    async def run(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        view = SetupView(self.bot, guild, interaction.user.id)
        await interaction.response.send_message(embed=view.summary_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="show", description="Show this guild's current adjutant configuration.")
    @require_admin()
    async def show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        row = await fetchone(self.bot.db, "SELECT * FROM guilds WHERE guild_id = ?", (guild.id,))
        if row is None:
            await interaction.response.send_message(
                voice.broken("This guild hasn't been set up yet.", "Run `/setup run` to get started."), ephemeral=True
            )
            return
        features = json.loads(row["features"] or "{}")
        enabled = ", ".join(sorted(k for k, v in features.items() if v)) or "none"
        audit = f"<#{row['audit_channel']}>" if row["audit_channel"] else "not set"
        ladder = await load_ladder(self.bot.db, guild.id)
        ladder_summary = f"{len(ladder)} rank(s) configured" if ladder else "none configured"
        lines = (
            f"**Features:** {enabled}\n"
            f"**Audit channel:** {audit}\n"
            f"**Minimal mode:** {'on' if row['minimal_mode'] else 'off'}\n"
            f"**Rank ladder:** {ladder_summary}"
        )
        await interaction.response.send_message(
            embed=voice.embed("Current Setup", lines, minimal=bool(row["minimal_mode"])), ephemeral=True
        )

    @app_commands.command(name="quick", description="Configure without the wizard UI — for when components aren't working.")
    @app_commands.describe(
        features="Comma-separated: teams, events, map, serverlink (omit for none)",
        audit_channel="Audit log channel",
        minimal_mode="Strip decorative output",
        create_default_ladder="Also create the default Recruit-through-Command rank ladder",
    )
    @require_admin()
    async def quick(
        self,
        interaction: discord.Interaction,
        features: str = "",
        audit_channel: discord.TextChannel | None = None,
        minimal_mode: bool = False,
        create_default_ladder: bool = False,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        chosen = {f.strip().lower() for f in features.split(",") if f.strip()}
        invalid = chosen - _VALID_FEATURES
        if invalid:
            await interaction.response.send_message(
                voice.decline(
                    f"Unknown feature(s): {', '.join(sorted(invalid))}. Valid: {', '.join(sorted(_VALID_FEATURES))}."
                ),
                ephemeral=True,
            )
            return

        await _save_guild_config(
            self.bot, guild.id,
            minimal_mode=minimal_mode, audit_channel_id=audit_channel.id if audit_channel else None, features=chosen,
        )
        created_ranks: list[discord.Role] = []
        if create_default_ladder:
            created_ranks = await _create_default_ladder(self.bot, guild)
        note = f" Default ladder created: {', '.join(r.name for r in created_ranks)}." if created_ranks else ""
        await interaction.response.send_message(f"Configuration saved.{note}", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


class TeardownConfirmModal(discord.ui.Modal, title="Confirm Teardown"):
    guild_name_input: discord.ui.TextInput = discord.ui.TextInput(label="Type this server's exact name to confirm")

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        super().__init__()
        self.bot = bot
        self.guild = guild
        self.guild_name_input.placeholder = guild.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.guild_name_input.value != self.guild.name:
            await interaction.response.send_message(
                voice.decline("That didn't match the server name exactly. Teardown cancelled — nothing changed."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        summary = await _teardown(self.bot, self.guild)
        await interaction.followup.send(f"Teardown complete. {summary}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class TeardownConfirmView(view_util.ErrorHandledView):
    def __init__(self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(content="Teardown timed out. Nothing was changed.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Continue to confirmation", style=discord.ButtonStyle.danger)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.send_modal(TeardownConfirmModal(self.bot, self.guild))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Stood down. Nothing changed.", view=None)


async def _teardown(bot: commands.Bot, guild: discord.Guild) -> str:
    assert bot.db is not None
    teams = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ?", (guild.id,))
    removed_teams = 0
    for team in teams:
        category = guild.get_channel(team["category_id"])
        role = guild.get_role(team["role_id"])
        try:
            if isinstance(category, discord.CategoryChannel):
                for channel in list(category.channels):
                    await channel.delete(reason="Adjutant: guild teardown")
                await category.delete(reason="Adjutant: guild teardown")
            if role is not None:
                await role.delete(reason="Adjutant: guild teardown")
            removed_teams += 1
        except discord.Forbidden:
            log.warning("Missing permission to remove team %s during teardown of guild %s", team["name"], guild.id)

    bot_ranks = await bot.db.execute_fetchall(
        "SELECT * FROM ranks WHERE guild_id = ? AND bot_created = 1", (guild.id,)
    )
    removed_ranks = 0
    for rank in bot_ranks:
        role = guild.get_role(rank["role_id"])
        if role is not None:
            try:
                await role.delete(reason="Adjutant: guild teardown")
                removed_ranks += 1
            except discord.Forbidden:
                log.warning("Missing permission to remove rank role %s during teardown of guild %s", rank["name"], guild.id)

    # guilds row deletion cascades to ranks/permissions/role_grants/teams/
    # events/maps/server_links (all FK ON DELETE CASCADE). incidents is
    # intentionally left alone — it's an audit trail, not live config.
    await bot.db.execute("DELETE FROM guilds WHERE guild_id = ?", (guild.id,))
    await bot.db.commit()
    return f"Removed {removed_teams} team(s) and {removed_ranks} bot-created rank role(s). Configuration cleared."


class TeardownCog(commands.Cog, name="teardown"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="teardown", description="Remove all bot-created roles/categories for this guild.")
    @require_admin()
    async def teardown(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        view = TeardownConfirmView(self.bot, guild, interaction.user.id)
        await interaction.response.send_message(
            f"This strips out every team role/category and any bot-created rank roles in **{guild.name}**, "
            "and clears its adjutant configuration. That is not reversible.\n\nContinue?",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
    await bot.add_cog(TeardownCog(bot))
