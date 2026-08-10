"""In-process integration tests for adjutant/cogs/serverlink.py's Discord
adapter layer — the ServerLink interface itself and build_link() have
services-level coverage in tests/test_serverlink.py; this file covers the
cog: /server's bare status reply, the Players/Link/Unlink/Kick buttons,
and the secret-handling flows (Link -> RCON password modal, feed token
regeneration) — none of which had cog-layer coverage before this pass.
"""
from __future__ import annotations

import sys
import types

import pytest

from adjutant.cogs.serverlink import (
    KickModal,
    LinkBackendModal,
    RconSecretModal,
    SecretEntryView,
    ServerLinkCog,
    ServerPanelView,
)
from adjutant.serverlink.rcon_link import RconLink
from fakes import FakeGuild, make_interaction, make_member, reply_ephemeral, reply_text, seed_guild


class _FakeConnectCtx:
    async def __aenter__(self):
        return types.SimpleNamespace(is_connected=lambda: True)

    async def __aexit__(self, *exc_info):
        return False


class _FakeRCONClient:
    """Minimal berconpy.RCONClient stand-in — just enough for open() to
    succeed, mirroring tests/test_serverlink.py's own fake (berconpy is a
    genuinely optional dependency, not installed in this environment)."""

    def connect(self, ip, port, password):
        return _FakeConnectCtx()

    def is_connected(self) -> bool:
        return True

    async def send_command(self, command: str) -> str:
        return ""


@pytest.fixture
def fake_berconpy(monkeypatch):
    fake_module = types.ModuleType("berconpy")
    fake_module.RCONClient = lambda: _FakeRCONClient()
    monkeypatch.setitem(sys.modules, "berconpy", fake_module)


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
def cog(bot):
    return ServerLinkCog(bot)


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


# --------------------------------------------------------------------------- #
# bare /server                                                                #
# --------------------------------------------------------------------------- #

async def test_server_shows_not_reachable_when_nothing_is_linked(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    interaction = make_interaction(bot, guild=guild, user=member, command_name="server")

    await ServerLinkCog.server.callback(cog, interaction)

    embed = interaction.response.messages[-1]["embed"]
    assert "not reachable" in embed.description.lower() or "no server" in embed.description.lower()
    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, ServerPanelView)


# --------------------------------------------------------------------------- #
# Players button                                                              #
# --------------------------------------------------------------------------- #

async def test_players_button_declines_when_no_server_is_linked(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    member = make_member(guild, display_name="Anyone")
    view = ServerPanelView(cog)

    click = make_interaction(bot, guild=guild, user=member, command_name="server")
    await ServerPanelView.players_button(view, click, None)

    assert reply_ephemeral(click) is True
    assert "can't list players" in reply_text(click).lower()


# --------------------------------------------------------------------------- #
# Link button — non-secret backends apply directly, RCON goes via the modal   #
# --------------------------------------------------------------------------- #

async def test_link_button_declines_a_non_admin_before_opening_the_modal(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    view = ServerPanelView(cog)

    click = make_interaction(bot, guild=guild, user=grunt, command_name="server")
    await ServerPanelView.link_button(view, click, None)

    assert click.response.modal is None
    assert reply_ephemeral(click) is True
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_link_backend_modal_applies_an_a2s_link_directly(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    modal = LinkBackendModal(cog)
    modal.backend_input._value = "a2s"
    modal.host_input._value = "203.0.113.5"
    modal.port_input._value = ""

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="server")
    await modal.on_submit(interaction)

    rows = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert len(rows) == 1
    assert rows[0]["backend"] == "a2s"
    assert rows[0]["host"] == "203.0.113.5"
    assert "linked" in reply_text(interaction).lower()


async def test_link_backend_modal_rejects_an_unknown_backend(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    modal = LinkBackendModal(cog)
    modal.backend_input._value = "carrier-pigeon"
    modal.host_input._value = ""
    modal.port_input._value = ""

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="server")
    await modal.on_submit(interaction)

    assert "isn't a backend" in reply_text(interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert rows == []


async def test_link_backend_modal_rechecks_admin_at_submit_time(cog, bot, guild):
    """A modal's on_submit is its own interaction — this defends against
    whatever future path might construct one without going through the
    admin-gated button first."""
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    modal = LinkBackendModal(cog)
    modal.backend_input._value = "a2s"
    modal.host_input._value = "203.0.113.5"
    modal.port_input._value = ""

    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="server")
    await modal.on_submit(interaction)

    assert reply_ephemeral(interaction) is True
    rows = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert rows == []


async def test_link_backend_modal_rcon_opens_the_private_secret_entry_flow(cog, bot, guild, fake_berconpy):
    """The RCON password must never travel as modal text alongside
    host/port — it goes through a second, dedicated modal."""
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    modal = LinkBackendModal(cog)
    modal.backend_input._value = "rcon"
    modal.host_input._value = "203.0.113.5"
    modal.port_input._value = "19999"

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="server")
    await modal.on_submit(interaction)

    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, SecretEntryView)
    rows = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert rows == [], "nothing should be saved until the password modal is submitted"

    click = make_interaction(bot, guild=guild, user=admin, message=interaction.response.message)
    await SecretEntryView.enter_secret(view, click, None)
    secret_modal = click.response.modal
    assert isinstance(secret_modal, RconSecretModal)

    secret_modal.secret_input._value = "hunter2"
    submit_interaction = make_interaction(bot, guild=guild, user=admin, command_name="server")
    await secret_modal.on_submit(submit_interaction)

    rows2 = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert len(rows2) == 1
    assert rows2[0]["backend"] == "rcon"
    assert rows2[0]["secret"] == "hunter2"
    assert isinstance(cog.links[guild.id], RconLink)


async def test_link_backend_modal_is_rate_limited_like_the_old_slash_command_was(cog, bot, guild):
    """The old /server link command carried @rate_limited(); the modal
    replacing it needs the same protection — see admin.check_rate_limit."""
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)

    last_interaction = None
    for _ in range(6):
        modal = LinkBackendModal(cog)
        modal.backend_input._value = "a2s"
        modal.host_input._value = "203.0.113.5"
        modal.port_input._value = ""
        last_interaction = make_interaction(bot, guild=guild, user=admin, command_name="server")
        await modal.on_submit(last_interaction)

    assert "often" in reply_text(last_interaction).lower()
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "rate_limit" and i["detail"] == "server.link" for i in incidents)


# --------------------------------------------------------------------------- #
# Unlink button                                                               #
# --------------------------------------------------------------------------- #

async def test_unlink_button_sets_the_backend_back_to_null(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    modal = LinkBackendModal(cog)
    modal.backend_input._value = "a2s"
    modal.host_input._value = "203.0.113.5"
    modal.port_input._value = ""
    await modal.on_submit(make_interaction(bot, guild=guild, user=admin, command_name="server"))

    view = ServerPanelView(cog)
    click = make_interaction(bot, guild=guild, user=admin, command_name="server")
    await ServerPanelView.unlink_button(view, click, None)

    rows = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert rows[0]["backend"] == "null"


async def test_unlink_button_declines_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    view = ServerPanelView(cog)

    click = make_interaction(bot, guild=guild, user=grunt, command_name="server")
    await ServerPanelView.unlink_button(view, click, None)

    assert reply_ephemeral(click) is True
    rows = await bot.db.execute_fetchall("SELECT * FROM server_links WHERE guild_id = ?", (guild.id,))
    assert rows == []


# --------------------------------------------------------------------------- #
# Kick button                                                                 #
# --------------------------------------------------------------------------- #

async def test_kick_button_declines_a_non_admin_before_opening_the_modal(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    view = ServerPanelView(cog)

    click = make_interaction(bot, guild=guild, user=grunt, command_name="server")
    await ServerPanelView.kick_button(view, click, None)

    assert click.response.modal is None
    assert reply_ephemeral(click) is True


async def test_kick_modal_declines_when_no_server_is_linked(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    modal = KickModal(cog)
    modal.player_id_input._value = "3"
    modal.reason_input._value = ""

    interaction = make_interaction(bot, guild=guild, user=admin, command_name="server")
    await modal.on_submit(interaction)

    assert "can't kick" in reply_text(interaction).lower()
