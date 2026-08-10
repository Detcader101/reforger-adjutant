"""SQLite access + startup migrations.

Migrations are numbered .sql files in adjutant/migrations/, applied in order;
the highest applied number is tracked in the `schema_version` table.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Return (number, path) pairs sorted ascending; ignores non-conforming files."""
    found = []
    for p in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return found


async def connect(db_path: Path) -> aiosqlite.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await _migrate(db)
    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    async with db.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
        row = await cur.fetchone()
    current = row["v"] or 0
    for number, path in discover_migrations():
        if number <= current:
            continue
        log.info("Applying migration %s", path.name)
        await db.executescript(path.read_text(encoding="utf-8"))
        await db.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
        await db.commit()
