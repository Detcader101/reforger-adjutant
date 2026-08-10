"""Every guild the bot can see must have a `guilds` row before any feature
writes, because every feature table has a foreign key to it. Without this,
a command used before /setup fails with an opaque IntegrityError — after
the Discord-side objects have already been created.
"""

import pytest

from adjutant.services import guilds as guilds_service


async def test_a_guild_row_is_created_for_a_guild_that_has_none(tmp_conn):
    await guilds_service.ensure_guild(tmp_conn, 4242)

    row = await guilds_service.get_guild(tmp_conn, 4242)
    assert row is not None


async def test_ensuring_twice_keeps_one_row_and_preserves_settings(tmp_conn):
    await guilds_service.ensure_guild(tmp_conn, 4242)
    await tmp_conn.execute(
        "UPDATE guilds SET minimal_mode = 1, audit_channel = 77 WHERE guild_id = ?", (4242,)
    )
    await tmp_conn.commit()

    await guilds_service.ensure_guild(tmp_conn, 4242)

    row = await guilds_service.get_guild(tmp_conn, 4242)
    assert row["minimal_mode"] == 1
    assert row["audit_channel"] == 77


async def test_feature_writes_succeed_after_ensuring_the_guild(tmp_conn):
    await guilds_service.ensure_guild(tmp_conn, 4242)

    await tmp_conn.execute(
        "INSERT INTO teams (guild_id, name, role_id, category_id) VALUES (?, 'Alpha', 1, 2)", (4242,)
    )
    await tmp_conn.commit()


async def test_feature_writes_fail_without_a_guild_row(tmp_conn):
    """Guards the reason ensure_guild exists — if this ever stops raising,
    the foreign keys have been weakened and the guarantee is gone."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await tmp_conn.execute(
            "INSERT INTO teams (guild_id, name, role_id, category_id) VALUES (?, 'Alpha', 1, 2)",
            (9999,),
        )
        await tmp_conn.commit()


async def test_ensure_many_creates_rows_for_every_guild(tmp_conn):
    created = await guilds_service.ensure_many(tmp_conn, [1, 2, 3])

    assert created == 3
    for gid in (1, 2, 3):
        assert await guilds_service.get_guild(tmp_conn, gid) is not None


async def test_ensure_many_reports_only_newly_created_guilds(tmp_conn):
    await guilds_service.ensure_guild(tmp_conn, 1)

    created = await guilds_service.ensure_many(tmp_conn, [1, 2])

    assert created == 1
