"""Crash-safe idempotent-posting substrate.

Three primitives, all keyed by guild and all operating on a connection
the caller already holds open (typically `bot.db`) rather than opening
their own — the bot keeps a single shared `aiosqlite.Connection` per
`adjutant/db.py`, and these helpers are meant to compose into whatever
transaction a cog is already running. They assume `conn.row_factory` is
`aiosqlite.Row`, which is true for any connection returned by
`adjutant.db.connect()`.

- **bot_state** — per-guild key/value store for small scalar state (a
  last-sweep timestamp, a one-shot dismissal, ...). The load-bearing
  rule: stamp state *before* the side effect it guards, so a crash
  mid-flow can't cause the next pass to re-fire. The write that matters
  for safety is the first one, not the last.

- **posted_messages** — idempotency log for one-shot posts, keyed by
  `(kind, identity, guild_id)`. `identity` is a caller-chosen
  deterministic key for the *occurrence* being posted — an event id, an
  ISO week like `"2026-W17"` — never a Discord message id. Callers call
  `find_posted_message()` before posting; a hit means skip. After a
  successful send, call `record_posted_message()` to log it. A crash
  between post and record leaves no row, so a retry may double-post in
  that narrow window — acceptable, since the alternative (record before
  send) risks silently skipping a post that never actually went out.

- **panels** — one durable message per `(guild, kind)`: a cog's
  editable "control panel" (a live map render, a setup summary, ...).
  `get_panel()` lets a cog find its own message and edit it in place
  instead of re-posting on every mutation; `set_panel()` upserts after a
  (re)post; `clear_panel()` forgets it (e.g. on teardown).

Every mutating call here ends with `await conn.commit()` — state is
durable the instant the call returns, since the whole point is
surviving a crash immediately after.
"""

from __future__ import annotations

import aiosqlite

# --------------------------------------------------------------------------- #
# bot_state                                                                    #
# --------------------------------------------------------------------------- #


async def get_bot_state(
    conn: aiosqlite.Connection,
    guild_id: int,
    key: str,
) -> str | None:
    async with conn.execute(
        "SELECT value FROM bot_state WHERE guild_id = ? AND key = ?",
        (guild_id, key),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def set_bot_state(
    conn: aiosqlite.Connection,
    guild_id: int,
    key: str,
    value: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO bot_state (guild_id, key, value, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(guild_id, key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (guild_id, key, value),
    )
    await conn.commit()


# --------------------------------------------------------------------------- #
# posted_messages                                                              #
# --------------------------------------------------------------------------- #


async def find_posted_message(
    conn: aiosqlite.Connection,
    kind: str,
    identity: str,
    guild_id: int,
) -> aiosqlite.Row | None:
    """Return the posted_messages row for (kind, identity, guild_id), or
    None if nothing was recorded for that key."""
    async with conn.execute(
        "SELECT * FROM posted_messages WHERE kind = ? AND identity = ? AND guild_id = ?",
        (kind, identity, guild_id),
    ) as cur:
        return await cur.fetchone()


async def record_posted_message(
    conn: aiosqlite.Connection,
    *,
    kind: str,
    identity: str,
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    """Stamp a (kind, identity, guild) tuple as posted. ON CONFLICT DO
    NOTHING keeps the original record if called twice for the same key
    (e.g. a retry after a transient error elsewhere in the flow)."""
    await conn.execute(
        """
        INSERT INTO posted_messages
            (kind, identity, guild_id, channel_id, message_id, posted_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(kind, identity, guild_id) DO NOTHING
        """,
        (kind, identity, guild_id, channel_id, message_id),
    )
    await conn.commit()


# --------------------------------------------------------------------------- #
# panels                                                                       #
# --------------------------------------------------------------------------- #


async def get_panel(
    conn: aiosqlite.Connection,
    guild_id: int,
    kind: str,
) -> aiosqlite.Row | None:
    async with conn.execute(
        "SELECT * FROM panels WHERE guild_id = ? AND kind = ?",
        (guild_id, kind),
    ) as cur:
        return await cur.fetchone()


async def set_panel(
    conn: aiosqlite.Connection,
    guild_id: int,
    kind: str,
    channel_id: int,
    message_id: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO panels (guild_id, kind, channel_id, message_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, kind) DO UPDATE SET
            channel_id = excluded.channel_id,
            message_id = excluded.message_id
        """,
        (guild_id, kind, channel_id, message_id),
    )
    await conn.commit()


async def clear_panel(
    conn: aiosqlite.Connection,
    guild_id: int,
    kind: str,
) -> None:
    await conn.execute(
        "DELETE FROM panels WHERE guild_id = ? AND kind = ?",
        (guild_id, kind),
    )
    await conn.commit()
