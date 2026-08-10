"""In-process integration tests for adjutant/cogs/admin.py: the token-bucket
rate limiter as wired into a real app_commands check, the incidents ledger,
and the shared error-handler path every cog's cog_app_command_error funnels
through.
"""
from __future__ import annotations

import pytest
from discord.app_commands import CheckFailure

from adjutant.cogs.admin import AdminCog, log_incident, rate_limited
from fakes import (
    FakeGuild,
    forbidden,
    make_interaction,
    make_member,
    next_id,
    reply_ephemeral,
    reply_text,
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
def cog(bot):
    return AdminCog(bot)


def _rate_limit_predicate():
    """rate_limited() wraps app_commands.check(predicate), which — applied
    to a plain function rather than an already-built Command — stashes the
    predicate on `__discord_app_commands_checks__` instead of a Command's
    `.checks` list (see discord.app_commands.check's source). Pulling it out
    that way tests the rate limiter itself, independent of any one command.
    """
    async def _dummy(interaction):
        return None
    decorated = rate_limited()(_dummy)
    return decorated.__discord_app_commands_checks__[0]


# --------------------------------------------------------------------------- #
# rate limiter                                                                #
# --------------------------------------------------------------------------- #

async def test_rate_limiter_allows_a_burst_then_declines_and_logs_an_incident(bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Spammer")
    predicate = _rate_limit_predicate()
    # Unique per test run: _LIMITER is a module-level singleton shared by
    # every test in the session, keyed on (guild_id, user_id, command_name).
    command_name = f"admin burst-test {next_id()}"

    interactions = [
        make_interaction(bot, guild=guild, user=member, command_name=command_name) for _ in range(6)
    ]
    results = [await predicate(interaction) for interaction in interactions]

    assert results == [True, True, True, True, True, False]
    for allowed_interaction in interactions[:5]:
        assert allowed_interaction.response.messages == [], "an allowed call must not reply at all"
    denied_interaction = interactions[-1]
    assert reply_ephemeral(denied_interaction) is True
    assert "often" in reply_text(denied_interaction).lower()
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "rate_limit" and i["detail"] == command_name for i in incidents)


async def test_rate_limiter_keeps_independent_buckets_per_command_name(bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Spammer2")
    predicate = _rate_limit_predicate()
    command_a = f"admin bucket-a {next_id()}"
    command_b = f"admin bucket-b {next_id()}"

    for _ in range(5):
        assert await predicate(make_interaction(bot, guild=guild, user=member, command_name=command_a)) is True
    # command_a's bucket is now empty, but command_b's is untouched
    assert await predicate(make_interaction(bot, guild=guild, user=member, command_name=command_a)) is False
    assert await predicate(make_interaction(bot, guild=guild, user=member, command_name=command_b)) is True


# --------------------------------------------------------------------------- #
# /incidents recent                                                           #
# --------------------------------------------------------------------------- #

async def test_incidents_recent_returns_logged_incidents_ephemerally(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await log_incident(bot, guild.id, 424242, "permission_denied", detail="team create")

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="incidents recent")
    await AdminCog.recent.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    embed = interaction.response.messages[-1]["embed"]
    assert "424242" in embed.description
    assert "permission_denied" in embed.description


async def test_incidents_recent_shows_a_clean_sheet_when_nothing_logged(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="incidents recent")

    await AdminCog.recent.callback(cog, interaction)

    embed = interaction.response.messages[-1]["embed"]
    assert "clean sheet" in embed.description.lower()


async def test_incidents_recent_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="incidents recent")

    await AdminCog.recent.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" and i["detail"] == "incidents recent" for i in incidents)


# --------------------------------------------------------------------------- #
# shared error-handler path (cog_app_command_error -> view_util)              #
# --------------------------------------------------------------------------- #

async def test_error_handler_gives_a_generic_message_without_leaking_exception_text(cog, bot, guild):
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="incidents recent")
    leaky = ValueError("column adjutant_secret_schema_v7 does not exist")

    await AdminCog.cog_app_command_error(cog, interaction, leaky)

    assert reply_ephemeral(interaction) is True
    text = reply_text(interaction)
    assert "adjutant_secret_schema_v7" not in text
    assert "column" not in text.lower()
    assert "something went wrong" in text.lower()


async def test_error_handler_gives_a_specific_message_for_forbidden(cog, bot, guild):
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="incidents recent")

    await AdminCog.cog_app_command_error(cog, interaction, forbidden("Missing Permissions"))

    assert reply_ephemeral(interaction) is True
    assert "authority" in reply_text(interaction).lower()
    assert "Missing Permissions" not in reply_text(interaction)


async def test_error_handler_is_a_noop_for_check_failures(cog, bot, guild):
    """CheckFailure means a check already messaged + logged the denial —
    the shared handler must not send a second, redundant reply."""
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="incidents recent")

    await AdminCog.cog_app_command_error(cog, interaction, CheckFailure("denied"))

    assert interaction.response.messages == []
    assert interaction.followup.messages == []
