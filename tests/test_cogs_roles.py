"""In-process integration tests for adjutant/cogs/roles.py: the rank ladder,
perma/temp/event role grants, and the background expiry sweep.

Command surface: `/rank` bare shows the ladder (open to anyone); with a
member and a role it grants (optionally timed via `for`), and the reply
carries a Revoke button. `/admin rank-revoke` is the raw fallback for when
that button isn't available. Ladder editing (ladder_add/ladder_remove) has
no slash command any more — see the module docstring in cogs/roles.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fakes import (
    FakeGuild,
    build_roles_cog,
    forbidden,
    make_interaction,
    make_member,
    make_role,
    reply_ephemeral,
    reply_text,
    seed_guild,
)

from adjutant.cogs.admin import AdminCog
from adjutant.cogs.roles import RevokeGrantView, RolesCog
from adjutant.services import grants as grants_service


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
async def cog(bot):
    return bot.register_cog(await build_roles_cog(bot))


@pytest.fixture
def admin_cog(bot):
    return AdminCog(bot)


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


# --------------------------------------------------------------------------- #
# ladder editing — plain methods only, no slash command any more              #
# --------------------------------------------------------------------------- #


async def test_ladder_add_then_bare_rank_shows_it(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    role = make_role("NCO")
    guild.roles.append(role)

    add_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await cog.ladder_add(add_interaction, role=role, position=2, name="NCO")
    assert "NCO" in reply_text(add_interaction)
    assert reply_ephemeral(add_interaction) is True

    show_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, show_interaction)
    embed = show_interaction.response.messages[-1]["embed"]
    assert "NCO" in embed.description
    assert f"<@&{role.id}>" in embed.description


async def test_ladder_remove_takes_a_rank_off_the_ladder(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    role = make_role("Temp Rank")
    guild.roles.append(role)
    await cog.ladder_add(
        make_interaction(bot, guild=guild, user=admin, command_name="rank"),
        role=role,
        position=1,
        name="Temp Rank",
    )

    remove_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await cog.ladder_remove(remove_interaction, role=role)

    assert "struck" in reply_text(remove_interaction).lower()
    show_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, show_interaction)
    assert "no rank ladder" in reply_text(show_interaction).lower()


# --------------------------------------------------------------------------- #
# bare /rank — shows the ladder, open to anyone                               #
# --------------------------------------------------------------------------- #


async def test_bare_rank_with_no_ladder_configured_says_so(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="rank")

    await RolesCog.rank.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "no rank ladder" in reply_text(interaction).lower()


# --------------------------------------------------------------------------- #
# /rank <member> <role> [for] — grant                                         #
# --------------------------------------------------------------------------- #


async def test_grant_with_duration_applies_role_and_writes_temp_grant_with_expiry(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Sniper")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")

    before = datetime.now(UTC).replace(tzinfo=None)
    await RolesCog.rank.callback(cog, interaction, member=recruit, role=role, duration="2h")
    after = datetime.now(UTC).replace(tzinfo=None)

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
    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, RevokeGrantView)


async def test_grant_without_duration_is_permanent(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Medic")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")

    await RolesCog.rank.callback(cog, interaction, member=recruit, role=role, duration=None)

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
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")

    await RolesCog.rank.callback(cog, interaction, member=recruit, role=role, duration="whenever")

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

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, interaction, member=recruit, role=role, duration=None)

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
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="rank")

    await RolesCog.rank.callback(cog, interaction, member=recruit, role=role, duration=None)

    assert reply_ephemeral(interaction) is True
    assert role not in recruit.roles
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_only_member_given_declines_and_explains_both_are_needed(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")

    await RolesCog.rank.callback(cog, interaction, member=recruit, role=None, duration=None)

    assert reply_ephemeral(interaction) is True
    assert "both" in reply_text(interaction).lower()


async def test_only_role_given_declines_and_explains_both_are_needed(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    role = make_role("Command")
    guild.roles.append(role)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")

    await RolesCog.rank.callback(cog, interaction, member=None, role=role, duration=None)

    assert reply_ephemeral(interaction) is True
    assert "both" in reply_text(interaction).lower()


# --------------------------------------------------------------------------- #
# Revoke button + /admin rank-revoke fallback                                 #
# --------------------------------------------------------------------------- #


async def test_revoke_button_removes_both_the_role_and_the_grant_row(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Corporal")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, grant_interaction, member=recruit, role=role, duration=None)
    view = grant_interaction.response.messages[-1]["view"]

    click = make_interaction(
        bot, guild=guild, user=admin, message=grant_interaction.response.message
    )
    await RevokeGrantView.revoke(view, click, None)

    assert role not in recruit.roles
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ? AND role_id = ?",
        (guild.id, recruit.id, role.id),
    )
    assert rows == []
    assert reply_ephemeral(click) is True


async def test_revoke_button_recheck_declines_a_non_privileged_clicker(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    grunt = make_member(guild, display_name="Grunt")
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Corporal")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, grant_interaction, member=recruit, role=role, duration=None)
    view = grant_interaction.response.messages[-1]["view"]

    click = make_interaction(
        bot, guild=guild, user=grunt, message=grant_interaction.response.message
    )
    await RevokeGrantView.revoke(view, click, None)

    assert role in recruit.roles  # untouched
    assert reply_ephemeral(click) is True


async def test_revoke_button_is_rate_limited_like_the_old_slash_command_was(cog, bot, guild):
    """The old /rank revoke command carried @rate_limited(); the button
    replacing it needs the same protection — see admin.check_rate_limit."""
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    role = make_role("Corporal")
    guild.roles.append(role)
    recruits = [make_member(guild, display_name=f"Recruit{i}") for i in range(6)]

    last_click = None
    for recruit in recruits:
        grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
        await RolesCog.rank.callback(
            cog, grant_interaction, member=recruit, role=role, duration=None
        )
        view = grant_interaction.response.messages[-1]["view"]
        last_click = make_interaction(
            bot, guild=guild, user=admin, message=grant_interaction.response.message
        )
        await RevokeGrantView.revoke(view, last_click, None)

    assert "often" in reply_text(last_click).lower()
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "rate_limit" and i["detail"] == "rank.revoke" for i in incidents)


async def test_admin_rank_revoke_removes_both_the_role_and_the_grant_row(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Sergeant")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, grant_interaction, member=recruit, role=role, duration=None)

    revoke_interaction = make_interaction(
        bot, guild=guild, user=admin, command_name="admin rank-revoke"
    )
    await AdminCog.rank_revoke.callback(admin_cog, revoke_interaction, member=recruit, role=role)

    assert role not in recruit.roles
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ? AND role_id = ?",
        (guild.id, recruit.id, role.id),
    )
    assert rows == []
    assert reply_ephemeral(revoke_interaction) is True


async def test_admin_rank_revoke_does_not_lose_the_grant_record_when_discord_refuses_removal(
    admin_cog, cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Sergeant")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, grant_interaction, member=recruit, role=role, duration=None)

    async def _raise_forbidden(*args, **kwargs):
        raise forbidden("Missing Permissions")

    recruit.remove_roles = _raise_forbidden

    revoke_interaction = make_interaction(
        bot, guild=guild, user=admin, command_name="admin rank-revoke"
    )
    await AdminCog.rank_revoke.callback(admin_cog, revoke_interaction, member=recruit, role=role)

    assert reply_ephemeral(revoke_interaction) is True
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM role_grants WHERE guild_id = ? AND user_id = ? AND role_id = ?",
        (guild.id, recruit.id, role.id),
    )
    assert len(rows) == 1, "the grant row should survive a failed Discord-side removal"


async def test_admin_rank_revoke_is_refused_for_a_non_privileged_member(admin_cog, cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    grunt = make_member(guild, display_name="Grunt")
    recruit = make_member(guild, display_name="Recruit")
    role = make_role("Sergeant")
    guild.roles.append(role)
    grant_interaction = make_interaction(bot, guild=guild, user=admin, command_name="rank")
    await RolesCog.rank.callback(cog, grant_interaction, member=recruit, role=role, duration=None)

    revoke_interaction = make_interaction(
        bot, guild=guild, user=grunt, command_name="admin rank-revoke"
    )
    await AdminCog.rank_revoke.callback(admin_cog, revoke_interaction, member=recruit, role=role)

    assert role in recruit.roles  # untouched
    assert reply_ephemeral(revoke_interaction) is True


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
        bot.db,
        guild_id=guild.id,
        user_id=member.id,
        role_id=due_role.id,
        kind="temp",
        granted_by=1,
        expires_at="2020-01-01 00:00:00",
    )
    live_id = await grants_service.record_grant(
        bot.db,
        guild_id=guild.id,
        user_id=member.id,
        role_id=live_role.id,
        kind="temp",
        granted_by=1,
        expires_at="2099-01-01 00:00:00",
    )
    perma_id = await grants_service.record_grant(
        bot.db,
        guild_id=guild.id,
        user_id=member.id,
        role_id=perma_role.id,
        kind="perma",
        granted_by=1,
    )

    await RolesCog.expire_grants.coro(cog)

    assert due_role not in member.roles
    assert live_role in member.roles
    assert perma_role in member.roles

    remaining_ids = {
        r["id"]
        for r in await bot.db.execute_fetchall(
            "SELECT id FROM role_grants WHERE guild_id = ?", (guild.id,)
        )
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
        bot.db,
        guild_id=guild.id,
        user_id=member.id,
        role_id=role.id,
        kind="temp",
        granted_by=1,
        expires_at="2020-01-01 00:00:00",
    )

    await RolesCog.expire_grants.coro(cog)

    remaining_ids = {
        r["id"]
        for r in await bot.db.execute_fetchall(
            "SELECT id FROM role_grants WHERE guild_id = ?", (guild.id,)
        )
    }
    assert grant_id in remaining_ids


async def test_expiry_sweep_discards_a_grant_whose_member_has_left(cog, bot, guild):
    """Nothing left to reclaim, so the row is stale rather than pending —
    keeping it would mean retrying forever."""
    await seed_guild(bot.db, guild.id)
    role = make_role("Expired Temp")
    guild.roles.append(role)

    grant_id = await grants_service.record_grant(
        bot.db,
        guild_id=guild.id,
        user_id=999_999,
        role_id=role.id,
        kind="temp",
        granted_by=1,
        expires_at="2020-01-01 00:00:00",
    )

    await RolesCog.expire_grants.coro(cog)

    remaining_ids = {
        r["id"]
        for r in await bot.db.execute_fetchall(
            "SELECT id FROM role_grants WHERE guild_id = ?", (guild.id,)
        )
    }
    assert grant_id not in remaining_ids
