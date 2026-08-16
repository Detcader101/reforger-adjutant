"""Guided setup wizard, plus the helpers its former subcommands folded into.

/setup is a single bare command now: it opens the same guided panel as the
/adjutant hub's Setup button (see SetupView below). What used to be /setup
show|check|ranks|quick lives on as importable helpers here, reached through
the hub's Config/Ranks/Diagnostics buttons or, when those aren't working,
/admin's raw fallbacks (adjutant/cogs/admin.py) — config-show wasn't added
there since /adjutant's Config button already renders a superset via
config.py's _build_summary. /teardown moved the same way: its confirm
view/modal and _teardown() stay here, unchanged, but the command entry
point is now /admin teardown.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from ..services import templates as templates_service
from .admin import minimal_mode, note_audit
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
    discord.SelectOption(
        label="Events", value="events", description="Ops/events with signup and reminders"
    ),
    discord.SelectOption(
        label="Map", value="map", description="Rendered map messages with markers"
    ),
    discord.SelectOption(
        label="Server Link", value="serverlink", description="Game-server status integration"
    ),
]
_VALID_FEATURES = {option.value for option in FEATURE_OPTIONS}

TEMPLATE_OPTIONS = [
    discord.SelectOption(
        label=tmpl.label,
        value=tmpl.key,
        description=tmpl.description,
        default=(tmpl.key == templates_service.DEFAULT_TEMPLATE_KEY),
    )
    for tmpl in templates_service.TEMPLATES.values()
]

# Permission list + what each one unlocks, in plain terms. Mirrors
# tools/probe_guild.py's NEEDED tuple (its read-only diagnostic checks the
# same bits) — /setup check is the in-Discord, admin-facing version of that.
# Deliberately excludes administrator: this bot is designed to run without
# it, so the preflight only ever names the specific permission that's short.
REQUIRED_PERMISSIONS: tuple[tuple[str, str], ...] = (
    (
        "view_channel",
        "I can't see channels at all — nearly everything breaks, since Discord hides a "
        "channel from anyone (bot included) whose effective view_channel resolves false.",
    ),
    (
        "manage_roles",
        "I can't create or manage rank and team roles; the ladder tools in /setup, /rank, "
        "and /team create will decline.",
    ),
    (
        "manage_channels",
        "I can't create or manage team and template channels/categories; /team create "
        "and channel-creating setup templates will decline.",
    ),
    ("send_messages", "I can't post replies, announcements, or audit notes at all."),
    ("embed_links", "I can't show the styled embeds used throughout, including this report."),
    ("attach_files", "I can't post rendered map images."),
    (
        "read_message_history",
        "I can't edit my own panel messages (map renders, event posts) in place, "
        "so they'd get reposted instead of updated.",
    ),
    (
        "connect",
        "I can't grant myself connect in a team's locked voice channel — Discord refuses an "
        "overwrite granting a permission I don't already hold myself.",
    ),
    ("speak", "I can't grant myself speak in a team's locked voice channel, for the same reason."),
)


def _missing_permissions(perms: discord.Permissions) -> list[tuple[str, str]]:
    return [(perm, why) for perm, why in REQUIRED_PERMISSIONS if not getattr(perms, perm)]


def _role_hierarchy_ok(guild: discord.Guild) -> bool:
    """Whether the bot's top role sits above every other role it might need
    to manage (rank roles, team roles). If it doesn't, Discord silently
    refuses role edits/deletes against anything at or above it."""
    me = guild.me
    if me is None or me.top_role is None:
        return True  # can't assess without a bot member; don't false-alarm
    others = [r.position for r in guild.roles if r != me.top_role and r != guild.default_role]
    highest_other = max(others, default=0)
    return me.top_role.position > highest_other


def _blocked_channels(guild: discord.Guild) -> list:
    """Channels whose overwrites resolve view_channel False for the bot.
    Discord blocks every action (read, manage, delete) once that resolves
    false, so these are invisible dead spots only an owner/admin can clear
    by editing or removing the offending overwrite."""
    me = guild.me
    if me is None:
        return []
    channels = getattr(guild, "channels", None)
    if channels is None:
        channels = list(getattr(guild, "text_channels", [])) + list(
            getattr(guild, "categories", [])
        )
    blocked = []
    for channel in channels:
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for is None:
            continue
        perms = permissions_for(me)
        if not perms.view_channel:
            blocked.append(channel)
    return blocked


def _preflight_embed(guild: discord.Guild, *, minimal: bool) -> discord.Embed:
    me = guild.me
    perms = getattr(me, "guild_permissions", None) if me is not None else None
    if perms is None:
        perms = discord.Permissions.none()

    missing = _missing_permissions(perms)
    held = [perm for perm, _why in REQUIRED_PERMISSIONS if (perm, _why) not in missing]
    hierarchy_ok = _role_hierarchy_ok(guild)
    blocked = _blocked_channels(guild)

    lines: list[str] = []
    if missing:
        lines.append("**Missing permissions:**")
        for perm, why in missing:
            lines.append(f"- `{perm}` — {why}")
    else:
        lines.append("**Permissions:** all present.")
    if held:
        lines.append(f"**Held:** {', '.join(f'`{p}`' for p in held)}")

    lines.append("")
    if hierarchy_ok:
        lines.append("**Role hierarchy:** my role sits above the roles I'd manage.")
    else:
        lines.append(
            "**Role hierarchy:** my role does **not** sit above every other role. "
            "An admin needs to drag it up the role list, or role edits/deletes below it will fail."
        )

    lines.append("")
    if blocked:
        names = ", ".join(f"**{getattr(c, 'name', c)}**" for c in blocked)
        lines.append(
            f"**Blind spots:** {names} — an overwrite there denies me view_channel, so it's invisible "
            "to me and I can't manage or delete it. An owner or admin needs to remove or adjust that "
            "overwrite; I can't fix it myself."
        )
    else:
        lines.append("**Blind spots:** none found.")

    lines.append("")
    lines.append(
        "I'm designed to run without Administrator, deliberately. That's never the fix for anything "
        "above — grant the specific permission named instead."
    )

    trouble = bool(missing) or not hierarchy_ok or bool(blocked)
    return voice.embed(
        "Setup Preflight",
        "\n".join(lines),
        colour=voice.COLOUR_ALERT if trouble else voice.COLOUR_PRIMARY,
        minimal=minimal,
    )


async def _save_guild_config(
    bot: commands.Bot,
    guild_id: int,
    *,
    minimal_mode: bool,
    audit_channel_id: int | None,
    features: set[str],
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


async def _upsert_ladder(
    bot: commands.Bot, guild: discord.Guild, ladder: Sequence[tuple[int, str]], *, reason: str
) -> list[discord.Role]:
    """Create-or-reuse a role for each (position, name) and upsert its ranks
    row. Additive: rows for ranks not present in `ladder` are left alone —
    that's what makes re-running /setup (or /admin setup-quick, or applying
    a template on top of a manually-extended ladder) always safe."""
    assert bot.db is not None
    created: list[discord.Role] = []
    for position, name in ladder:
        # Re-running /setup is common ("did that work?"). Creating blindly
        # would leave the server with two roles of the same name and a
        # rank table with two entries per position.
        role = discord.utils.get(guild.roles, name=name)
        made_it_now = role is None
        if role is None:
            try:
                role = await guild.create_role(name=name, reason=reason)
            except discord.Forbidden:
                log.warning(
                    "Missing permission to create ladder role %s in guild %s", name, guild.id
                )
                break
        # bot_created is deliberately left out of the UPDATE clause: it must
        # keep its original value so /teardown never deletes a rank role the
        # admin made themselves and we merely adopted.
        await bot.db.execute(
            "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET position = excluded.position, name = excluded.name",
            (guild.id, role.id, position, name, 1 if made_it_now else 0),
        )
        created.append(role)
    await bot.db.commit()
    return created


async def _create_default_ladder(bot: commands.Bot, guild: discord.Guild) -> list[discord.Role]:
    return await _upsert_ladder(
        bot, guild, DEFAULT_LADDER, reason="Adjutant: default rank ladder from /setup"
    )


async def _apply_custom_ladder(
    bot: commands.Bot, guild: discord.Guild, names: list[str]
) -> list[discord.Role]:
    """Replace the guild's entire rank ladder with `names` (lowest-first).

    Unlike _upsert_ladder (additive, used by templates/default), this is a
    full replace: submitting a new ladder (via /admin ranks or the Ranks
    editor) means "here is my new ladder", so stale rows for ranks no
    longer in the list are dropped. The Discord roles behind them are
    never touched — only the bot's own bookkeeping changes.

    Reuses same-named existing roles, creates missing ones, and — critically
    — carries forward bot_created for any role already tracked, so editing
    the ladder again never un-marks a role the bot itself made, which would
    let /teardown skip cleaning it up.
    """
    assert bot.db is not None
    old_rows = await bot.db.execute_fetchall(
        "SELECT role_id, bot_created FROM ranks WHERE guild_id = ?", (guild.id,)
    )
    previously_bot_created = {row["role_id"]: row["bot_created"] for row in old_rows}

    roles: list[discord.Role] = []
    new_rows: list[tuple[int, int, int, str, int]] = []
    for position, name in enumerate(names):
        role = discord.utils.get(guild.roles, name=name)
        made_it_now = role is None
        if role is None:
            try:
                role = await guild.create_role(
                    name=name, reason="Adjutant: custom rank ladder from /admin ranks"
                )
            except discord.Forbidden:
                log.warning(
                    "Missing permission to create ladder role %s in guild %s", name, guild.id
                )
                break
        bot_created = 1 if made_it_now else previously_bot_created.get(role.id, 0)
        new_rows.append((guild.id, role.id, position, name, bot_created))
        roles.append(role)

    await bot.db.execute("DELETE FROM ranks WHERE guild_id = ?", (guild.id,))
    for row in new_rows:
        await bot.db.execute(
            "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, ?)",
            row,
        )
    await bot.db.commit()
    return roles


async def _validate_and_apply_custom_ladder(
    bot: commands.Bot, guild: discord.Guild, names: list[str]
) -> tuple[list[discord.Role] | None, list[str]]:
    """Shared by RankLadderModal (opened from the /adjutant hub's Ranks
    button) and /admin ranks, its flat-command fallback. Returns
    (roles, problems) — exactly one of which is non-empty/None."""
    problems = templates_service.validate_ladder_names(names)
    if problems:
        return None, problems
    roles = await _apply_custom_ladder(bot, guild, names)
    await note_audit(bot, guild.id, f"Rank ladder updated: {', '.join(r.name for r in roles)}.")
    return roles, []


async def _apply_channels(
    guild: discord.Guild, specs: Sequence[templates_service.ChannelSpec]
) -> list:
    """Create-or-reuse each channel (and its category) by name — the same
    reuse discipline as _upsert_ladder, so applying a template twice never
    duplicates a category or channel."""
    created: list = []
    categories: dict[str, discord.CategoryChannel] = {}
    for spec in specs:
        category = categories.get(spec.category) or discord.utils.get(
            guild.categories, name=spec.category
        )
        if category is None:
            try:
                category = await guild.create_category(
                    name=spec.category, reason="Adjutant: template scaffold"
                )
            except discord.Forbidden:
                log.warning(
                    "Missing permission to create category %s in guild %s", spec.category, guild.id
                )
                continue
        categories[spec.category] = category

        existing = discord.utils.get(category.channels, name=spec.name)
        if existing is not None:
            created.append(existing)
            continue
        try:
            if spec.kind == "voice":
                channel = await category.create_voice_channel(
                    spec.name, reason="Adjutant: template scaffold"
                )
            else:
                channel = await category.create_text_channel(
                    spec.name, reason="Adjutant: template scaffold"
                )
        except discord.Forbidden:
            log.warning("Missing permission to create channel %s in guild %s", spec.name, guild.id)
            continue
        created.append(channel)
    return created


async def _apply_template(
    bot: commands.Bot, guild: discord.Guild, tmpl: templates_service.Template
) -> tuple[list[discord.Role], list]:
    ranks: list[discord.Role] = []
    if tmpl.ranks:
        ranks = await _upsert_ladder(
            bot,
            guild,
            list(enumerate(tmpl.ranks)),
            reason=f"Adjutant: {tmpl.label} template from /setup",
        )
    channels: list = []
    if tmpl.channels:
        channels = await _apply_channels(guild, tmpl.channels)
    return ranks, channels


def _format_creation_note(ranks: list[discord.Role], channels: list) -> str:
    parts: list[str] = []
    if ranks:
        parts.append(f" Ranks: {', '.join(r.name for r in ranks)}.")
    if channels:
        parts.append(f" Channels: {', '.join(getattr(c, 'name', str(c)) for c in channels)}.")
    return "".join(parts)


async def apply_setup_selection(
    bot: commands.Bot,
    guild: discord.Guild,
    *,
    minimal_mode: bool,
    audit_channel_id: int | None,
    features: set[str],
    template_key: str = templates_service.DEFAULT_TEMPLATE_KEY,
    create_default_ladder: bool = False,
) -> tuple[list[discord.Role], list]:
    """Save guild config, then apply a template — or, when the chosen
    template supplies no ladder of its own, fall back to the legacy
    default-ladder toggle. This is what SetupView's Finish button and
    /admin setup-quick both reduce to; deliberately importable (like
    build_team in teams.py) so either caller can invoke it directly
    instead of re-deriving the template-vs-default-ladder branching.

    Returns (created_ranks, created_channels).
    """
    await _save_guild_config(
        bot,
        guild.id,
        minimal_mode=minimal_mode,
        audit_channel_id=audit_channel_id,
        features=features,
    )
    tmpl = templates_service.TEMPLATES.get(
        template_key, templates_service.TEMPLATES[templates_service.DEFAULT_TEMPLATE_KEY]
    )
    if tmpl.ranks or tmpl.channels:
        return await _apply_template(bot, guild, tmpl)
    if create_default_ladder:
        return await _create_default_ladder(bot, guild), []
    return [], []


class SetupView(view_util.ErrorHandledView):
    """Stateful wizard: builds up guild config across several interactions
    on one ephemeral message, then writes it all on Finish."""

    def __init__(
        self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, timeout: float = 300.0
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.invoker_id = invoker_id
        self.features: set[str] = set()
        self.audit_channel_id: int | None = None
        self.minimal_mode = False
        self.create_default_ladder = False
        self.template_key = templates_service.DEFAULT_TEMPLATE_KEY
        self.message: discord.Message | None = None
        self._sync_labels()

    def _sync_labels(self) -> None:
        self.minimal_toggle.label = f"Minimal mode: {'ON' if self.minimal_mode else 'OFF'}"
        self.ladder_toggle.label = (
            f"Default ladder: {'YES' if self.create_default_ladder else 'NO'}"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This wizard belongs to whoever opened it."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Setup timed out. Run `/setup` again when ready.", embed=None, view=None
                )
            except discord.HTTPException:
                pass

    def summary_embed(self) -> discord.Embed:
        features = ", ".join(sorted(self.features)) or "none selected"
        audit = f"<#{self.audit_channel_id}>" if self.audit_channel_id else "not set"
        tmpl = templates_service.TEMPLATES[self.template_key]
        if tmpl.ranks:
            ladder_line = f"**Rank ladder:** supplied by the {tmpl.label} template"
        else:
            ladder_line = (
                f"**Default rank ladder:** {'yes' if self.create_default_ladder else 'no'}"
            )
        lines = (
            f"**Template:** {tmpl.label} — {tmpl.description}\n"
            f"**Features:** {features}\n"
            f"**Audit channel:** {audit}\n"
            f"**Minimal mode:** {'on' if self.minimal_mode else 'off'}\n"
            f"{ladder_line}"
        )
        return voice.embed("Setup", lines, minimal=self.minimal_mode)

    @discord.ui.select(
        placeholder="Choose a starting template",
        min_values=1,
        max_values=1,
        options=TEMPLATE_OPTIONS,
        row=0,
    )
    async def template_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        self.template_key = select.values[0]
        await interaction.response.edit_message(embed=self.summary_embed())

    @discord.ui.select(
        placeholder="Choose features to enable",
        min_values=0,
        max_values=len(FEATURE_OPTIONS),
        options=FEATURE_OPTIONS,
        row=1,
    )
    async def feature_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        self.features = set(select.values)
        await interaction.response.edit_message(embed=self.summary_embed())

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Audit log channel (optional)",
        channel_types=[discord.ChannelType.text],
        min_values=0,
        max_values=1,
        row=2,
    )
    async def audit_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.audit_channel_id = select.values[0].id if select.values else None
        await interaction.response.edit_message(embed=self.summary_embed())

    @discord.ui.button(label="Minimal mode: OFF", style=discord.ButtonStyle.secondary, row=3)
    async def minimal_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.minimal_mode = not self.minimal_mode
        self._sync_labels()
        await interaction.response.edit_message(embed=self.summary_embed(), view=self)

    @discord.ui.button(label="Default ladder: NO", style=discord.ButtonStyle.secondary, row=3)
    async def ladder_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.create_default_ladder = not self.create_default_ladder
        self._sync_labels()
        await interaction.response.edit_message(embed=self.summary_embed(), view=self)

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.success, row=4)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        created_ranks, created_channels = await apply_setup_selection(
            self.bot,
            self.guild,
            minimal_mode=self.minimal_mode,
            audit_channel_id=self.audit_channel_id,
            features=self.features,
            template_key=self.template_key,
            create_default_ladder=self.create_default_ladder,
        )

        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self.stop()
        note = _format_creation_note(created_ranks, created_channels)
        await note_audit(self.bot, self.guild.id, f"Setup wizard: configuration saved.{note}")
        await interaction.response.edit_message(
            content=f"Configuration saved.{note}", embed=None, view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Setup cancelled. Nothing was changed.", embed=None, view=None
        )


class RankLadderModal(discord.ui.Modal, title="Custom Rank Ladder"):
    ranks_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Ranks, one per line — lowest first",
        style=discord.TextStyle.paragraph,
        placeholder="Recruit\nPrivate\nNCO\nOfficer\nCommand",
        max_length=2000,
    )

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        super().__init__()
        self.bot = bot
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction) -> None:
        names = [line.strip() for line in self.ranks_input.value.splitlines() if line.strip()]
        roles, problems = await _validate_and_apply_custom_ladder(self.bot, self.guild, names)
        if problems:
            await interaction.response.send_message(
                voice.decline("Ladder wasn't applied — " + "; ".join(problems) + "."),
                ephemeral=True,
            )
            return
        assert roles is not None
        await interaction.response.send_message(
            f"Ladder updated, junior to senior: {', '.join(r.name for r in roles)}.", ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


async def ladder_embed(bot: commands.Bot, guild: discord.Guild) -> discord.Embed:
    """The guild's ladder, junior to senior. Importable so /rank, /admin
    ranks and the /adjutant hub all render it identically."""
    assert bot.db is not None
    ladder = await load_ladder(bot.db, guild.id)
    body = (
        "\n".join(
            f"{entry.position}. {entry.name}" for entry in sorted(ladder, key=lambda e: e.position)
        )
        if ladder
        else "No ladder configured yet."
    )
    return voice.embed("Current Rank Ladder", body, minimal=await minimal_mode(bot.db, guild.id))


class RankLadderView(view_util.ErrorHandledView):
    """Shows the current ladder; the button opens the multi-line modal.
    Opened from the /adjutant hub's Ranks button. /admin ranks is the
    slash-command fallback (a `ranks:` argument applies directly and never
    opens this view) — see repo convention in tests/test_cogs_teams.py."""

    def __init__(
        self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, timeout: float = 180.0
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This ladder editor belongs to whoever opened it."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Ladder editor timed out. Reopen it from the Ranks button, or run `/admin ranks` again.",
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Edit ladder", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RankLadderModal(self.bot, self.guild))


class SetupCog(commands.Cog, name="SetupCog"):
    """/setup — bare command, opens the guided setup panel (same SetupView
    the /adjutant hub's Setup button opens). Everything the old subcommand
    group did lives on: /setup show → the hub's Config button (or /admin
    config's equivalent), /setup check → the hub's Diagnostics button or
    /admin preflight, /setup ranks → the hub's Ranks button or /admin ranks,
    /setup quick → /admin setup-quick."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Open the guided setup wizard: pick a template, features, and options.",
    )
    @require_admin()
    async def setup_command(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        view = SetupView(self.bot, guild, interaction.user.id)
        await interaction.response.send_message(
            embed=view.summary_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


class TeardownConfirmModal(discord.ui.Modal, title="Confirm Teardown"):
    guild_name_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Type this server's exact name to confirm"
    )

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        super().__init__()
        self.bot = bot
        self.guild = guild
        self.guild_name_input.placeholder = guild.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.guild_name_input.value != self.guild.name:
            await interaction.response.send_message(
                voice.decline(
                    "That didn't match the server name exactly. Teardown cancelled — nothing changed."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        summary = await _teardown(self.bot, self.guild)
        await interaction.followup.send(f"Teardown complete. {summary}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class TeardownConfirmView(view_util.ErrorHandledView):
    def __init__(
        self, bot: commands.Bot, guild: discord.Guild, invoker_id: int, timeout: float = 60.0
    ):
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
                await self.message.edit(
                    content="Teardown timed out. Nothing was changed.", view=None
                )
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
            log.warning(
                "Missing permission to remove team %s during teardown of guild %s",
                team["name"],
                guild.id,
            )

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
                log.warning(
                    "Missing permission to remove rank role %s during teardown of guild %s",
                    rank["name"],
                    guild.id,
                )

    # guilds row deletion cascades to ranks/permissions/role_grants/teams/
    # events/maps/server_links (all FK ON DELETE CASCADE). incidents is
    # intentionally left alone — it's an audit trail, not live config.
    await bot.db.execute("DELETE FROM guilds WHERE guild_id = ?", (guild.id,))
    await bot.db.commit()
    return f"Removed {removed_teams} team(s) and {removed_ranks} bot-created rank role(s). Configuration cleared."


def teardown_warning(guild: discord.Guild) -> str:
    """Confirmation copy shown before opening TeardownConfirmView. Pulled out
    so /admin teardown (adjutant/cogs/admin.py) doesn't have to re-derive it —
    there's exactly one place this sentence is written."""
    return (
        f"This strips out every team role/category and any bot-created rank roles in **{guild.name}**, "
        "and clears its adjutant configuration. That is not reversible.\n\nContinue?"
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
