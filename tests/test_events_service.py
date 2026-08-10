"""Events service: creation, status transitions, signups, reminder/teardown
due-queries, and start-time parsing.

Uses a real aiosqlite connection (migrated fresh per test) since events.py
operates directly on the events/event_signups tables — no mocking the DB
layer, matching the pattern in test_grants.py.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from adjutant import db as database
from adjutant.services import events


async def _conn(tmp_path: Path):
    conn = await database.connect(tmp_path / "t.db")
    await conn.execute("INSERT INTO guilds (guild_id) VALUES (1)")
    await conn.commit()
    return conn


async def _make_event(conn, **overrides):
    kwargs = dict(
        guild_id=1,
        name="Op Flashpoint",
        description="A raid on the coast.",
        starts_at="2026-01-01 12:00:00",
        created_by=10,
    )
    kwargs.update(overrides)
    return await events.create_event(conn, **kwargs)


# -- parse_start ---------------------------------------------------------

def test_parse_start_accepts_relative_duration():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert events.parse_start("2h", now) == now + timedelta(hours=2)


def test_parse_start_accepts_absolute_utc_datetime():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert events.parse_start("2026-01-02 18:30", now) == datetime(2026, 1, 2, 18, 30)


def test_parse_start_strips_surrounding_whitespace():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert events.parse_start("  3d  ", now) == now + timedelta(days=3)


def test_parse_start_rejects_unrecognised_text():
    now = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        events.parse_start("whenever", now)


def test_parse_start_rejects_absolute_time_in_the_past():
    now = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        events.parse_start("2025-01-01 12:00", now)


def test_parse_start_rejects_absolute_time_equal_to_now():
    now = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        events.parse_start("2026-01-01 12:00", now)


def test_parse_start_rejects_zero_duration():
    now = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        events.parse_start("0m", now)


# -- stored_to_datetime ----------------------------------------------------

def test_stored_to_datetime_is_the_inverse_of_format_timestamp():
    dt = datetime(2026, 3, 4, 9, 30, 0)
    assert events.stored_to_datetime(events.format_timestamp(dt)) == dt


# -- create_event / get_event --------------------------------------------

async def test_create_event_persists_and_returns_id(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        assert isinstance(event_id, int)
        event = await events.get_event(conn, event_id)
        assert event is not None
        assert event.name == "Op Flashpoint"
        assert event.description == "A raid on the coast."
        assert event.starts_at == "2026-01-01 12:00:00"
        assert event.created_by == 10
        assert event.status == "open"
        assert event.reminded == 0
        assert event.announce_channel is None
        assert event.announce_message is None
    finally:
        await conn.close()


async def test_create_event_defaults_description_to_empty_string(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await events.create_event(
            conn, guild_id=1, name="Op Bare", starts_at="2026-01-01 12:00:00", created_by=10
        )
        event = await events.get_event(conn, event_id)
        assert event.description == ""
    finally:
        await conn.close()


async def test_get_event_returns_none_for_missing_id(tmp_path):
    conn = await _conn(tmp_path)
    try:
        assert await events.get_event(conn, 999) is None
    finally:
        await conn.close()


# -- set_announce_message / get_event_by_announce_message -----------------

async def test_set_announce_message_persists_channel_and_message_id(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.set_announce_message(conn, event_id, channel_id=500, message_id=600)
        event = await events.get_event(conn, event_id)
        assert event.announce_channel == 500
        assert event.announce_message == 600
    finally:
        await conn.close()


async def test_get_event_by_announce_message_finds_matching_event(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.set_announce_message(conn, event_id, channel_id=500, message_id=600)
        event = await events.get_event_by_announce_message(conn, 600)
        assert event is not None
        assert event.id == event_id
    finally:
        await conn.close()


async def test_get_event_by_announce_message_returns_none_when_unmatched(tmp_path):
    conn = await _conn(tmp_path)
    try:
        assert await events.get_event_by_announce_message(conn, 999) is None
    finally:
        await conn.close()


# -- list_upcoming ----------------------------------------------------------

async def test_list_upcoming_returns_future_open_and_closed_events_in_order(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 0, 0, 0)
        later_id = await _make_event(conn, name="Later", starts_at="2026-01-03 00:00:00")
        sooner_id = await _make_event(conn, name="Sooner", starts_at="2026-01-02 00:00:00")
        await events.set_status(conn, sooner_id, "closed")
        upcoming = await events.list_upcoming(conn, 1, now)
        assert [e.id for e in upcoming] == [sooner_id, later_id]
    finally:
        await conn.close()


async def test_list_upcoming_excludes_past_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 5, 0, 0, 0)
        await _make_event(conn, name="Past", starts_at="2026-01-01 00:00:00")
        upcoming = await events.list_upcoming(conn, 1, now)
        assert upcoming == []
    finally:
        await conn.close()


async def test_list_upcoming_excludes_done_and_cancelled_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 0, 0, 0)
        done_id = await _make_event(conn, name="Done", starts_at="2026-01-02 00:00:00")
        cancelled_id = await _make_event(conn, name="Cancelled", starts_at="2026-01-02 00:00:00")
        await events.set_status(conn, done_id, "done")
        await events.set_status(conn, cancelled_id, "cancelled")
        upcoming = await events.list_upcoming(conn, 1, now)
        assert upcoming == []
    finally:
        await conn.close()


async def test_list_upcoming_scopes_to_guild(tmp_path):
    conn = await _conn(tmp_path)
    try:
        await conn.execute("INSERT INTO guilds (guild_id) VALUES (2)")
        await conn.commit()
        now = datetime(2026, 1, 1, 0, 0, 0)
        await _make_event(conn, guild_id=2, starts_at="2026-01-02 00:00:00")
        upcoming = await events.list_upcoming(conn, 1, now)
        assert upcoming == []
    finally:
        await conn.close()


# -- set_status transitions --------------------------------------------------

async def test_set_status_open_to_closed_succeeds(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        updated = await events.set_status(conn, event_id, "closed")
        assert updated.status == "closed"
    finally:
        await conn.close()


async def test_set_status_closed_to_done_succeeds(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.set_status(conn, event_id, "closed")
        updated = await events.set_status(conn, event_id, "done")
        assert updated.status == "done"
    finally:
        await conn.close()


async def test_set_status_open_to_done_succeeds_directly(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        updated = await events.set_status(conn, event_id, "done")
        assert updated.status == "done"
    finally:
        await conn.close()


async def test_set_status_open_to_cancelled_succeeds(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        updated = await events.set_status(conn, event_id, "cancelled")
        assert updated.status == "cancelled"
    finally:
        await conn.close()


async def test_set_status_closed_to_cancelled_succeeds(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.set_status(conn, event_id, "closed")
        updated = await events.set_status(conn, event_id, "cancelled")
        assert updated.status == "cancelled"
    finally:
        await conn.close()


async def test_set_status_from_done_raises(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.set_status(conn, event_id, "done")
        with pytest.raises(ValueError):
            await events.set_status(conn, event_id, "cancelled")
    finally:
        await conn.close()


async def test_set_status_from_cancelled_raises(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.set_status(conn, event_id, "cancelled")
        with pytest.raises(ValueError):
            await events.set_status(conn, event_id, "closed")
    finally:
        await conn.close()


async def test_set_status_to_same_status_raises(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        with pytest.raises(ValueError):
            await events.set_status(conn, event_id, "open")
    finally:
        await conn.close()


async def test_set_status_on_missing_event_raises(tmp_path):
    conn = await _conn(tmp_path)
    try:
        with pytest.raises(ValueError):
            await events.set_status(conn, 999, "closed")
    finally:
        await conn.close()


# -- add_signup / remove_signup / signups_for_event --------------------------

async def test_add_signup_new_persists_and_returns_true(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        added = await events.add_signup(conn, event_id, 42)
        assert added is True
        assert await events.signups_for_event(conn, event_id) == [42]
    finally:
        await conn.close()


async def test_add_signup_is_idempotent_returns_false_second_time(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.add_signup(conn, event_id, 42)
        added_again = await events.add_signup(conn, event_id, 42)
        assert added_again is False
        assert await events.signups_for_event(conn, event_id) == [42]
    finally:
        await conn.close()


async def test_remove_signup_existing_returns_true_and_deletes(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.add_signup(conn, event_id, 42)
        removed = await events.remove_signup(conn, event_id, 42)
        assert removed is True
        assert await events.signups_for_event(conn, event_id) == []
    finally:
        await conn.close()


async def test_remove_signup_missing_returns_false(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        removed = await events.remove_signup(conn, event_id, 42)
        assert removed is False
    finally:
        await conn.close()


async def test_remove_signup_is_idempotent_when_called_twice(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_id = await _make_event(conn)
        await events.add_signup(conn, event_id, 42)
        await events.remove_signup(conn, event_id, 42)
        removed_again = await events.remove_signup(conn, event_id, 42)
        assert removed_again is False
    finally:
        await conn.close()


async def test_signups_for_event_scopes_to_that_event_only(tmp_path):
    conn = await _conn(tmp_path)
    try:
        event_a = await _make_event(conn, name="A")
        event_b = await _make_event(conn, name="B")
        await events.add_signup(conn, event_a, 1)
        await events.add_signup(conn, event_b, 2)
        assert await events.signups_for_event(conn, event_a) == [1]
    finally:
        await conn.close()


# -- events_due_reminder / mark_reminded -------------------------------------

async def test_events_due_reminder_includes_event_starting_within_lead(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:20:00")
        due = await events.events_due_reminder(conn, now, timedelta(minutes=30))
        assert [e.id for e in due] == [event_id]
    finally:
        await conn.close()


async def test_events_due_reminder_excludes_event_beyond_lead_window(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        await _make_event(conn, starts_at="2026-01-01 13:00:00")
        due = await events.events_due_reminder(conn, now, timedelta(minutes=30))
        assert due == []
    finally:
        await conn.close()


async def test_events_due_reminder_excludes_already_reminded_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:20:00")
        await events.mark_reminded(conn, event_id)
        due = await events.events_due_reminder(conn, now, timedelta(minutes=30))
        assert due == []
    finally:
        await conn.close()


async def test_events_due_reminder_excludes_events_already_started(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        await _make_event(conn, starts_at="2026-01-01 11:59:00")
        due = await events.events_due_reminder(conn, now, timedelta(minutes=30))
        assert due == []
    finally:
        await conn.close()


async def test_events_due_reminder_includes_closed_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:20:00")
        await events.set_status(conn, event_id, "closed")
        due = await events.events_due_reminder(conn, now, timedelta(minutes=30))
        assert [e.id for e in due] == [event_id]
    finally:
        await conn.close()


async def test_events_due_reminder_excludes_cancelled_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:20:00")
        await events.set_status(conn, event_id, "cancelled")
        due = await events.events_due_reminder(conn, now, timedelta(minutes=30))
        assert due == []
    finally:
        await conn.close()


async def test_mark_reminded_sets_flag_so_event_no_longer_due(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:20:00")
        await events.mark_reminded(conn, event_id)
        event = await events.get_event(conn, event_id)
        assert event.reminded == 1
    finally:
        await conn.close()


# -- events_due_teardown -------------------------------------------------------

async def test_events_due_teardown_includes_open_event_past_start(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 2, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:00:00")
        due = await events.events_due_teardown(conn, now)
        assert [e.id for e in due] == [event_id]
    finally:
        await conn.close()


async def test_events_due_teardown_includes_closed_event_past_start(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 2, 12, 0, 0)
        event_id = await _make_event(conn, starts_at="2026-01-01 12:00:00")
        await events.set_status(conn, event_id, "closed")
        due = await events.events_due_teardown(conn, now)
        assert [e.id for e in due] == [event_id]
    finally:
        await conn.close()


async def test_events_due_teardown_excludes_future_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 0, 0, 0)
        await _make_event(conn, starts_at="2026-01-02 00:00:00")
        due = await events.events_due_teardown(conn, now)
        assert due == []
    finally:
        await conn.close()


async def test_events_due_teardown_excludes_done_and_cancelled_events(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 2, 12, 0, 0)
        done_id = await _make_event(conn, starts_at="2026-01-01 12:00:00")
        cancelled_id = await _make_event(conn, starts_at="2026-01-01 12:00:00")
        await events.set_status(conn, done_id, "done")
        await events.set_status(conn, cancelled_id, "cancelled")
        due = await events.events_due_teardown(conn, now)
        assert due == []
    finally:
        await conn.close()
