"""Behaviour spec for services/persistence.py: bot_state, posted_messages,
panels — the crash-safe idempotent-posting substrate.

Uses a real aiosqlite connection (migrated fresh via the `tmp_conn`
fixture from conftest.py) since these helpers operate directly against
the DB rather than being mocked.
"""

from __future__ import annotations

from adjutant.services import persistence


async def _with_guild(conn, guild_id: int = 1) -> None:
    await conn.execute("INSERT INTO guilds (guild_id) VALUES (?)", (guild_id,))
    await conn.commit()


# --------------------------------------------------------------------------- #
# bot_state                                                                    #
# --------------------------------------------------------------------------- #


async def test_get_bot_state_returns_none_when_key_is_unset(tmp_conn):
    await _with_guild(tmp_conn)
    assert await persistence.get_bot_state(tmp_conn, 1, "last_sweep") is None


async def test_set_then_get_bot_state_round_trips_the_value(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.set_bot_state(tmp_conn, 1, "last_sweep", "2026-08-10T00:00:00Z")
    value = await persistence.get_bot_state(tmp_conn, 1, "last_sweep")
    assert value == "2026-08-10T00:00:00Z"


async def test_setting_bot_state_twice_overwrites_the_previous_value(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.set_bot_state(tmp_conn, 1, "k", "first")
    await persistence.set_bot_state(tmp_conn, 1, "k", "second")
    assert await persistence.get_bot_state(tmp_conn, 1, "k") == "second"


async def test_bot_state_is_scoped_per_guild(tmp_conn):
    await _with_guild(tmp_conn, 1)
    await _with_guild(tmp_conn, 2)
    await persistence.set_bot_state(tmp_conn, 1, "k", "guild-1-value")
    assert await persistence.get_bot_state(tmp_conn, 2, "k") is None


# --------------------------------------------------------------------------- #
# posted_messages                                                              #
# --------------------------------------------------------------------------- #


async def test_find_posted_message_returns_none_when_nothing_recorded(tmp_conn):
    await _with_guild(tmp_conn)
    found = await persistence.find_posted_message(tmp_conn, "weekly_recap", "2026-W17", 1)
    assert found is None


async def test_record_then_find_posted_message_round_trips(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.record_posted_message(
        tmp_conn,
        kind="weekly_recap",
        identity="2026-W17",
        guild_id=1,
        channel_id=555,
        message_id=777,
    )
    found = await persistence.find_posted_message(tmp_conn, "weekly_recap", "2026-W17", 1)
    assert found is not None
    assert found["channel_id"] == 555
    assert found["message_id"] == 777


async def test_recording_the_same_identity_twice_keeps_the_first_record(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.record_posted_message(
        tmp_conn,
        kind="weekly_recap",
        identity="2026-W17",
        guild_id=1,
        channel_id=1,
        message_id=100,
    )
    await persistence.record_posted_message(
        tmp_conn,
        kind="weekly_recap",
        identity="2026-W17",
        guild_id=1,
        channel_id=2,
        message_id=200,
    )
    found = await persistence.find_posted_message(tmp_conn, "weekly_recap", "2026-W17", 1)
    assert found["message_id"] == 100


async def test_posted_messages_are_scoped_by_kind(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.record_posted_message(
        tmp_conn,
        kind="weekly_recap",
        identity="X",
        guild_id=1,
        channel_id=1,
        message_id=100,
    )
    found = await persistence.find_posted_message(tmp_conn, "event_reminder", "X", 1)
    assert found is None


async def test_posted_messages_are_scoped_by_guild(tmp_conn):
    await _with_guild(tmp_conn, 1)
    await _with_guild(tmp_conn, 2)
    await persistence.record_posted_message(
        tmp_conn,
        kind="weekly_recap",
        identity="X",
        guild_id=1,
        channel_id=1,
        message_id=100,
    )
    found = await persistence.find_posted_message(tmp_conn, "weekly_recap", "X", 2)
    assert found is None


# --------------------------------------------------------------------------- #
# panels                                                                       #
# --------------------------------------------------------------------------- #


async def test_get_panel_returns_none_when_unset(tmp_conn):
    await _with_guild(tmp_conn)
    assert await persistence.get_panel(tmp_conn, 1, "map") is None


async def test_set_then_get_panel_round_trips(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.set_panel(tmp_conn, 1, "map", channel_id=10, message_id=20)
    panel = await persistence.get_panel(tmp_conn, 1, "map")
    assert panel is not None
    assert panel["channel_id"] == 10
    assert panel["message_id"] == 20


async def test_setting_a_panel_again_updates_the_existing_row(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.set_panel(tmp_conn, 1, "map", channel_id=10, message_id=20)
    await persistence.set_panel(tmp_conn, 1, "map", channel_id=10, message_id=99)
    panel = await persistence.get_panel(tmp_conn, 1, "map")
    assert panel["message_id"] == 99


async def test_clear_panel_removes_the_row(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.set_panel(tmp_conn, 1, "map", channel_id=10, message_id=20)
    await persistence.clear_panel(tmp_conn, 1, "map")
    assert await persistence.get_panel(tmp_conn, 1, "map") is None


async def test_clear_panel_is_a_noop_when_nothing_was_set(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.clear_panel(tmp_conn, 1, "map")  # must not raise


async def test_panels_are_scoped_per_kind(tmp_conn):
    await _with_guild(tmp_conn)
    await persistence.set_panel(tmp_conn, 1, "map", channel_id=10, message_id=20)
    assert await persistence.get_panel(tmp_conn, 1, "roster") is None
