"""In-process integration tests for adjutant/cogs/hub.py: /adjutant opens a
panel routing to Config, Server Link, and Incidents; Setup/Ranks/
Diagnostics are placeholders. Every button re-checks its own gating at
click time — this is the discoverability surface, so it's reached by
whoever is curious, not just admins, and must decline politely rather
than assume the panel's invoker is still privileged.
"""
from __future__ import annotations

import pytest

from adjutant.cogs.admin import AdminCog, log_incident
from adjutant.cogs.config import ConfigCog, ConfigPanelView
from adjutant.cogs.hub import HubCog, HubView
from adjutant.cogs.serverlink import ServerLinkCog, ServerPanelView
from fakes import (
    FakeGuild,
    make_interaction,
    make_member,
    make_role,
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
    return HubCog(bot)


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


async def _open_hub(cog, bot, guild, user):
    interaction = make_interaction(bot, guild=guild, user=user, command_name="adjutant")
    await HubCog.adjutant.callback(cog, interaction)
    view = interaction.response.messages[-1]["view"]
    return view, interaction.response.message


# --------------------------------------------------------------------------- #
# /adjutant — opens the panel                                                 #
# --------------------------------------------------------------------------- #

async def test_adjutant_reply_is_ephemeral_and_names_the_quick_commands(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    interaction = make_interaction(bot, guild=guild, user=member, command_name="adjutant")

    await HubCog.adjutant.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    text = reply_text(interaction)
    for cmd in ("/team", "/event", "/map", "/rank", "/server"):
        assert cmd in text


async def test_panel_is_locked_to_whoever_opened_it(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    view, message = await _open_hub(cog, bot, guild, member)
    bystander = make_member(guild, display_name="Bystander")

    click = make_interaction(bot, guild=guild, user=bystander, message=message)
    allowed = await view.interaction_check(click)

    assert allowed is False
    assert reply_ephemeral(click) is True


# --------------------------------------------------------------------------- #
# Config button                                                               #
# --------------------------------------------------------------------------- #

async def test_config_button_opens_the_config_panel_for_an_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    bot.register_cog(ConfigCog(bot))
    admin = _admin(guild)
    view, message = await _open_hub(cog, bot, guild, admin)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await HubView.config_button(view, click, None)

    panel_view = click.response.messages[-1]["view"]
    assert isinstance(panel_view, ConfigPanelView)


async def test_config_button_declines_a_non_admin_without_touching_config(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    bot.register_cog(ConfigCog(bot))
    grunt = make_member(guild, display_name="Grunt")
    view, message = await _open_hub(cog, bot, guild, grunt)

    click = make_interaction(bot, guild=guild, user=grunt, message=message)
    await HubView.config_button(view, click, None)

    assert reply_ephemeral(click) is True
    assert "admin" in reply_text(click).lower()
    assert click.response.messages[-1]["view"] is None
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_config_button_degrades_politely_when_config_cog_is_not_loaded(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    view, message = await _open_hub(cog, bot, guild, admin)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await HubView.config_button(view, click, None)

    assert reply_ephemeral(click) is True
    assert "isn't loaded" in reply_text(click).lower()


# --------------------------------------------------------------------------- #
# Server Link button                                                          #
# --------------------------------------------------------------------------- #

async def test_server_button_shows_the_server_status_panel(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    bot.register_cog(ServerLinkCog(bot))
    member = make_member(guild, display_name="Anyone")
    view, message = await _open_hub(cog, bot, guild, member)

    click = make_interaction(bot, guild=guild, user=member, message=message)
    await HubView.server_button(view, click, None)

    server_view = click.response.messages[-1]["view"]
    assert isinstance(server_view, ServerPanelView)


async def test_server_button_degrades_politely_when_serverlink_cog_is_not_loaded(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    view, message = await _open_hub(cog, bot, guild, member)

    click = make_interaction(bot, guild=guild, user=member, message=message)
    await HubView.server_button(view, click, None)

    assert reply_ephemeral(click) is True
    assert "isn't loaded" in reply_text(click).lower()


# --------------------------------------------------------------------------- #
# Incidents button                                                            #
# --------------------------------------------------------------------------- #

async def test_incidents_button_shows_the_ledger_for_an_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    bot.register_cog(AdminCog(bot))
    admin = _admin(guild)
    await log_incident(bot, guild.id, 424242, "permission_denied", detail="team create")
    view, message = await _open_hub(cog, bot, guild, admin)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await HubView.incidents_button(view, click, None)

    embed = click.response.messages[-1]["embed"]
    assert "424242" in embed.description


async def test_incidents_button_declines_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    bot.register_cog(AdminCog(bot))
    grunt = make_member(guild, display_name="Grunt")
    view, message = await _open_hub(cog, bot, guild, grunt)

    click = make_interaction(bot, guild=guild, user=grunt, message=message)
    await HubView.incidents_button(view, click, None)

    assert reply_ephemeral(click) is True
    assert "admin" in reply_text(click).lower()


# --------------------------------------------------------------------------- #
# Setup / Ranks / Diagnostics — wired through to setup.py                     #
# --------------------------------------------------------------------------- #

async def test_setup_button_opens_the_setup_wizard_for_an_admin(cog, bot, guild):
    from adjutant.cogs.setup import SetupView

    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    view, message = await _open_hub(cog, bot, guild, admin)
    click = make_interaction(bot, guild=guild, user=admin, message=message)

    await HubView.setup_button(view, click, None)

    assert reply_ephemeral(click) is True
    assert isinstance(click.response.messages[-1]["view"], SetupView)


async def test_ranks_button_shows_the_current_ladder_for_an_admin(cog, bot, guild):
    from adjutant.cogs.setup import RankLadderView

    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    role = make_role("Sergeant")
    guild.roles.append(role)
    await bot.db.execute(
        "INSERT INTO ranks (guild_id, role_id, position, name) VALUES (?, ?, 0, 'Sergeant')",
        (guild.id, role.id),
    )
    await bot.db.commit()
    view, message = await _open_hub(cog, bot, guild, admin)
    click = make_interaction(bot, guild=guild, user=admin, message=message)

    await HubView.ranks_button(view, click, None)

    assert isinstance(click.response.messages[-1]["view"], RankLadderView)
    assert "Sergeant" in click.response.messages[-1]["embed"].description


async def test_diagnostics_button_reports_the_preflight(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    view, message = await _open_hub(cog, bot, guild, admin)
    click = make_interaction(bot, guild=guild, user=admin, message=message)

    await HubView.diagnostics_button(view, click, None)

    embed = click.response.messages[-1]["embed"]
    assert "permission" in (embed.description or "").lower()
    # The preflight must never tell an owner to hand over the keys — the bot
    # is built to run without Administrator on purpose.
    assert "grant administrator" not in (embed.description or "").lower()


@pytest.mark.parametrize("button_name", ["setup_button", "ranks_button", "diagnostics_button"])
async def test_admin_only_buttons_decline_a_non_admin(cog, bot, guild, button_name):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    view, message = await _open_hub(cog, bot, guild, member)
    click = make_interaction(bot, guild=guild, user=member, message=message)

    await getattr(HubView, button_name)(view, click, None)

    assert reply_ephemeral(click) is True
    assert "admin" in reply_text(click).lower()
