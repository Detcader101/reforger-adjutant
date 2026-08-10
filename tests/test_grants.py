"""Role-grant bookkeeping: record/revoke, expiry queries, event-scoped queries.

Uses a real aiosqlite connection (migrated fresh per test) since grants.py
operates directly on the role_grants table — no mocking the DB layer.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from adjutant import db as database
from adjutant.services import grants


async def _conn(tmp_path: Path):
    conn = await database.connect(tmp_path / "t.db")
    await conn.execute("INSERT INTO guilds (guild_id) VALUES (1)")
    await conn.commit()
    return conn


# -- parse_duration ----------------------------------------------------------

def test_parse_duration_parses_minutes():
    assert grants.parse_duration("30m") == timedelta(minutes=30)


def test_parse_duration_parses_hours():
    assert grants.parse_duration("2h") == timedelta(hours=2)


def test_parse_duration_parses_days():
    assert grants.parse_duration("3d") == timedelta(days=3)


def test_parse_duration_parses_weeks():
    assert grants.parse_duration("1w") == timedelta(weeks=1)


def test_parse_duration_is_case_insensitive():
    assert grants.parse_duration("2H") == timedelta(hours=2)


def test_parse_duration_strips_surrounding_whitespace():
    assert grants.parse_duration("  2h  ") == timedelta(hours=2)


@pytest.mark.parametrize("bad", ["", "2", "h", "2hours", "-2h", "2.5h", "2x"])
def test_parse_duration_rejects_invalid_format(bad):
    with pytest.raises(ValueError):
        grants.parse_duration(bad)


def test_compute_expiry_adds_duration_to_reference_time():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert grants.compute_expiry(now, "2h") == now + timedelta(hours=2)


# -- record_grant / revoke_grant ---------------------------------------------

async def test_record_perma_grant_persists_with_null_expiry(tmp_path):
    conn = await _conn(tmp_path)
    try:
        grant_id = await grants.record_grant(
            conn, guild_id=1, user_id=10, role_id=20, kind="perma", granted_by=1,
        )
        row = await (await conn.execute("SELECT * FROM role_grants WHERE id = ?", (grant_id,))).fetchone()
        assert row["kind"] == "perma"
        assert row["expires_at"] is None
    finally:
        await conn.close()


async def test_record_temp_grant_without_expiry_raises():
    conn = None
    with pytest.raises(ValueError):
        await grants.record_grant(
            conn, guild_id=1, user_id=10, role_id=20, kind="temp", granted_by=1,
        )


async def test_record_event_grant_without_event_id_raises():
    with pytest.raises(ValueError):
        await grants.record_grant(
            None, guild_id=1, user_id=10, role_id=20, kind="event", granted_by=1,
        )


async def test_record_event_grant_persists_event_id(tmp_path):
    conn = await _conn(tmp_path)
    try:
        grant_id = await grants.record_grant(
            conn, guild_id=1, user_id=10, role_id=20, kind="event", granted_by=1, event_id=5,
        )
        row = await (await conn.execute("SELECT * FROM role_grants WHERE id = ?", (grant_id,))).fetchone()
        assert row["event_id"] == 5
    finally:
        await conn.close()


async def test_revoke_grant_removes_matching_rows_and_returns_count(tmp_path):
    conn = await _conn(tmp_path)
    try:
        await grants.record_grant(conn, guild_id=1, user_id=10, role_id=20, kind="perma", granted_by=1)
        await grants.record_grant(conn, guild_id=1, user_id=10, role_id=20, kind="perma", granted_by=1)
        removed = await grants.revoke_grant(conn, guild_id=1, user_id=10, role_id=20)
        assert removed == 2
        remaining = await (await conn.execute("SELECT COUNT(*) AS c FROM role_grants")).fetchone()
        assert remaining["c"] == 0
    finally:
        await conn.close()


async def test_revoke_grant_is_noop_when_no_matching_rows(tmp_path):
    conn = await _conn(tmp_path)
    try:
        removed = await grants.revoke_grant(conn, guild_id=1, user_id=999, role_id=999)
        assert removed == 0
    finally:
        await conn.close()


async def test_revoke_grant_by_id_removes_single_row(tmp_path):
    conn = await _conn(tmp_path)
    try:
        grant_id = await grants.record_grant(conn, guild_id=1, user_id=10, role_id=20, kind="perma", granted_by=1)
        ok = await grants.revoke_grant_by_id(conn, grant_id)
        assert ok is True
        row = await (await conn.execute("SELECT * FROM role_grants WHERE id = ?", (grant_id,))).fetchone()
        assert row is None
    finally:
        await conn.close()


async def test_revoke_grant_by_id_returns_false_when_missing(tmp_path):
    conn = await _conn(tmp_path)
    try:
        assert await grants.revoke_grant_by_id(conn, 99999) is False
    finally:
        await conn.close()


# -- due_expiries -------------------------------------------------------------

async def test_due_expiries_returns_temp_grants_past_expiry(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        past = grants.format_timestamp(now - timedelta(hours=1))
        await grants.record_grant(
            conn, guild_id=1, user_id=10, role_id=20, kind="temp", granted_by=1, expires_at=past,
        )
        due = await grants.due_expiries(conn, now)
        assert [g.role_id for g in due] == [20]
    finally:
        await conn.close()


async def test_due_expiries_excludes_temp_grants_not_yet_due(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        future = grants.format_timestamp(now + timedelta(hours=1))
        await grants.record_grant(
            conn, guild_id=1, user_id=10, role_id=20, kind="temp", granted_by=1, expires_at=future,
        )
        due = await grants.due_expiries(conn, now)
        assert due == []
    finally:
        await conn.close()


async def test_due_expiries_excludes_perma_and_event_grants(tmp_path):
    conn = await _conn(tmp_path)
    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        await grants.record_grant(conn, guild_id=1, user_id=10, role_id=20, kind="perma", granted_by=1)
        await grants.record_grant(conn, guild_id=1, user_id=11, role_id=21, kind="event", granted_by=1, event_id=1)
        due = await grants.due_expiries(conn, now)
        assert due == []
    finally:
        await conn.close()


# -- grants_for_event ----------------------------------------------------------

async def test_grants_for_event_returns_only_that_events_grants(tmp_path):
    conn = await _conn(tmp_path)
    try:
        await grants.record_grant(conn, guild_id=1, user_id=10, role_id=20, kind="event", granted_by=1, event_id=1)
        await grants.record_grant(conn, guild_id=1, user_id=11, role_id=20, kind="event", granted_by=1, event_id=2)
        result = await grants.grants_for_event(conn, 1)
        assert [g.user_id for g in result] == [10]
    finally:
        await conn.close()


async def test_grants_for_event_returns_empty_for_event_with_no_grants(tmp_path):
    conn = await _conn(tmp_path)
    try:
        result = await grants.grants_for_event(conn, 999)
        assert result == []
    finally:
        await conn.close()
