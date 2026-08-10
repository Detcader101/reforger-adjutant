"""In-process integration tests for adjutant/cogs/map.py: post/refresh-in-
place, marker placement (with the documented DB `y` <-> service `z` axis
mapping), and marker clearing.
"""
from __future__ import annotations

import pytest
from discord import app_commands

from adjutant.cogs.map import MapCog
from adjutant.services import mapping as mapping_service
from fakes import (
    FakeGuild,
    make_interaction,
    make_member,
    reply_ephemeral,
    reply_text,
    run_checks,
    seed_guild,
)

_OBJECTIVE = app_commands.Choice(name="Objective", value="objective")


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
def cog(bot):
    return MapCog(bot)


@pytest.fixture
def channel(guild):
    return guild.create_standalone_text_channel("maps")


async def _show(cog, bot, guild, channel, user, terrain=None):
    interaction = make_interaction(bot, guild=guild, user=user, channel=channel, command_name="map show")
    await MapCog.show.callback(cog, interaction, terrain=terrain)
    row = (
        await bot.db.execute_fetchall(
            "SELECT * FROM maps WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
    )[0]
    return row, interaction


# --------------------------------------------------------------------------- #
# /map show                                                                    #
# --------------------------------------------------------------------------- #

async def test_show_posts_a_new_message_the_first_time(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")

    row, interaction = await _show(cog, bot, guild, channel, member)

    assert len(channel.sent) == 1
    posted = channel.sent[0]
    assert row["message_id"] == posted.id
    assert posted.attachments, "a rendered PNG should have been attached"


async def test_show_edits_the_existing_message_in_place_the_second_time(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    row, _ = await _show(cog, bot, guild, channel, member)
    first_message = channel.sent[-1]
    channel.sent.clear()

    row2, interaction2 = await _show(cog, bot, guild, channel, member)

    assert channel.sent == [], "a second /map show must not post a new message"
    assert first_message.edits, "it should edit the existing message instead"
    assert row2["id"] == row["id"]
    assert reply_ephemeral(interaction2) is True
    assert "refreshed" in reply_text(interaction2).lower()
    all_maps = await bot.db.execute_fetchall(
        "SELECT * FROM maps WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
    )
    assert len(all_maps) == 1


# --------------------------------------------------------------------------- #
# /map mark                                                                    #
# --------------------------------------------------------------------------- #

async def test_mark_with_valid_grid_stores_marker_at_the_right_world_coords(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    map_row, _ = await _show(cog, bot, guild, channel, admin)

    mark_interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map mark")
    await MapCog.mark.callback(cog, mark_interaction, kind=_OBJECTIVE, label="Hilltop", grid="023 087")

    expected_x, expected_z = mapping_service.parse_grid("023 087")
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers WHERE map_id = ?", (map_row["id"],))
    assert len(rows) == 1
    row = rows[0]
    assert row["x"] == expected_x
    # The load-bearing assertion: services/mapping.py's Marker.z is stored in
    # the DB's `y` column (see cogs/map.py's _load_markers docstring). A
    # "helpful" rename of either side of that mapping would silently flip
    # markers onto the wrong axis without this check catching it.
    assert row["y"] == expected_z
    assert row["kind"] == "objective"
    assert row["label"] == "Hilltop"
    assert reply_ephemeral(mark_interaction) is True
    assert "Hilltop" in reply_text(mark_interaction)


async def test_mark_with_malformed_grid_declines_instead_of_raising(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    mark_interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map mark")
    await MapCog.mark.callback(cog, mark_interaction, kind=_OBJECTIVE, label="X", grid="not-a-grid")

    assert reply_ephemeral(mark_interaction) is True
    assert "isn't a grid reference" in reply_text(mark_interaction)
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers")
    assert rows == []


async def test_mark_without_a_map_yet_declines_and_suggests_show(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    mark_interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map mark")

    await MapCog.mark.callback(cog, mark_interaction, kind=_OBJECTIVE, label="X", grid="023 087")

    assert "map show" in reply_text(mark_interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers")
    assert rows == []


async def test_mark_is_refused_for_a_non_privileged_member(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, channel=channel, command_name="map mark")

    allowed = await run_checks(MapCog.mark, interaction)

    assert allowed is False
    assert reply_ephemeral(interaction) is True
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)


# --------------------------------------------------------------------------- #
# /map clear                                                                   #
# --------------------------------------------------------------------------- #

async def _place_marker(cog, bot, guild, channel, admin, label, grid="023 087"):
    interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map mark")
    await MapCog.mark.callback(cog, interaction, kind=_OBJECTIVE, label=label, grid=grid)


async def test_clear_with_an_id_removes_only_that_marker(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    map_row, _ = await _show(cog, bot, guild, channel, admin)
    await _place_marker(cog, bot, guild, channel, admin, "First", grid="010 010")
    await _place_marker(cog, bot, guild, channel, admin, "Second", grid="020 020")
    markers = await bot.db.execute_fetchall(
        "SELECT id, label FROM map_markers WHERE map_id = ? ORDER BY id", (map_row["id"],)
    )
    first_id = markers[0]["id"]

    clear_interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map clear")
    await MapCog.clear.callback(cog, clear_interaction, marker_id=first_id)

    remaining = await bot.db.execute_fetchall("SELECT label FROM map_markers WHERE map_id = ?", (map_row["id"],))
    assert [r["label"] for r in remaining] == ["Second"]
    assert f"marker #{first_id}" in reply_text(clear_interaction).lower()


async def test_clear_without_an_id_removes_every_marker(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    map_row, _ = await _show(cog, bot, guild, channel, admin)
    await _place_marker(cog, bot, guild, channel, admin, "First", grid="010 010")
    await _place_marker(cog, bot, guild, channel, admin, "Second", grid="020 020")

    clear_interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map clear")
    await MapCog.clear.callback(cog, clear_interaction, marker_id=None)

    remaining = await bot.db.execute_fetchall("SELECT * FROM map_markers WHERE map_id = ?", (map_row["id"],))
    assert remaining == []
    assert "every marker" in reply_text(clear_interaction).lower()


async def test_clear_with_an_unknown_id_declines(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    clear_interaction = make_interaction(bot, guild=guild, user=admin, channel=channel, command_name="map clear")
    await MapCog.clear.callback(cog, clear_interaction, marker_id=999999)

    assert "no marker #999999" in reply_text(clear_interaction).lower()
