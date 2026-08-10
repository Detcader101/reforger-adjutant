"""In-process integration tests for adjutant/cogs/map.py: post/refresh-in-
place, marker placement (with the documented DB `y` <-> service `z` axis
mapping), and marker clearing.

Command surface: `/map [terrain]` shows/refreshes the map and attaches a
MapPanelView with Mark/Clear buttons, each opening a modal so nobody has
to remember a subcommand's argument order.
"""
from __future__ import annotations

import pytest

from adjutant.cogs.map import MapCog, MapClearModal, MapMarkModal, MapPanelView
from adjutant.services import mapping as mapping_service
from fakes import (
    FakeGuild,
    make_interaction,
    make_member,
    reply_ephemeral,
    reply_text,
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
    return MapCog(bot)


@pytest.fixture
def channel(guild):
    return guild.create_standalone_text_channel("maps")


async def _show(cog, bot, guild, channel, user, terrain=None):
    interaction = make_interaction(bot, guild=guild, user=user, channel=channel, command_name="map")
    await MapCog.map_command.callback(cog, interaction, terrain=terrain)
    row = (
        await bot.db.execute_fetchall(
            "SELECT * FROM maps WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
        )
    )[0]
    return row, interaction


def _submit_mark(cog, channel_id, kind, label, grid):
    modal = MapMarkModal(cog, channel_id)
    modal.kind_input._value = kind
    modal.label_input._value = label
    modal.grid_input._value = grid
    return modal


def _submit_clear(cog, channel_id, marker_id=None):
    modal = MapClearModal(cog, channel_id)
    modal.marker_id_input._value = "" if marker_id is None else str(marker_id)
    return modal


# --------------------------------------------------------------------------- #
# /map                                                                         #
# --------------------------------------------------------------------------- #

async def test_show_posts_a_new_message_the_first_time(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")

    row, interaction = await _show(cog, bot, guild, channel, member)

    assert len(channel.sent) == 1
    posted = channel.sent[0]
    assert row["message_id"] == posted.id
    assert posted.attachments, "a rendered PNG should have been attached"
    view = interaction.followup.messages[-1]["view"]
    assert isinstance(view, MapPanelView)


async def test_show_edits_the_existing_message_in_place_the_second_time(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    row, _ = await _show(cog, bot, guild, channel, member)
    first_message = channel.sent[-1]
    channel.sent.clear()

    row2, interaction2 = await _show(cog, bot, guild, channel, member)

    assert channel.sent == [], "a second /map must not post a new message"
    assert first_message.edits, "it should edit the existing message instead"
    assert row2["id"] == row["id"]
    assert reply_ephemeral(interaction2) is True
    assert "refreshed" in reply_text(interaction2).lower()
    all_maps = await bot.db.execute_fetchall(
        "SELECT * FROM maps WHERE guild_id = ? AND channel_id = ?", (guild.id, channel.id)
    )
    assert len(all_maps) == 1


# --------------------------------------------------------------------------- #
# Mark modal                                                                   #
# --------------------------------------------------------------------------- #

async def test_mark_with_valid_grid_stores_marker_at_the_right_world_coords(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    map_row, _ = await _show(cog, bot, guild, channel, admin)

    modal = _submit_mark(cog, channel.id, "objective", "Hilltop", "023 087")
    submit_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(submit_interaction)

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
    assert reply_ephemeral(submit_interaction) is True
    assert "Hilltop" in reply_text(submit_interaction)


async def test_mark_with_an_unknown_kind_declines_instead_of_raising(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    modal = _submit_mark(cog, channel.id, "spaceship", "X", "023 087")
    submit_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(submit_interaction)

    assert reply_ephemeral(submit_interaction) is True
    assert "isn't a marker kind" in reply_text(submit_interaction)
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers")
    assert rows == []


async def test_mark_with_malformed_grid_declines_instead_of_raising(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    modal = _submit_mark(cog, channel.id, "objective", "X", "not-a-grid")
    submit_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(submit_interaction)

    assert reply_ephemeral(submit_interaction) is True
    assert "isn't a grid reference" in reply_text(submit_interaction)
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers")
    assert rows == []


async def test_mark_without_a_map_yet_declines_and_suggests_map(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)

    modal = _submit_mark(cog, channel.id, "objective", "X", "023 087")
    submit_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(submit_interaction)

    assert "/map" in reply_text(submit_interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers")
    assert rows == []


async def test_mark_modal_is_rate_limited_like_the_old_slash_command_was(cog, bot, guild, channel):
    """The old /map mark command carried @rate_limited(); the modal
    replacing it needs the same protection — see admin.check_rate_limit."""
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    last_interaction = None
    for _ in range(6):
        modal = _submit_mark(cog, channel.id, "objective", "X", "023 087")
        last_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
        await modal.on_submit(last_interaction)

    assert "often" in reply_text(last_interaction).lower()
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "rate_limit" and i["detail"] == "map.mark" for i in incidents)


async def test_mark_modal_submission_is_refused_for_a_non_privileged_member(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)
    grunt = make_member(guild, display_name="Grunt")

    modal = _submit_mark(cog, channel.id, "objective", "X", "023 087")
    submit_interaction = make_interaction(bot, guild=guild, user=grunt, command_name="map")
    await modal.on_submit(submit_interaction)

    assert reply_ephemeral(submit_interaction) is True
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)
    rows = await bot.db.execute_fetchall("SELECT * FROM map_markers")
    assert rows == []


async def test_mark_button_declines_a_non_privileged_clicker_before_opening_the_modal(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    _, show_interaction = await _show(cog, bot, guild, channel, admin)
    view = show_interaction.followup.messages[-1]["view"]
    message = show_interaction.followup.messages[-1]["message"]
    grunt = make_member(guild, display_name="Grunt")

    click = make_interaction(bot, guild=guild, user=grunt, message=message)
    await MapPanelView.mark_button(view, click, None)

    assert click.response.modal is None
    assert reply_ephemeral(click) is True


# --------------------------------------------------------------------------- #
# Clear modal                                                                  #
# --------------------------------------------------------------------------- #

async def _place_marker(cog, bot, guild, channel, admin, label, grid="023 087"):
    modal = _submit_mark(cog, channel.id, "objective", label, grid)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(interaction)


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

    modal = _submit_clear(cog, channel.id, marker_id=first_id)
    clear_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(clear_interaction)

    remaining = await bot.db.execute_fetchall("SELECT label FROM map_markers WHERE map_id = ?", (map_row["id"],))
    assert [r["label"] for r in remaining] == ["Second"]
    assert f"marker #{first_id}" in reply_text(clear_interaction).lower()


async def test_clear_without_an_id_removes_every_marker(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    map_row, _ = await _show(cog, bot, guild, channel, admin)
    await _place_marker(cog, bot, guild, channel, admin, "First", grid="010 010")
    await _place_marker(cog, bot, guild, channel, admin, "Second", grid="020 020")

    modal = _submit_clear(cog, channel.id, marker_id=None)
    clear_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(clear_interaction)

    remaining = await bot.db.execute_fetchall("SELECT * FROM map_markers WHERE map_id = ?", (map_row["id"],))
    assert remaining == []
    assert "every marker" in reply_text(clear_interaction).lower()


async def test_clear_with_an_unknown_id_declines(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    modal = _submit_clear(cog, channel.id, marker_id=999999)
    clear_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(clear_interaction)

    assert "no marker #999999" in reply_text(clear_interaction).lower()


async def test_clear_with_a_non_numeric_id_declines_instead_of_raising(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await _show(cog, bot, guild, channel, admin)

    modal = MapClearModal(cog, channel.id)
    modal.marker_id_input._value = "not-a-number"
    clear_interaction = make_interaction(bot, guild=guild, user=admin, command_name="map")
    await modal.on_submit(clear_interaction)

    assert "isn't a whole number" in reply_text(clear_interaction)
