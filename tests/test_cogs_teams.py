"""In-process integration tests for adjutant/cogs/teams.py.

/team is the leak-prevention surface SPEC.md calls out explicitly: "Team
channels are locked at the Discord permission layer, not just by
convention." The create() test below is the most rigorous test in this
whole suite for exactly that reason — it doesn't just check the command
replied "success", it inspects the actual discord.PermissionOverwrite
objects handed to guild.create_category and asserts @everyone is denied
view_channel and the team role is granted it.

Command surface: bare `/team` opens a management panel (TeamPanelView) —
a team Select, a member UserSelect, and Assign/Remove/Disband buttons
acting on whatever's currently selected. `/team <name>` creates a team and
replies with that same panel pre-selected on the new team. `/admin
team-disband` is the raw fallback for when the panel's Disband button
isn't available.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakes import (
    FakeGuild,
    forbidden,
    make_interaction,
    make_member,
    reply_ephemeral,
    reply_text,
    seed_guild,
)

from adjutant.cogs.admin import AdminCog
from adjutant.cogs.teams import DisbandConfirmView, TeamPanelView, TeamsCog


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
def cog(bot):
    return bot.register_cog(TeamsCog(bot))


@pytest.fixture
def admin_cog(bot):
    return AdminCog(bot)


async def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


# --------------------------------------------------------------------------- #
# /team <name> — create — leak prevention (the critical test)                 #
# --------------------------------------------------------------------------- #


async def test_create_makes_role_category_text_and_voice_channel(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")

    await TeamsCog.team.callback(cog, interaction, name="Alpha")

    category = next(c for c in guild.categories if c.name == "Alpha")
    assert category is not None
    assert {ch.name for ch in category.channels} == {"chat", "voice"}
    role = next(r for r in guild.roles if r.name == "Team Alpha")
    assert role is not None

    row = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Alpha")
    )
    assert len(row) == 1
    assert row[0]["role_id"] == role.id
    assert row[0]["category_id"] == category.id
    # The success reply carries a panel pre-selected on the team just made.
    view = interaction.followup.messages[-1]["view"]
    assert isinstance(view, TeamPanelView)
    assert view.selected_team == "Alpha"


async def test_create_permission_overwrites_deny_everyone_and_allow_team_role(cog, bot, guild):
    """The leak-prevention guarantee from SPEC.md, checked at the data
    level: @everyone must be explicitly denied view_channel on the team's
    category, and the freshly-created team role must be explicitly granted
    it (plus connect/speak/send_messages) — not merely "not mentioned",
    which in Discord's permission model defaults to inherited/allow and
    would be a silent leak."""
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")

    await TeamsCog.team.callback(cog, interaction, name="Bravo")

    category = next(c for c in guild.categories if c.name == "Bravo")
    role = next(r for r in guild.roles if r.name == "Team Bravo")

    everyone_overwrite = category.overwrites.get(guild.default_role)
    assert everyone_overwrite is not None, (
        "no overwrite at all for @everyone means Discord's default (visible) applies"
    )
    assert everyone_overwrite.view_channel is False

    team_overwrite = category.overwrites.get(role)
    assert team_overwrite is not None
    assert team_overwrite.view_channel is True
    assert team_overwrite.connect is True
    assert team_overwrite.speak is True
    assert team_overwrite.send_messages is True

    # The bot itself must retain access, or it can't manage what it just
    # locked down.
    bot_overwrite = category.overwrites.get(guild.me)
    assert bot_overwrite is not None
    assert bot_overwrite.view_channel is True
    assert bot_overwrite.manage_channels is True
    # ...but NOT manage_roles: Discord refuses an overwrite granting that
    # bit unless the actor is an Administrator, which made every real
    # /team create fail with a misleading "check my role is high enough".
    assert bot_overwrite.manage_roles is None

    # The child text/voice channels must not carry their own overwrite that
    # would override (or accidentally re-open) what the category just
    # locked down — an empty overwrites dict is what makes a channel
    # "synced" to its category in real Discord.
    for channel in category.channels:
        assert channel.overwrites == {}, (
            f"{channel.name} has its own overwrites {channel.overwrites!r} — "
            "these would override the category's deny and could reopen the leak"
        )


async def test_create_declines_a_duplicate_team_name(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    first = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, first, name="Charlie")

    second = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, second, name="Charlie")

    assert "already exists" in reply_text(second)
    assert reply_ephemeral(second) is True
    # only one category was created
    assert len([c for c in guild.categories if c.name == "Charlie"]) == 1


async def test_create_reports_forbidden_cleanly_when_bot_lacks_permission(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    guild.fail_next_create_role(forbidden("Missing Permissions"))
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")

    await TeamsCog.team.callback(cog, interaction, name="Delta")

    assert reply_ephemeral(interaction) is True
    assert (
        "lack permission" in reply_text(interaction)
        or "small snag" in reply_text(interaction).lower()
    )
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Delta")
    )
    assert rows == []


async def test_create_is_refused_for_a_non_privileged_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")  # no rank, not admin
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="team")

    await TeamsCog.team.callback(cog, interaction, name="Echo")

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower() or "rank" in reply_text(interaction).lower()
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "permission_denied" for i in incidents)
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Echo")
    )
    assert rows == []


# --------------------------------------------------------------------------- #
# bare /team — the manage panel                                               #
# --------------------------------------------------------------------------- #


async def _create_team(cog, bot, guild, name="Echo"):
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, interaction, name=name)
    role = next(r for r in guild.roles if r.name == f"Team {name}")
    return role


async def test_bare_team_lists_every_team_on_record(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Foxtrot")
    await _create_team(cog, bot, guild, "Golf")
    viewer = make_member(guild, display_name="Anyone")
    interaction = make_interaction(bot, guild=guild, user=viewer, command_name="team")

    await TeamsCog.team.callback(cog, interaction, name=None)

    assert reply_ephemeral(interaction) is True
    text = reply_text(interaction)
    assert "Foxtrot" in text and "Golf" in text
    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, TeamPanelView)
    assert {opt.label for opt in view.team_select.options} == {"Foxtrot", "Golf"}


async def test_bare_team_with_no_teams_yet_says_so_without_erroring(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    viewer = make_member(guild, display_name="Anyone")
    interaction = make_interaction(bot, guild=guild, user=viewer, command_name="team")

    await TeamsCog.team.callback(cog, interaction, name=None)

    assert "no teams yet" in reply_text(interaction).lower()


async def _select_team_and_member(view, bot, guild, actor, message, team_name, member):
    team_click = make_interaction(bot, guild=guild, user=actor, message=message)
    await TeamPanelView.team_select(view, team_click, SimpleNamespace(values=[team_name]))
    member_click = make_interaction(bot, guild=guild, user=actor, message=message)
    await TeamPanelView.member_select(view, member_click, SimpleNamespace(values=[member]))


async def test_panel_assign_button_adds_the_team_role_to_the_selected_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "Hotel")
    recruit = make_member(guild, display_name="Recruit")
    admin = await _admin(guild)
    open_interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, open_interaction, name=None)
    view = open_interaction.response.messages[-1]["view"]
    message = open_interaction.response.message
    await _select_team_and_member(view, bot, guild, admin, message, "Hotel", recruit)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await TeamPanelView.assign_button(view, click, None)

    assert role in recruit.roles
    assert reply_ephemeral(click) is True


async def test_panel_remove_button_takes_the_team_role_off_the_selected_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "India")
    recruit = make_member(guild, display_name="Recruit", roles=[role])
    admin = await _admin(guild)
    open_interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, open_interaction, name=None)
    view = open_interaction.response.messages[-1]["view"]
    message = open_interaction.response.message
    await _select_team_and_member(view, bot, guild, admin, message, "India", recruit)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await TeamPanelView.remove_button(view, click, None)

    assert role not in recruit.roles


async def test_panel_assign_and_disband_buttons_are_disabled_until_a_team_is_chosen(
    cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Juliet")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, interaction, name=None)
    view = interaction.response.messages[-1]["view"]

    assert view.assign_button.disabled is True
    assert view.remove_button.disabled is True
    assert view.disband_button.disabled is True


async def test_panel_buttons_recheck_permission_and_decline_a_non_privileged_clicker(
    cog, bot, guild
):
    """A panel click is a fresh interaction — teams.manage must be
    rechecked there, not just trusted from whoever opened /team."""
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "Kilo")
    recruit = make_member(guild, display_name="Recruit")
    admin = await _admin(guild)
    grunt = make_member(guild, display_name="Grunt")
    open_interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, open_interaction, name=None)
    view = open_interaction.response.messages[-1]["view"]
    message = open_interaction.response.message
    await _select_team_and_member(view, bot, guild, admin, message, "Kilo", recruit)

    click = make_interaction(bot, guild=guild, user=grunt, message=message)
    await TeamPanelView.assign_button(view, click, None)

    assert role not in recruit.roles
    assert reply_ephemeral(click) is True
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_panel_assign_button_is_rate_limited_like_the_old_slash_command_was(cog, bot, guild):
    """The old /team assign command carried @rate_limited(); the button
    replacing it needs the same protection since a button click has no
    interaction.command for rate_limited()'s app_commands check to key
    off of — see admin.check_rate_limit."""
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Lima2")
    recruit = make_member(guild, display_name="Recruit")
    admin = await _admin(guild)
    open_interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, open_interaction, name=None)
    view = open_interaction.response.messages[-1]["view"]
    message = open_interaction.response.message
    await _select_team_and_member(view, bot, guild, admin, message, "Lima2", recruit)

    last_click = None
    for _ in range(6):
        last_click = make_interaction(bot, guild=guild, user=admin, message=message)
        await TeamPanelView.assign_button(view, last_click, None)

    assert "often" in reply_text(last_click).lower()
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "rate_limit" and i["detail"] == "team.assign" for i in incidents)


async def test_panel_is_locked_to_whoever_opened_it(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Lima")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, interaction, name=None)
    view = interaction.response.messages[-1]["view"]
    message = interaction.response.message
    bystander = make_member(guild, display_name="Bystander")

    click = make_interaction(bot, guild=guild, user=bystander, message=message)
    allowed = await view.interaction_check(click)

    assert allowed is False
    assert reply_ephemeral(click) is True


# --------------------------------------------------------------------------- #
# /team disband (panel button + /admin team-disband fallback)                 #
# --------------------------------------------------------------------------- #


async def test_panel_disband_button_shows_the_confirm_view_which_completes_the_teardown(
    cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Mike")
    category = next(c for c in guild.categories if c.name == "Mike")
    admin = await _admin(guild)
    open_interaction = make_interaction(bot, guild=guild, user=admin, command_name="team")
    await TeamsCog.team.callback(cog, open_interaction, name=None)
    view = open_interaction.response.messages[-1]["view"]
    message = open_interaction.response.message
    team_click = make_interaction(bot, guild=guild, user=admin, message=message)
    await TeamPanelView.team_select(view, team_click, SimpleNamespace(values=["Mike"]))

    disband_click = make_interaction(bot, guild=guild, user=admin, message=message)
    await TeamPanelView.disband_button(view, disband_click, None)

    confirm_view = disband_click.response.messages[-1]["view"]
    assert isinstance(confirm_view, DisbandConfirmView)
    assert category.deleted is False  # nothing happened yet — waiting on the button

    confirm_click = make_interaction(
        bot, guild=guild, user=admin, message=disband_click.response.message
    )
    await DisbandConfirmView.confirm(confirm_view, confirm_click, None)

    assert category.deleted is True
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Mike")
    )
    assert rows == []


async def test_disband_confirm_cancel_button_leaves_everything_untouched(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "November")
    category = next(c for c in guild.categories if c.name == "November")
    admin = await _admin(guild)

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin team-disband")
    await AdminCog.team_disband.callback(admin_cog, interaction, name="November", confirm=False)
    view = interaction.response.messages[-1]["view"]

    click = make_interaction(bot, guild=guild, user=admin, message=interaction.response.message)
    await DisbandConfirmView.cancel(view, click, None)

    assert category.deleted is False
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "November")
    )
    assert len(rows) == 1


async def test_admin_team_disband_with_confirm_true_removes_role_category_and_db_row(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "Oscar")
    category = next(c for c in guild.categories if c.name == "Oscar")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin team-disband")

    await AdminCog.team_disband.callback(admin_cog, interaction, name="Oscar", confirm=True)

    assert category.deleted is True
    for channel in list(category.channels):
        assert channel.deleted is True
    assert role not in guild.roles or role.id not in {r.id for r in guild.roles}
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Oscar")
    )
    assert rows == []


async def test_admin_team_disband_on_a_nonexistent_team_declines_courteously(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin team-disband")

    await AdminCog.team_disband.callback(admin_cog, interaction, name="Nonexistent", confirm=True)

    assert reply_ephemeral(interaction) is True
    assert "no team" in reply_text(interaction).lower()


async def test_admin_team_disband_is_refused_for_a_non_privileged_member(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Papa")
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin team-disband")

    await AdminCog.team_disband.callback(admin_cog, interaction, name="Papa", confirm=True)

    assert reply_ephemeral(interaction) is True
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Papa")
    )
    assert len(rows) == 1  # untouched
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_admin_team_disband_reports_forbidden_cleanly_when_deletion_is_blocked(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Quebec")
    category = next(c for c in guild.categories if c.name == "Quebec")
    admin = await _admin(guild)

    async def _raise_forbidden(reason=None):
        raise forbidden("Missing Permissions")

    category.delete = _raise_forbidden

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin team-disband")
    await AdminCog.team_disband.callback(admin_cog, interaction, name="Quebec", confirm=True)

    assert reply_ephemeral(interaction) is True
    assert (
        "couldn't remove" in reply_text(interaction).lower()
        or "small snag" in reply_text(interaction).lower()
    )
    # DB row must survive — teardown only commits after full deletion succeeds
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Quebec")
    )
    assert len(rows) == 1
