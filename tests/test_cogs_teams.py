"""In-process integration tests for adjutant/cogs/teams.py.

/team is the leak-prevention surface SPEC.md calls out explicitly: "Team
channels are locked at the Discord permission layer, not just by
convention." The create() test below is the most rigorous test in this
whole suite for exactly that reason — it doesn't just check the command
replied "success", it inspects the actual discord.PermissionOverwrite
objects handed to guild.create_category and asserts @everyone is denied
view_channel and the team role is granted it.
"""
from __future__ import annotations

import discord
import pytest

from adjutant.cogs.teams import DisbandConfirmView, TeamsCog
from fakes import (
    FakeGuild,
    all_replies,
    build_roles_cog,
    forbidden,
    make_interaction,
    make_member,
    next_id,
    reply_ephemeral,
    reply_text,
    run_checks,
    seed_guild,
)


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
def cog(bot):
    return TeamsCog(bot)


async def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


# --------------------------------------------------------------------------- #
# /team create — leak prevention (the critical test)                          #
# --------------------------------------------------------------------------- #

async def test_create_makes_role_category_text_and_voice_channel(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team create")

    await TeamsCog.create.callback(cog, interaction, name="Alpha")

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


async def test_create_permission_overwrites_deny_everyone_and_allow_team_role(cog, bot, guild):
    """The leak-prevention guarantee from SPEC.md, checked at the data
    level: @everyone must be explicitly denied view_channel on the team's
    category, and the freshly-created team role must be explicitly granted
    it (plus connect/speak/send_messages) — not merely "not mentioned",
    which in Discord's permission model defaults to inherited/allow and
    would be a silent leak."""
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team create")

    await TeamsCog.create.callback(cog, interaction, name="Bravo")

    category = next(c for c in guild.categories if c.name == "Bravo")
    role = next(r for r in guild.roles if r.name == "Team Bravo")

    everyone_overwrite = category.overwrites.get(guild.default_role)
    assert everyone_overwrite is not None, "no overwrite at all for @everyone means Discord's default (visible) applies"
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
    assert bot_overwrite.manage_roles is True

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
    first = make_interaction(bot, guild=guild, user=admin, command_name="team create")
    await TeamsCog.create.callback(cog, first, name="Charlie")

    second = make_interaction(bot, guild=guild, user=admin, command_name="team create")
    await TeamsCog.create.callback(cog, second, name="Charlie")

    assert "already exists" in reply_text(second)
    assert reply_ephemeral(second) is True
    # only one category was created
    assert len([c for c in guild.categories if c.name == "Charlie"]) == 1


async def test_create_reports_forbidden_cleanly_when_bot_lacks_permission(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    guild.fail_next_create_role(forbidden("Missing Permissions"))
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team create")

    await TeamsCog.create.callback(cog, interaction, name="Delta")

    assert reply_ephemeral(interaction) is True
    assert "lack permission" in reply_text(interaction) or "small snag" in reply_text(interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Delta"))
    assert rows == []


async def test_create_is_refused_for_a_non_privileged_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")  # no rank, not admin
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="team create")

    allowed = await run_checks(TeamsCog.create, interaction)

    assert allowed is False
    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower() or "rank" in reply_text(interaction).lower()
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)


# --------------------------------------------------------------------------- #
# /team assign, /team remove                                                  #
# --------------------------------------------------------------------------- #

async def _create_team(cog, bot, guild, name="Echo"):
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team create")
    await TeamsCog.create.callback(cog, interaction, name=name)
    role = next(r for r in guild.roles if r.name == f"Team {name}")
    return role


async def test_assign_adds_the_team_role_to_the_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "Echo")
    recruit = make_member(guild, display_name="Recruit")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team assign")

    await TeamsCog.assign.callback(cog, interaction, member=recruit, team="Echo")

    assert role in recruit.roles
    assert reply_ephemeral(interaction) is True


async def test_remove_takes_the_team_role_off_the_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "Foxtrot")
    recruit = make_member(guild, display_name="Recruit", roles=[role])
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team remove")

    await TeamsCog.remove.callback(cog, interaction, member=recruit, team="Foxtrot")

    assert role not in recruit.roles
    assert reply_ephemeral(interaction) is True


async def test_assign_declines_for_an_unknown_team(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    recruit = make_member(guild, display_name="Recruit")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team assign")

    await TeamsCog.assign.callback(cog, interaction, member=recruit, team="Ghost Team")

    assert "no team" in reply_text(interaction).lower()
    assert reply_ephemeral(interaction) is True


# --------------------------------------------------------------------------- #
# /team disband                                                               #
# --------------------------------------------------------------------------- #

async def test_disband_with_confirm_true_removes_role_category_and_db_row(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    role = await _create_team(cog, bot, guild, "Golf")
    category = next(c for c in guild.categories if c.name == "Golf")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team disband")

    await TeamsCog.disband.callback(cog, interaction, name="Golf", confirm=True)

    assert category.deleted is True
    for channel in list(category.channels):
        assert channel.deleted is True
    assert role not in guild.roles or role.id not in {r.id for r in guild.roles}
    rows = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Golf"))
    assert rows == []


async def test_disband_without_confirm_shows_a_button_view_that_performs_the_same_teardown(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Hotel")
    category = next(c for c in guild.categories if c.name == "Hotel")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team disband")

    await TeamsCog.disband.callback(cog, interaction, name="Hotel", confirm=False)

    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, DisbandConfirmView)
    assert category.deleted is False  # nothing happened yet — waiting on the button

    # Simulate the admin clicking "Disband" — a second interaction on the
    # confirmation message, per the harness's button-callback pattern.
    click = make_interaction(bot, guild=guild, user=admin, message=interaction.response.message)
    await DisbandConfirmView.confirm(view, click, None)

    assert category.deleted is True
    rows = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Hotel"))
    assert rows == []


async def test_disband_cancel_button_leaves_everything_untouched(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "India")
    category = next(c for c in guild.categories if c.name == "India")
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team disband")
    await TeamsCog.disband.callback(cog, interaction, name="India", confirm=False)
    view = interaction.response.messages[-1]["view"]

    click = make_interaction(bot, guild=guild, user=admin, message=interaction.response.message)
    await DisbandConfirmView.cancel(view, click, None)

    assert category.deleted is False
    rows = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "India"))
    assert len(rows) == 1


async def test_disband_on_a_nonexistent_team_declines_courteously(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = await _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team disband")

    await TeamsCog.disband.callback(cog, interaction, name="Nonexistent", confirm=True)

    assert reply_ephemeral(interaction) is True
    assert "no team" in reply_text(interaction).lower()


async def test_disband_reports_forbidden_cleanly_when_deletion_is_blocked(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _create_team(cog, bot, guild, "Juliet")
    category = next(c for c in guild.categories if c.name == "Juliet")
    admin = await _admin(guild)

    async def _raise_forbidden(reason=None):
        raise forbidden("Missing Permissions")
    category.delete = _raise_forbidden

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="team disband")
    await TeamsCog.disband.callback(cog, interaction, name="Juliet", confirm=True)

    assert reply_ephemeral(interaction) is True
    assert "couldn't remove" in reply_text(interaction).lower() or "small snag" in reply_text(interaction).lower()
    # DB row must survive — teardown only commits after full deletion succeeds
    rows = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ? AND name = ?", (guild.id, "Juliet"))
    assert len(rows) == 1
