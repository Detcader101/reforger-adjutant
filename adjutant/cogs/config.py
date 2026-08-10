"""/config — see and change every /setup-managed setting afterwards.

/setup (setup.py) is the one-shot wizard; this cog is its lifelong sibling:
nothing here scaffolds Discord objects (roles, categories, channels), it
only edits rows the wizard already created. Every write goes through
services/guilds.py's ensure_guild() first since a guild can reach /config
before ever running /setup.
"""

from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from ..services import config as config_service
from ..services import guilds as guilds_service
from ..services import ranks as ranks_service
from .admin import fetchone, note_audit
from .roles import load_ladder, load_permission_overrides

log = logging.getLogger(__name__)

FEATURE_OPTIONS = [
    discord.SelectOption(label="Teams", value="teams", description="Locked team roles + channels"),
    discord.SelectOption(label="Events", value="events", description="Ops/events with signup and reminders"),
    discord.SelectOption(label="Map", value="map", description="Rendered map messages with markers"),
    discord.SelectOption(label="Server Link", value="serverlink", description="Game-server status integration"),
]
_FEATURE_LABELS = {o.value: o.label for o in FEATURE_OPTIONS}

_ON_OFF_CHOICES = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]
_FEATURE_CHOICES = [app_commands.Choice(name=o.label, value=o.value) for o in FEATURE_OPTIONS]


async def _build_summary(bot: commands.Bot, guild: discord.Guild) -> tuple[str, bool]:
    """Everything /config show and /config panel display, rendered as one
    embed body. Re-read from the DB on every call so the panel can call
    this again after each edit and show current truth, not stale state."""
    assert bot.db is not None
    await guilds_service.ensure_guild(bot.db, guild.id)
    row = await fetchone(bot.db, "SELECT * FROM guilds WHERE guild_id = ?", (guild.id,))
    assert row is not None
    minimal = bool(row["minimal_mode"])

    enabled = config_service.enabled_features(row["features"])
    feature_lines = "\n".join(
        f"- {label}: {'on' if key in enabled else 'off'}" for key, label in _FEATURE_LABELS.items()
    )
    audit = f"<#{row['audit_channel']}>" if row["audit_channel"] else "not set"

    ladder = await load_ladder(bot.db, guild.id)
    if ladder:
        ladder_lines = "\n".join(
            f"{entry.position}. {entry.name} — <@&{entry.role_id}>"
            for entry in sorted(ladder, key=lambda e: -e.position)
        )
    else:
        ladder_lines = "none configured"

    overrides = await load_permission_overrides(bot.db, guild.id)
    perm_lines = "\n".join(
        f"- {key}: {config_service.rank_name_for_position(ladder, ranks_service.get_min_rank(key, overrides))}"
        for key in sorted(ranks_service.DEFAULT_PERMISSIONS)
    )

    lines = (
        f"**Features**\n{feature_lines}\n\n"
        f"**Audit channel:** {audit}\n"
        f"**Minimal mode:** {'on' if minimal else 'off'}\n\n"
        f"**Rank ladder**\n{ladder_lines}\n\n"
        f"**Permission thresholds**\n{perm_lines}"
    )
    return lines, minimal


class PermissionThresholdModal(discord.ui.Modal, title="Set Permission Threshold"):
    permission_key_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Permission key", placeholder="e.g. teams.manage"
    )
    min_rank_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Minimum rank position", placeholder="e.g. 3"
    )

    def __init__(self, bot: commands.Bot, guild: discord.Guild, panel: "ConfigPanelView"):
        super().__init__()
        self.bot = bot
        self.guild = guild
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        key = self.permission_key_input.value.strip()
        if not config_service.valid_permission_key(key):
            valid = ", ".join(sorted(ranks_service.DEFAULT_PERMISSIONS))
            await interaction.response.send_message(
                voice.decline(f"`{key}` isn't a recognised permission. Valid keys: {valid}."), ephemeral=True
            )
            return

        raw_rank = self.min_rank_input.value.strip()
        try:
            position = int(raw_rank)
        except ValueError:
            await interaction.response.send_message(
                voice.decline(f"`{raw_rank}` isn't a whole number."), ephemeral=True
            )
            return

        assert self.bot.db is not None
        ladder = await load_ladder(self.bot.db, self.guild.id)
        if not config_service.valid_rank_position(position, ladder):
            detail = (
                f"Valid ranks: {', '.join(f'{e.position} ({e.name})' for e in sorted(ladder, key=lambda e: e.position))}."
                if ladder else "No rank ladder is configured yet — add one with `/rank ladder-add`."
            )
            await interaction.response.send_message(
                voice.decline(f"Position {position} isn't on this guild's ladder. {detail}"), ephemeral=True
            )
            return

        await guilds_service.ensure_guild(self.bot.db, self.guild.id)
        await self.bot.db.execute(
            "INSERT INTO permissions (guild_id, permission, min_rank) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, permission) DO UPDATE SET min_rank = excluded.min_rank",
            (self.guild.id, key, position),
        )
        await self.bot.db.commit()
        rank_name = config_service.rank_name_for_position(ladder, position)
        await note_audit(
            self.bot, self.guild.id, f"Config: `{key}` threshold set to {rank_name} by <@{interaction.user.id}>."
        )
        await self.panel.refresh(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class ConfigPanelView(view_util.ErrorHandledView):
    """Live editing surface for /config panel.

    Every control writes to the DB immediately and re-renders in place —
    there is no Save button. A Save button would need to buffer changes
    somewhere, and this view can end without a clean goodbye (300s
    timeout, or the admin just closing Discord); a buffered-but-unsaved
    edit would then vanish silently, and the admin would have no way to
    tell "I changed it but it didn't save" from "I never changed it".
    Writing on every interaction means whatever is on screen is always
    exactly what's live — /config show a minute later can never disagree
    with what this panel last displayed.
    """

    def __init__(self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None
        self._minimal = False
        self._sync_labels()

    def _sync_labels(self) -> None:
        self.minimal_toggle.label = f"Minimal mode: {'ON' if self._minimal else 'OFF'}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This panel belongs to whoever ran `/config panel`."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Config panel timed out. Nothing is lost — every change was saved the moment "
                    "you made it. Run `/config panel` again to keep editing.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Re-render the embed from current DB state and push it back onto
        the panel message — via edit_message if this callback hasn't
        responded yet (selects/buttons, and modal submissions launched
        from this view's own button), else via the followup."""
        lines, minimal = await _build_summary(self.bot, self.guild)
        self._minimal = minimal
        self._sync_labels()
        embed = voice.embed("Configuration", lines, minimal=minimal)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.select(
        placeholder="Toggle features", min_values=0, max_values=len(FEATURE_OPTIONS), options=FEATURE_OPTIONS
    )
    async def feature_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        assert self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, self.guild.id)
        row = await fetchone(self.bot.db, "SELECT features FROM guilds WHERE guild_id = ?", (self.guild.id,))
        features = config_service.parse_features(row["features"] if row else None)
        chosen = set(select.values)
        for key in _FEATURE_LABELS:
            features[key] = key in chosen
        await self.bot.db.execute(
            "UPDATE guilds SET features = ? WHERE guild_id = ?", (json.dumps(features), self.guild.id)
        )
        await self.bot.db.commit()
        await note_audit(
            self.bot, self.guild.id,
            f"Config: features set to [{', '.join(sorted(chosen)) or 'none'}] by <@{interaction.user.id}>.",
        )
        await self.refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Set audit channel (clears if you pick none)",
        channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=1,
    )
    async def audit_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        assert self.bot.db is not None
        channel_id = select.values[0].id if select.values else None
        await guilds_service.ensure_guild(self.bot.db, self.guild.id)
        await self.bot.db.execute(
            "UPDATE guilds SET audit_channel = ? WHERE guild_id = ?", (channel_id, self.guild.id)
        )
        await self.bot.db.commit()
        detail = f"<#{channel_id}>" if channel_id else "cleared"
        await note_audit(self.bot, self.guild.id, f"Config: audit channel {detail} by <@{interaction.user.id}>.")
        await self.refresh(interaction)

    @discord.ui.button(label="Clear audit channel", style=discord.ButtonStyle.secondary, row=2)
    async def clear_audit_channel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, self.guild.id)
        await self.bot.db.execute("UPDATE guilds SET audit_channel = NULL WHERE guild_id = ?", (self.guild.id,))
        await self.bot.db.commit()
        await note_audit(self.bot, self.guild.id, f"Config: audit channel cleared by <@{interaction.user.id}>.")
        await self.refresh(interaction)

    @discord.ui.button(label="Minimal mode: OFF", style=discord.ButtonStyle.secondary, row=2)
    async def minimal_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        assert self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, self.guild.id)
        row = await fetchone(self.bot.db, "SELECT minimal_mode FROM guilds WHERE guild_id = ?", (self.guild.id,))
        new_value = not bool(row["minimal_mode"]) if row is not None else True
        await self.bot.db.execute(
            "UPDATE guilds SET minimal_mode = ? WHERE guild_id = ?", (int(new_value), self.guild.id)
        )
        await self.bot.db.commit()
        await note_audit(
            self.bot, self.guild.id, f"Config: minimal mode {'on' if new_value else 'off'} by <@{interaction.user.id}>."
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Set permission threshold…", style=discord.ButtonStyle.primary, row=3)
    async def set_permission(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PermissionThresholdModal(self.bot, self.guild, self))


class ResetConfirmModal(discord.ui.Modal, title="Confirm Config Reset"):
    guild_name_input: discord.ui.TextInput = discord.ui.TextInput(label="Type this server's exact name to confirm")

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        super().__init__()
        self.bot = bot
        self.guild = guild
        self.guild_name_input.placeholder = guild.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.guild_name_input.value != self.guild.name:
            await interaction.response.send_message(
                voice.decline("That didn't match the server name exactly. Reset cancelled — nothing changed."),
                ephemeral=True,
            )
            return
        assert self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, self.guild.id)
        await self.bot.db.execute("DELETE FROM permissions WHERE guild_id = ?", (self.guild.id,))
        await self.bot.db.execute("UPDATE guilds SET minimal_mode = 0 WHERE guild_id = ?", (self.guild.id,))
        await self.bot.db.commit()
        await note_audit(
            self.bot, self.guild.id, f"Config: reset to default thresholds by <@{interaction.user.id}>."
        )
        await interaction.response.send_message(
            "Reset complete. Permission thresholds are back to their defaults and minimal mode is off. "
            "Ranks, teams, events, maps and channels were left exactly as they were.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class ResetConfirmView(view_util.ErrorHandledView):
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
                await self.message.edit(content="Reset timed out. Nothing was changed.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Continue to confirmation", style=discord.ButtonStyle.danger)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.send_modal(ResetConfirmModal(self.bot, self.guild))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Stood down. Nothing changed.", view=None)


class ConfigCog(commands.Cog, name="ConfigCog"):
    """No direct slash commands any more — /config's old surface now lives
    behind /adjutant's Config button (`panel`, below) and /admin's raw
    fallbacks (feature/audit_channel/minimal/permission/reset). Every
    caller is responsible for its own admin recheck at the point it calls
    in here (the /admin subcommands via @require_admin(), the hub button
    via roles.decline_admin_only before it calls `panel`)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        lines, minimal = await _build_summary(self.bot, guild)
        await interaction.response.send_message(
            embed=voice.embed("Configuration", lines, minimal=minimal), ephemeral=True
        )

    async def panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        lines, minimal = await _build_summary(self.bot, guild)
        view = ConfigPanelView(self.bot, guild, interaction.user.id)
        view._minimal = minimal
        view._sync_labels()
        await interaction.response.send_message(
            embed=voice.embed("Configuration", lines, minimal=minimal), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def feature(
        self, interaction: discord.Interaction, feature: app_commands.Choice[str], state: app_commands.Choice[str]
    ) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, guild.id)
        row = await fetchone(self.bot.db, "SELECT features FROM guilds WHERE guild_id = ?", (guild.id,))
        new_json = config_service.set_feature(row["features"] if row else None, feature.value, state.value == "on")
        await self.bot.db.execute("UPDATE guilds SET features = ? WHERE guild_id = ?", (new_json, guild.id))
        await self.bot.db.commit()
        await note_audit(
            self.bot, guild.id, f"Config: `{feature.value}` set {state.value} by <@{interaction.user.id}>."
        )
        await interaction.response.send_message(f"Done. `{feature.value}` is now {state.value}.", ephemeral=True)

    async def audit_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, guild.id)
        channel_id = channel.id if channel is not None else None
        await self.bot.db.execute(
            "UPDATE guilds SET audit_channel = ? WHERE guild_id = ?", (channel_id, guild.id)
        )
        await self.bot.db.commit()
        detail = channel.mention if channel is not None else "cleared"
        await note_audit(self.bot, guild.id, f"Config: audit channel {detail} by <@{interaction.user.id}>.")
        await interaction.response.send_message(f"Done. Audit channel {detail}.", ephemeral=True)

    async def minimal(self, interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        await guilds_service.ensure_guild(self.bot.db, guild.id)
        await self.bot.db.execute(
            "UPDATE guilds SET minimal_mode = ? WHERE guild_id = ?", (1 if state.value == "on" else 0, guild.id)
        )
        await self.bot.db.commit()
        await note_audit(self.bot, guild.id, f"Config: minimal mode {state.value} by <@{interaction.user.id}>.")
        await interaction.response.send_message(f"Done. Minimal mode is now {state.value}.", ephemeral=True)

    async def permission(self, interaction: discord.Interaction, key: str, min_rank: int) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        if not config_service.valid_permission_key(key):
            valid = ", ".join(sorted(ranks_service.DEFAULT_PERMISSIONS))
            await interaction.response.send_message(
                voice.decline(f"`{key}` isn't a recognised permission. Valid keys: {valid}."), ephemeral=True
            )
            return

        ladder = await load_ladder(self.bot.db, guild.id)
        if not config_service.valid_rank_position(min_rank, ladder):
            detail = (
                f"Valid ranks: {', '.join(f'{e.position} ({e.name})' for e in sorted(ladder, key=lambda e: e.position))}."
                if ladder else "No rank ladder is configured yet — add one with `/rank ladder-add`."
            )
            await interaction.response.send_message(
                voice.decline(f"Position {min_rank} isn't on this guild's ladder. {detail}"), ephemeral=True
            )
            return

        await guilds_service.ensure_guild(self.bot.db, guild.id)
        await self.bot.db.execute(
            "INSERT INTO permissions (guild_id, permission, min_rank) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, permission) DO UPDATE SET min_rank = excluded.min_rank",
            (guild.id, key, min_rank),
        )
        await self.bot.db.commit()
        rank_name = config_service.rank_name_for_position(ladder, min_rank)
        await note_audit(
            self.bot, guild.id, f"Config: `{key}` threshold set to {rank_name} by <@{interaction.user.id}>."
        )
        await interaction.response.send_message(f"Done. `{key}` now requires **{rank_name}** or higher.", ephemeral=True)

    async def reset(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        view = ResetConfirmView(self.bot, guild, interaction.user.id)
        await interaction.response.send_message(
            f"This restores default permission thresholds and turns minimal mode off in **{guild.name}**. "
            "Ranks, teams, events, maps and channels are left untouched. Continue?",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConfigCog(bot))
