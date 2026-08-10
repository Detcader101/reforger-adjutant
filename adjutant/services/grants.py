"""Role-grant bookkeeping: record/revoke, expiry queries, event-scoped queries.

Operates directly on the `role_grants` table via an aiosqlite connection
passed in by the caller (cogs own the connection lifecycle). No discord
imports — timestamps are plain datetimes/strings, ids are plain ints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

_VALID_KINDS = ("perma", "temp", "event")

# Matches how SQLite's own `datetime('now')` default formats TEXT columns
# elsewhere in the schema (created_at, granted_at, ...), so expiry
# comparisons stay simple lexicographic string comparisons.
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_DURATION_RE = re.compile(r"^(\d+)([mhdw])$")
_DURATION_UNITS = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


@dataclass(frozen=True, slots=True)
class Grant:
    id: int
    guild_id: int
    user_id: int
    role_id: int
    kind: str
    expires_at: str | None
    event_id: int | None
    granted_by: int
    granted_at: str


def format_timestamp(dt: datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def parse_duration(text: str) -> timedelta:
    """Parse a short duration like "2h" or "3d" into a timedelta.

    Supports a single integer amount plus one unit: m(inutes), h(ours),
    d(ays), w(eeks). Raises ValueError on anything else.
    """
    match = _DURATION_RE.match(text.strip().lower())
    if not match:
        raise ValueError(
            f"Invalid duration {text!r}; expected a number plus one of m/h/d/w, e.g. '2h' or '3d'."
        )
    amount, unit = match.groups()
    return int(amount) * _DURATION_UNITS[unit]


def compute_expiry(now: datetime, duration_text: str) -> datetime:
    return now + parse_duration(duration_text)


def _row_to_grant(row: aiosqlite.Row) -> Grant:
    return Grant(
        id=row["id"],
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        role_id=row["role_id"],
        kind=row["kind"],
        expires_at=row["expires_at"],
        event_id=row["event_id"],
        granted_by=row["granted_by"],
        granted_at=row["granted_at"],
    )


async def record_grant(
    conn: aiosqlite.Connection,
    *,
    guild_id: int,
    user_id: int,
    role_id: int,
    kind: str,
    granted_by: int,
    expires_at: str | None = None,
    event_id: int | None = None,
) -> int:
    """Insert a role_grants row. Returns the new row id.

    Validates the kind/field combination before touching the database:
    'temp' requires expires_at, 'event' requires event_id.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid grant kind {kind!r}; expected one of {_VALID_KINDS}.")
    if kind == "temp" and expires_at is None:
        raise ValueError("A 'temp' grant requires expires_at.")
    if kind == "event" and event_id is None:
        raise ValueError("An 'event' grant requires event_id.")

    cursor = await conn.execute(
        "INSERT INTO role_grants (guild_id, user_id, role_id, kind, expires_at, event_id, granted_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, role_id, kind, expires_at, event_id, granted_by),
    )
    await conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def revoke_grant(conn: aiosqlite.Connection, *, guild_id: int, user_id: int, role_id: int) -> int:
    """Delete all grants matching guild/user/role, regardless of kind.
    Returns the number of rows removed."""
    cursor = await conn.execute(
        "DELETE FROM role_grants WHERE guild_id = ? AND user_id = ? AND role_id = ?",
        (guild_id, user_id, role_id),
    )
    await conn.commit()
    return cursor.rowcount


async def revoke_grant_by_id(conn: aiosqlite.Connection, grant_id: int) -> bool:
    """Delete a single grant by row id. Returns whether a row was removed."""
    cursor = await conn.execute("DELETE FROM role_grants WHERE id = ?", (grant_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def due_expiries(conn: aiosqlite.Connection, now: datetime) -> list[Grant]:
    """Temp grants whose expires_at has passed, as of `now`."""
    rows = await conn.execute_fetchall(
        "SELECT * FROM role_grants WHERE kind = 'temp' AND expires_at IS NOT NULL AND expires_at <= ?",
        (format_timestamp(now),),
    )
    return [_row_to_grant(row) for row in rows]


async def grants_for_event(conn: aiosqlite.Connection, event_id: int) -> list[Grant]:
    """All grants bound to a given event (for teardown)."""
    rows = await conn.execute_fetchall(
        "SELECT * FROM role_grants WHERE kind = 'event' AND event_id = ?",
        (event_id,),
    )
    return [_row_to_grant(row) for row in rows]
