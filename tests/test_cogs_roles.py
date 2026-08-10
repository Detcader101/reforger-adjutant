"""In-process integration tests for adjutant/cogs/roles.py: the rank ladder,
perma/temp/event role grants, and the background expiry sweep.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adjutant.cogs.roles import RolesCog
from adjutant.services import grants as grants_service
from fakes import (
    FakeGuild,
    build_roles_cog,
    forbidden,
    make_interaction,
    make_member,
    make_role,
    reply_ephemeral,
    reply_text,
    run_checks,
    seed_guild,
)


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
async def cog(bot):
    return await build_roles_cog(bot)


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


# --------------------------------------------------------------------------- #
# ladder                                                                       #
# --------------------------------------------------------------------------- #

async def test_ladder_add_then_ladder_show_reflects_it(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    role = make_role("NCO")
    guild.roles.append(role)

    add_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank ladder-add")
    await RolesCog.ladder_add.callback(cog, add_interaction, role=role, position=2, name="NCO")
    assert "NCO" in reply_text(add_interaction)
    assert reply_ephemeral(add_interaction) is True

    show_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank ladder-show")
    await RolesCog.ladder_show.callback(cog, show_interaction)
    embed = show_interaction.response.messages[-1]["embed"]
    assert "NCO" in embed.description
    assert f"<@&{role.id}>" in embed.description


async def test_ladder_add_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    role = make_role("NCO")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="rank ladder-add")

    allowed = await run_checks(RolesCog.ladder_add, interaction)

    assert allowed is False
    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()


# --------------------------------------------------------------------------- #
# grants                                                                       #
# --------------------------------------------------------------------------- #

async def test_grant_with_duration_applies_role_and_writes_temp_grant_with_expiry(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Sniper")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank grant")

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await RolesCog.grant.callback(cog, interaction, member=recruit, role=role, duration="2h")
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    assert role in recruit.roles
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ?", (guild.id, recruit.id)
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "temp"
    expiry = datetime.strptime(rows[0]["expires_at"], "%Y-%m-%d %H:%M:%S")
    assert before + timedelta(hours=2) - timedelta(seconds=5) <= expiry
    assert expiry <= after + timedelta(hours=2) + timedelta(seconds=5)
    assert reply_ephemeral(interaction) is True


async def test_grant_without_duration_is_permanent(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Medic")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank grant")

    await RolesCog.grant.callback(cog, interaction, member=recruit, role=role, duration=None)

    assert role in recruit.roles
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ?", (guild.id, recruit.id)
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "perma"
    assert rows[0]["expires_at"] is None


async def test_grant_with_unparseable_duration_declines_and_records_nothing(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Engineer")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank grant")

    await RolesCog.grant.callback(cog, interaction, member=recruit, role=role, duration="whenever")

    assert "couldn't parse" in reply_text(interaction).lower()
    assert role not in recruit.roles
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ?", (guild.id, recruit.id)
    )
    assert rows == []


async def test_grant_reports_forbidden_and_records_no_grant(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Officer")
    guild.roles.append(role)

    async def _raise_forbidden(*args, **kwargs):
        raise forbidden("Missing Permissions")
    recruit.add_roles = _raise_forbidden

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank grant")
    await RolesCog.grant.callback(cog, interaction, member=recruit, role=role, duration=None)

    assert reply_ephemeral(interaction) is True
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ?", (guild.id, recruit.id)
    )
    assert rows == []


async def test_grant_is_refused_for_a_non_privileged_member(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Command")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="rank grant")

    allowed = await run_checks(RolesCog.grant, interaction)

    assert allowed is False
    assert reply_ephemeral(interaction) is True
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_revoke_removes_both_the_role_and_the_grant_row(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Corporal")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank grant")
    await RolesCog.grant.callback(cog, grant_interaction, member=recruit, role=role, duration=None)

    revoke_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank revoke")
    await RolesCog.revoke.callback(cog, revoke_interaction, member=recruit, role=role)

    assert role not in recruit.roles
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ? AND role_id = ?",
        (guild.id, recruit.id, role.id),
    )
    assert rows == []
    assert reply_ephemeral(revoke_interaction) is True


async def test_revoke_does_not_lose_the_grant_record_when_discord_refuses_removal(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Sergeant")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank grant")
    await RolesCog.grant.callback(cog, grant_interaction, member=recruit, role=role, duration=None)

    async def _raise_forbidden(*args, **kwargs):
        raise forbidden("Missing Permissions")
    recruit.remove_roles = _raise_forbidden

    revoke_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank revoke")
    await RolesCog.revoke.callback(cog, revoke_interaction, member=recruit, role=role)

    assert reply_ephemeral(revoke_interaction) is True
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ? AND role_id = ?",
        (guild.id, recruit.id, role.id),
    )
    assert len(rows) == 1, "the grant row should survive a failed Discord-side removal"


# --------------------------------------------------------------------------- #
# background expiry sweep                                                     #
# --------------------------------------------------------------------------- #

async def test_expiry_sweep_removes_only_due_grants_and_leaves_perma_alone(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    due_role = make_role("Expired Temp")
    live_role = make_role("Live Temp")
    perma_role = make_role("Perma")
    guild.roles.extend([due_role, live_role, perma_role])
    member = make_member(guild, display_name="Recruit", roles=[due_role, live_role, perma_role])

    due_id = await grants_service.record_grant(
        bot.db, guild_id=guild.id, user_id=member.id, role_id=due_role.id,
        kind="temp", granted_by=1, expires_at="2020-01-01 00:00:00",
    )
    live_id = await grants_service.record_grant(
        bot.db, guild_id=guild.id, user_id=member.id, role_id=live_role.id,
        kind="temp", granted_by=1, expires_at="2099-01-01 00:00:00",
    )
    perma_id = await grants_service.record_grant(
        bot.db, guild_id=guild.id, user_id=member.id, role_id=perma_role.id,
        kind="perma", granted_by=1,
    )

    await RolesCog.expire_grants.coro(cog)

    assert due_role not in member.roles
    assert live_role in member.roles
    assert perma_role in member.roles

    remaining_ids = {
        r["id"] for r in await bot.db.execute_fetchall("SELECT id FROM role_grants WHERE guild_id = ?", (guild.id,))
    }
    assert due_id not in remaining_ids
    assert live_id in remaining_ids
    assert perma_id in remaining_ids


async def test_expiry_sweep_keeps_a_grant_it_could_not_remove_so_it_retries(cog, bot, guild):
    """Dropping the record on a failed removal would quietly promote a
    temporary rank to a permanent one — the exact thing temp grants exist
    to prevent."""
    await seed_guild(bot.db, guild.id)
    role = make_role("Expired Temp")
    guild.roles.append(role)
    member = make_member(guild, display_name="Recruit", roles=[role])

    async def _raise_forbidden(*args, **kwargs):
        raise forbidden("Missing Permissions")
    member.remove_roles = _raise_forbidden

    grant_id = await grants_service.record_grant(
        bot.db, guild_id=guild.id, user_id=member.id, role_id=role.id,
        kind="temp", granted_by=1, expires_at="2020-01-01 00:00:00",
    )

    await RolesCog.expire_grants.coro(cog)

    remaining_ids = {
        r["id"] for r in await bot.db.execute_fetchall("SELECT id FROM role_grants WHERE guild_id = ?", (guild.id,))
    }
    assert grant_id in remaining_ids


async def test_expiry_sweep_discards_a_grant_whose_member_has_left(cog, bot, guild):
    """Nothing left to reclaim, so the row is stale rather than pending —
    keeping it would mean retrying forever."""
    await seed_guild(bot.db, guild.id)
    role = make_role("Expired Temp")
    guild.roles.append(role)

    grant_id = await grants_service.record_grant(
        bot.db, guild_id=guild.id, user_id=999_999, role_id=role.id,
        kind="temp", granted_by=1, expires_at="2020-01-01 00:00:00",
    )

    await RolesCog.expire_grants.coro(cog)

    remaining_ids = {
        r["id"] for r in await bot.db.execute_fetchall("SELECT id FROM role_grants WHERE guild_id = ?", (guild.id,))
    }
    assert grant_id not in remaining_ids
