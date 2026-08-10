"""In-process integration tests for adjutant/cogs/setup.py.

Focused on re-running setup, because "did that work? let me run it again"
is one of the first things a new admin does.
"""
from __future__ import annotations

import pytest

from adjutant.cogs.setup import DEFAULT_LADDER, _create_default_ladder
from fakes import FakeGuild, seed_guild


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


async def test_creating_the_default_ladder_records_every_rank(bot, guild):
    await seed_guild(bot.db, guild.id)

    created = await _create_default_ladder(bot, guild)

    assert len(created) == len(DEFAULT_LADDER)
    rows = await bot.db.execute_fetchall("SELECT * FROM ranks WHERE guild_id = ?", (guild.id,))
    assert len(rows) == len(DEFAULT_LADDER)


async def test_running_setup_twice_reuses_the_ladder_roles_it_already_made(bot, guild):
    """Otherwise a second run leaves the server with two 'Recruit' roles,
    two 'Private' roles and a rank table with two entries per position."""
    await seed_guild(bot.db, guild.id)
    await _create_default_ladder(bot, guild)
    roles_after_first = [r.name for r in guild.roles]

    await _create_default_ladder(bot, guild)

    assert [r.name for r in guild.roles] == roles_after_first
    rows = await bot.db.execute_fetchall("SELECT * FROM ranks WHERE guild_id = ?", (guild.id,))
    assert len(rows) == len(DEFAULT_LADDER)


async def test_a_rank_role_the_admin_made_is_adopted_but_not_marked_bot_created(bot, guild):
    """Adopting an existing role by name avoids duplicates, but /teardown
    must never delete a role the bot didn't create."""
    await seed_guild(bot.db, guild.id)
    existing = await guild.create_role(name=DEFAULT_LADDER[0][1])

    await _create_default_ladder(bot, guild)

    rows = await bot.db.execute_fetchall(
        "SELECT * FROM ranks WHERE guild_id = ? AND role_id = ?", (guild.id, existing.id)
    )
    assert len(rows) == 1
    assert rows[0]["bot_created"] == 0
