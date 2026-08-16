"""Migration runner behaviour: applies in order, records version, is idempotent."""

from pathlib import Path

from adjutant import db as database


async def test_fresh_database_gets_all_migrations_applied(tmp_path: Path):
    conn = await database.connect(tmp_path / "t.db")
    try:
        async with conn.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
            row = await cur.fetchone()
        expected = max(n for n, _ in database.discover_migrations())
        assert row["v"] == expected
    finally:
        await conn.close()


async def test_reconnecting_does_not_reapply_migrations(tmp_path: Path):
    p = tmp_path / "t.db"
    conn = await database.connect(p)
    await conn.close()
    conn = await database.connect(p)  # would raise "table already exists" if reapplied
    try:
        async with conn.execute("SELECT COUNT(*) AS c FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row["c"] == len(database.discover_migrations())
    finally:
        await conn.close()


def test_non_conforming_filenames_are_ignored(tmp_path: Path):
    (tmp_path / "0001_real.sql").write_text("SELECT 1;")
    (tmp_path / "notes.sql").write_text("SELECT 1;")
    (tmp_path / "02_bad.sql").write_text("SELECT 1;")
    found = database.discover_migrations(tmp_path)
    assert [n for n, _ in found] == [1]
