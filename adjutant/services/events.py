"""Event bookkeeping: creation, status transitions, signups, and the
due-queries the background loop in cogs/events.py polls.

Operates directly on the `events` / `event_signups` tables via an aiosqlite
connection passed in by the caller (cogs own the connection lifecycle), same
shape as services/grants.py. No discord imports — timestamps are plain
datetimes/strings, ids are plain ints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from . import grants as grants_service

# Matches how SQLite's own `datetime('now')` default formats TEXT columns
# elsewhere in the schema, and how services/grants.py stores expires_at — so
# starts_at comparisons stay simple lexicographic string comparisons.
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_ABSOLUTE_INPUT_FORMAT = "%Y-%m-%d %H:%M"

# open -> closed -> done is the normal lifecycle; open -> done is also valid
# (teardown can run on an event that was never explicitly closed); cancelled
# is reachable from either live state but is terminal, same as done.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"closed", "done", "cancelled"},
    "closed": {"done", "cancelled"},
    "done": set(),
    "cancelled": set(),
}

_LIVE_STATUSES = ("open", "closed")


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    guild_id: int
    name: str
    description: str
    starts_at: str
    created_by: int
    announce_channel: int | None
    announce_message: int | None
    event_role_id: int | None
    status: str
    reminded: int
    created_at: str


def format_timestamp(dt: datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def stored_to_datetime(text: str) -> datetime:
    """Inverse of format_timestamp — parses a stored starts_at value back
    into a naive UTC datetime, e.g. for building Discord timestamp markdown."""
    return datetime.strptime(text, _TIMESTAMP_FORMAT)


def parse_start(text: str, now: datetime) -> datetime:
    """Parse an event start time: either a relative duration ("2h", "3d",
    reusing services.grants.parse_duration) or an absolute UTC datetime
    ("YYYY-MM-DD HH:MM"). Raises ValueError with a helpful message on
    anything unparseable, or on a result that isn't after `now`.
    """
    stripped = text.strip()
    try:
        start = now + grants_service.parse_duration(stripped)
    except ValueError:
        try:
            start = datetime.strptime(stripped, _ABSOLUTE_INPUT_FORMAT)
        except ValueError:
            raise ValueError(
                f"Couldn't parse start time {text!r} — use a relative duration like '2h' or "
                "'3d', or an absolute UTC time like '2026-08-12 18:00'."
            ) from None

    if start <= now:
        raise ValueError(f"Start time {text!r} doesn't land in the future — pick a later time.")
    return start


def _row_to_event(row: aiosqlite.Row) -> Event:
    return Event(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        description=row["description"],
        starts_at=row["starts_at"],
        created_by=row["created_by"],
        announce_channel=row["announce_channel"],
        announce_message=row["announce_message"],
        event_role_id=row["event_role_id"],
        status=row["status"],
        reminded=row["reminded"],
        created_at=row["created_at"],
    )


async def create_event(
    conn: aiosqlite.Connection,
    *,
    guild_id: int,
    name: str,
    starts_at: str,
    created_by: int,
    description: str = "",
    event_role_id: int | None = None,
) -> int:
    """Insert an events row. Returns the new row id."""
    cursor = await conn.execute(
        "INSERT INTO events (guild_id, name, description, starts_at, created_by, event_role_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, name, description, starts_at, created_by, event_role_id),
    )
    await conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def get_event(conn: aiosqlite.Connection, event_id: int) -> Event | None:
    async with conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_event(row) if row is not None else None


async def get_event_by_announce_message(
    conn: aiosqlite.Connection, message_id: int
) -> Event | None:
    async with conn.execute(
        "SELECT * FROM events WHERE announce_message = ?", (message_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_event(row) if row is not None else None


async def list_upcoming(conn: aiosqlite.Connection, guild_id: int, now: datetime) -> list[Event]:
    """Open/closed events in `guild_id` starting at or after `now`, soonest
    first."""
    rows = await conn.execute_fetchall(
        "SELECT * FROM events WHERE guild_id = ? AND status IN ('open', 'closed') AND starts_at >= ? "
        "ORDER BY starts_at ASC",
        (guild_id, format_timestamp(now)),
    )
    return [_row_to_event(row) for row in rows]


async def set_announce_message(
    conn: aiosqlite.Connection, event_id: int, *, channel_id: int, message_id: int
) -> None:
    await conn.execute(
        "UPDATE events SET announce_channel = ?, announce_message = ? WHERE id = ?",
        (channel_id, message_id, event_id),
    )
    await conn.commit()


async def set_status(conn: aiosqlite.Connection, event_id: int, new_status: str) -> Event:
    """Move an event to `new_status`, honouring the lifecycle:
    open -> closed -> done, open -> done, and (open|closed) -> cancelled.
    Raises ValueError on a missing event or an invalid transition.
    """
    event = await get_event(conn, event_id)
    if event is None:
        raise ValueError(f"No event with id {event_id}.")
    allowed = _ALLOWED_TRANSITIONS.get(event.status, set())
    if new_status not in allowed:
        raise ValueError(f"Can't move event {event_id} from {event.status!r} to {new_status!r}.")

    await conn.execute("UPDATE events SET status = ? WHERE id = ?", (new_status, event_id))
    await conn.commit()
    updated = await get_event(conn, event_id)
    assert updated is not None
    return updated


async def add_signup(conn: aiosqlite.Connection, event_id: int, user_id: int) -> bool:
    """Sign a user up for an event. Idempotent: returns False if they were
    already signed up."""
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO event_signups (event_id, user_id) VALUES (?, ?)", (event_id, user_id)
    )
    await conn.commit()
    return cursor.rowcount > 0


async def remove_signup(conn: aiosqlite.Connection, event_id: int, user_id: int) -> bool:
    """Withdraw a user from an event. Idempotent: returns False if they
    weren't signed up."""
    cursor = await conn.execute(
        "DELETE FROM event_signups WHERE event_id = ? AND user_id = ?", (event_id, user_id)
    )
    await conn.commit()
    return cursor.rowcount > 0


async def signups_for_event(conn: aiosqlite.Connection, event_id: int) -> list[int]:
    rows = await conn.execute_fetchall(
        "SELECT user_id FROM event_signups WHERE event_id = ? ORDER BY signed_at ASC, user_id ASC",
        (event_id,),
    )
    return [row["user_id"] for row in rows]


async def mark_reminded(conn: aiosqlite.Connection, event_id: int) -> None:
    await conn.execute("UPDATE events SET reminded = 1 WHERE id = ?", (event_id,))
    await conn.commit()


async def events_due_reminder(
    conn: aiosqlite.Connection, now: datetime, lead: timedelta
) -> list[Event]:
    """Live (open/closed) events starting within `lead` of `now`, not yet
    reminded, and not already started."""
    rows = await conn.execute_fetchall(
        "SELECT * FROM events WHERE status IN ('open', 'closed') AND reminded = 0 "
        "AND starts_at > ? AND starts_at <= ? ORDER BY starts_at ASC",
        (format_timestamp(now), format_timestamp(now + lead)),
    )
    return [_row_to_event(row) for row in rows]


async def events_due_teardown(conn: aiosqlite.Connection, now: datetime) -> list[Event]:
    """Live (open/closed) events whose start has already passed `now`.
    The caller decides how much grace to give past-start events by choosing
    what `now` to pass in (e.g. `real_now - timedelta(hours=24)`)."""
    rows = await conn.execute_fetchall(
        "SELECT * FROM events WHERE status IN ('open', 'closed') AND starts_at <= ? ORDER BY starts_at ASC",
        (format_timestamp(now),),
    )
    return [_row_to_event(row) for row in rows]
