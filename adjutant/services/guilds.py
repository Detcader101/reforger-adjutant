"""Guild-row bookkeeping.

Every feature table (teams, events, maps, ranks, role_grants, server_links)
carries a foreign key to `guilds`, and `PRAGMA foreign_keys=ON` is set, so a
missing row makes every feature write fail. `/setup` creates the row, but a
guild can use commands before running it — and `/team create` builds the
Discord role and channels *before* its insert, so the failure would strand
real objects in the server.

So the bot guarantees the row exists from the moment it can see a guild:
backfilled on ready and created on join. Settings are never overwritten —
ensuring is always safe to repeat.
"""

from __future__ import annotations

from collections.abc import Iterable

import aiosqlite


async def ensure_guild(conn: aiosqlite.Connection, guild_id: int) -> bool:
    """Create the guild's row if absent. Returns True if one was created.

    Existing settings are left untouched: this is INSERT OR IGNORE, not an
    upsert, so calling it on a fully configured guild is a no-op.
    """
    cursor = await conn.execute("INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)", (guild_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def ensure_many(conn: aiosqlite.Connection, guild_ids: Iterable[int]) -> int:
    """Ensure rows for several guilds. Returns how many were newly created."""
    created = 0
    for guild_id in guild_ids:
        if await ensure_guild(conn, guild_id):
            created += 1
    return created


async def get_guild(conn: aiosqlite.Connection, guild_id: int) -> aiosqlite.Row | None:
    async with conn.execute("SELECT * FROM guilds WHERE guild_id = ?", (guild_id,)) as cur:
        return await cur.fetchone()
