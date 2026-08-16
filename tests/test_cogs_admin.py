"""In-process integration tests for adjutant/cogs/admin.py: the token-bucket
rate limiter as wired into a real app_commands check, the /admin group's
raw-fallback forwarding (config's five commands, and the setup/teardown
fallbacks folded out of the old /setup subcommand group, both covered in
this file — team-disband/rank-revoke/event-cancel/event-teardown are
covered alongside their own cogs in test_cogs_teams.py/test_cogs_roles.py/
test_cogs_events.py since they need those cogs' fixtures), the incidents
ledger, and the shared error-handler path every cog's cog_app_command_error
funnels through.
"""

from __future__ import annotations

import json

import discord
import pytest
from discord import app_commands
from discord.app_commands import CheckFailure
from fakes import (
    FakeGuild,
    forbidden,
    make_interaction,
    make_member,
    make_role,
    next_id,
    reply_ephemeral,
    reply_text,
    seed_guild,
)

from adjutant.cogs.admin import AdminCog, log_incident, rate_limited
from adjutant.cogs.config import ConfigCog
from adjutant.cogs.setup import (
    DEFAULT_LADDER,
    TeardownConfirmModal,
    TeardownConfirmView,
    _create_default_ladder,
)
from adjutant.services import templates as templates_service


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


@pytest.fixture
def config_cog(bot):
    return bot.register_cog(ConfigCog(bot))


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
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(i["kind"] == "rate_limit" and i["detail"] == command_name for i in incidents)


async def test_rate_limiter_keeps_independent_buckets_per_command_name(bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Spammer2")
    predicate = _rate_limit_predicate()
    command_a = f"admin bucket-a {next_id()}"
    command_b = f"admin bucket-b {next_id()}"

    for _ in range(5):
        assert (
            await predicate(make_interaction(bot, guild=guild, user=member, command_name=command_a))
            is True
        )
    # command_a's bucket is now empty, but command_b's is untouched
    assert (
        await predicate(make_interaction(bot, guild=guild, user=member, command_name=command_a))
        is False
    )
    assert (
        await predicate(make_interaction(bot, guild=guild, user=member, command_name=command_b))
        is True
    )


# --------------------------------------------------------------------------- #
# /admin incidents                                                            #
# --------------------------------------------------------------------------- #


async def test_incidents_returns_logged_incidents_ephemerally(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    await log_incident(bot, guild.id, 424242, "permission_denied", detail="team create")

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin incidents")
    await AdminCog.incidents.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    embed = interaction.response.messages[-1]["embed"]
    assert "424242" in embed.description
    assert "permission_denied" in embed.description


async def test_incidents_shows_a_clean_sheet_when_nothing_logged(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin incidents")

    await AdminCog.incidents.callback(cog, interaction)

    embed = interaction.response.messages[-1]["embed"]
    assert "clean sheet" in embed.description.lower()


async def test_incidents_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin incidents")

    await AdminCog.incidents.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()
    incidents = await bot.db.execute_fetchall(
        "SELECT * FROM incidents WHERE guild_id = ?", (guild.id,)
    )
    assert any(
        i["kind"] == "permission_denied" and i["detail"] == "admin incidents" for i in incidents
    )


# --------------------------------------------------------------------------- #
# /admin's config forwards: feature/audit-channel/minimal/permission/reset    #
# --------------------------------------------------------------------------- #
# team-disband, rank-revoke, event-cancel and event-teardown are covered in
# test_cogs_teams.py / test_cogs_roles.py / test_cogs_events.py respectively,
# alongside the panel-button paths they mirror.


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


async def test_admin_feature_forwards_to_config_and_requires_admin(cog, config_cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin feature")

    await AdminCog.feature.callback(
        cog,
        interaction,
        feature=app_commands.Choice(name="Teams", value="teams"),
        state=app_commands.Choice(name="on", value="on"),
    )

    rows = await bot.db.execute_fetchall(
        "SELECT features FROM guilds WHERE guild_id = ?", (guild.id,)
    )
    assert json.loads(rows[0]["features"]) == {"teams": True}

    grunt = make_member(guild, display_name="Grunt")
    denied = make_interaction(bot, guild=guild, user=grunt, command_name="admin feature")
    await AdminCog.feature.callback(
        cog,
        denied,
        feature=app_commands.Choice(name="Events", value="events"),
        state=app_commands.Choice(name="on", value="on"),
    )
    rows2 = await bot.db.execute_fetchall(
        "SELECT features FROM guilds WHERE guild_id = ?", (guild.id,)
    )
    assert json.loads(rows2[0]["features"]) == {"teams": True}  # unchanged
    assert reply_ephemeral(denied) is True


async def test_admin_audit_channel_forwards_to_config(cog, config_cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    channel = guild.create_standalone_text_channel(name="audit-log")
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin audit-channel")

    await AdminCog.audit_channel.callback(cog, interaction, channel=channel)

    rows = await bot.db.execute_fetchall(
        "SELECT audit_channel FROM guilds WHERE guild_id = ?", (guild.id,)
    )
    assert rows[0]["audit_channel"] == channel.id


async def test_admin_minimal_forwards_to_config(cog, config_cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin minimal")

    await AdminCog.minimal.callback(
        cog, interaction, state=app_commands.Choice(name="on", value="on")
    )

    rows = await bot.db.execute_fetchall(
        "SELECT minimal_mode FROM guilds WHERE guild_id = ?", (guild.id,)
    )
    assert rows[0]["minimal_mode"] == 1


async def test_admin_permission_forwards_to_config(cog, config_cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    role = make_role("Officer")
    guild.roles.append(role)
    await bot.db.execute(
        "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, 0)",
        (guild.id, role.id, 3, "Officer"),
    )
    await bot.db.commit()
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin permission")

    await AdminCog.permission.callback(cog, interaction, key="teams.manage", min_rank=3)

    rows = await bot.db.execute_fetchall(
        "SELECT * FROM permissions WHERE guild_id = ? AND permission = ?",
        (guild.id, "teams.manage"),
    )
    assert len(rows) == 1
    assert rows[0]["min_rank"] == 3


async def test_admin_reset_forwards_to_config(cog, config_cog, bot, guild):
    await seed_guild(bot.db, guild.id, minimal_mode=True)
    await bot.db.execute(
        "INSERT INTO permissions (guild_id, permission, min_rank) VALUES (?, ?, ?)",
        (guild.id, "teams.manage", 3),
    )
    await bot.db.commit()
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin reset")

    await AdminCog.reset.callback(cog, interaction)

    view = interaction.response.messages[-1]["view"]
    assert (
        view is not None
    )  # ResetConfirmView — the reset flow itself is exercised in test_cogs_config.py


async def test_admin_config_fallback_reports_clearly_when_config_cog_is_not_loaded(cog, bot, guild):
    """Nothing registers ConfigCog on the bot in this test — confirms the
    /admin forward degrades politely rather than raising when a cog isn't
    up (e.g. mid-restart)."""
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin minimal")

    await AdminCog.minimal.callback(
        cog, interaction, state=app_commands.Choice(name="on", value="on")
    )

    assert reply_ephemeral(interaction) is True
    assert "isn't loaded" in reply_text(interaction).lower()


# --------------------------------------------------------------------------- #
# /admin config-show — the text-only read of the configuration               #
# --------------------------------------------------------------------------- #


async def test_admin_config_show_reports_the_configuration_as_text(cog, bot, guild):
    """The Config button is itself a component, so this is the only way to
    read the configuration when components are what's broken."""
    await seed_guild(bot.db, guild.id)
    await bot.db.execute(
        "UPDATE guilds SET features = ?, minimal_mode = 0 WHERE guild_id = ?",
        ('{"teams": true}', guild.id),
    )
    await bot.db.commit()
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin config-show")

    await AdminCog.config_show.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "eams" in reply_text(interaction)  # the Teams feature, however it's cased


async def test_admin_config_show_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin config-show")

    await AdminCog.config_show.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()


# --------------------------------------------------------------------------- #
# /admin preflight — folded out of the old /setup check                      #
# --------------------------------------------------------------------------- #


async def test_admin_preflight_reports_missing_permissions(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    guild.me.guild_permissions = discord.Permissions.none()
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin preflight")

    await AdminCog.preflight.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "manage_roles" in reply_text(interaction)


async def test_admin_preflight_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin preflight")

    await AdminCog.preflight.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()


# --------------------------------------------------------------------------- #
# /admin ranks — folded out of the old /setup ranks                          #
# --------------------------------------------------------------------------- #


async def test_admin_ranks_argument_applies_without_opening_a_view(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin ranks")

    await AdminCog.ranks.callback(cog, interaction, ranks="Recruit, Private, NCO")

    assert reply_ephemeral(interaction) is True
    assert "Recruit" in reply_text(interaction) and "NCO" in reply_text(interaction)
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert {r["name"] for r in rows} == {"Recruit", "Private", "NCO"}


async def test_admin_ranks_declines_invalid_input_and_changes_nothing(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin ranks")

    await AdminCog.ranks.callback(cog, interaction, ranks="OnlyOne")

    assert reply_ephemeral(interaction) is True
    assert "afraid not" in reply_text(interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert rows == []


async def test_admin_ranks_with_no_argument_shows_the_current_ladder_without_a_view(
    cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin ranks")

    await AdminCog.ranks.callback(cog, interaction, ranks="")

    assert reply_ephemeral(interaction) is True
    assert (
        interaction.response.messages[-1]["view"] is None
    )  # raw fallback: never opens a component
    assert "No ladder configured yet" in reply_text(interaction)


async def test_admin_ranks_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin ranks")

    await AdminCog.ranks.callback(cog, interaction, ranks="Recruit, Private")

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert rows == []


# --------------------------------------------------------------------------- #
# /admin setup-quick — folded out of the old /setup quick                    #
# --------------------------------------------------------------------------- #


async def test_admin_setup_quick_with_a_template_applies_its_ladder_and_channels(cog, bot, guild):
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin setup-quick")

    await AdminCog.setup_quick.callback(cog, interaction, template="vanilla")

    tmpl = templates_service.TEMPLATES["vanilla"]
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert {r["name"] for r in rows} == set(tmpl.ranks)
    assert any(c.name == tmpl.channels[0].category for c in guild.categories)


async def test_admin_setup_quick_rejects_an_unknown_template(cog, bot, guild):
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin setup-quick")

    await AdminCog.setup_quick.callback(cog, interaction, template="hardcore")

    assert "afraid not" in reply_text(interaction).lower()
    row = await bot.db.execute_fetchall("SELECT * FROM guilds WHERE guild_id = ?", (guild.id,))
    assert row == []  # declined before anything was saved


async def test_admin_setup_quick_rejects_an_unknown_feature(cog, bot, guild):
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin setup-quick")

    await AdminCog.setup_quick.callback(cog, interaction, features="teams,mind-reading")

    assert "afraid not" in reply_text(interaction).lower()
    row = await bot.db.execute_fetchall("SELECT * FROM guilds WHERE guild_id = ?", (guild.id,))
    assert row == []


async def test_admin_setup_quick_running_twice_with_the_same_template_creates_no_duplicates(
    cog, bot, guild
):
    admin = _admin(guild)

    await AdminCog.setup_quick.callback(
        cog,
        make_interaction(bot, guild=guild, user=admin, command_name="admin setup-quick"),
        template="milsim",
    )
    roles_after_first = sorted(r.name for r in guild.roles)

    await AdminCog.setup_quick.callback(
        cog,
        make_interaction(bot, guild=guild, user=admin, command_name="admin setup-quick"),
        template="milsim",
    )

    assert sorted(r.name for r in guild.roles) == roles_after_first


async def test_admin_setup_quick_with_no_template_can_still_create_the_default_ladder(
    cog, bot, guild
):
    """create_default_ladder is the one /setup-quick capability that isn't a
    direct template/feature passthrough — worth its own case so folding
    /setup quick into /admin doesn't quietly drop it."""
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin setup-quick")

    await AdminCog.setup_quick.callback(cog, interaction, create_default_ladder=True)

    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert {r["name"] for r in rows} == {name for _, name in DEFAULT_LADDER}


async def test_admin_setup_quick_is_refused_for_a_non_admin(cog, bot, guild):
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin setup-quick")

    await AdminCog.setup_quick.callback(cog, interaction, template="vanilla")

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()
    row = await bot.db.execute_fetchall("SELECT * FROM guilds WHERE guild_id = ?", (guild.id,))
    assert row == []


# --------------------------------------------------------------------------- #
# /admin teardown — moved from the old flat /teardown; confirm modal must    #
# keep working exactly as before, it's a deliberate safety property         #
# --------------------------------------------------------------------------- #


async def test_admin_teardown_opens_the_confirm_view_for_an_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin teardown")

    await AdminCog.teardown.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, TeardownConfirmView)
    assert guild.name in reply_text(interaction)


async def test_admin_teardown_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="admin teardown")

    await AdminCog.teardown.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    assert "admin" in reply_text(interaction).lower()
    assert interaction.response.messages[-1]["view"] is None


async def test_admin_teardown_confirm_flow_removes_bot_created_ranks_and_clears_config(
    cog, bot, guild
):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    created = await _create_default_ladder(bot, guild)
    open_interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin teardown")

    await AdminCog.teardown.callback(cog, open_interaction)
    view = open_interaction.response.messages[-1]["view"]
    message = open_interaction.response.message

    proceed_click = make_interaction(bot, guild=guild, user=admin, message=message)
    await TeardownConfirmView.proceed(view, proceed_click, None)
    modal = proceed_click.response.modal
    assert isinstance(modal, TeardownConfirmModal)
    modal.guild_name_input._value = guild.name

    submit_interaction = make_interaction(bot, guild=guild, user=admin)
    await modal.on_submit(submit_interaction)

    assert "teardown complete" in reply_text(submit_interaction).lower()
    remaining_role_ids = {r.id for r in guild.roles}
    for role in created:
        assert role.id not in remaining_role_ids
    guild_rows = await bot.db.execute_fetchall(
        "SELECT * FROM guilds WHERE guild_id = ?", (guild.id,)
    )
    assert guild_rows == []
    rank_rows = await bot.db.execute_fetchall("SELECT * FROM ranks WHERE guild_id = ?", (guild.id,))
    assert rank_rows == []


async def test_admin_teardown_declines_on_a_mismatched_name_and_changes_nothing(bot, guild):
    await seed_guild(bot.db, guild.id)
    created = await _create_default_ladder(bot, guild)
    modal = TeardownConfirmModal(bot, guild)
    modal.guild_name_input._value = "Not The Right Name"

    submit_interaction = make_interaction(bot, guild=guild, user=_admin(guild))
    await modal.on_submit(submit_interaction)

    assert "cancelled" in reply_text(submit_interaction).lower()
    remaining_role_ids = {r.id for r in guild.roles}
    for role in created:
        assert role.id in remaining_role_ids
    guild_rows = await bot.db.execute_fetchall(
        "SELECT * FROM guilds WHERE guild_id = ?", (guild.id,)
    )
    assert len(guild_rows) == 1


# --------------------------------------------------------------------------- #
# shared error-handler path (cog_app_command_error -> view_util)              #
# --------------------------------------------------------------------------- #


async def test_error_handler_gives_a_generic_message_without_leaking_exception_text(
    cog, bot, guild
):
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin incidents")
    leaky = ValueError("column adjutant_secret_schema_v7 does not exist")

    await AdminCog.cog_app_command_error(cog, interaction, leaky)

    assert reply_ephemeral(interaction) is True
    text = reply_text(interaction)
    assert "adjutant_secret_schema_v7" not in text
    assert "column" not in text.lower()
    assert "something went wrong" in text.lower()


async def test_error_handler_gives_a_specific_message_for_forbidden(cog, bot, guild):
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin incidents")

    await AdminCog.cog_app_command_error(cog, interaction, forbidden("Missing Permissions"))

    assert reply_ephemeral(interaction) is True
    assert "authority" in reply_text(interaction).lower()
    assert "Missing Permissions" not in reply_text(interaction)


async def test_error_handler_is_a_noop_for_check_failures(cog, bot, guild):
    """CheckFailure means a check already messaged + logged the denial —
    the shared handler must not send a second, redundant reply."""
    admin = make_member(guild, display_name="Admin", is_admin=True)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="admin incidents")

    await AdminCog.cog_app_command_error(cog, interaction, CheckFailure("denied"))

    assert interaction.response.messages == []
    assert interaction.followup.messages == []
