"""ServerLink interface, NullLink degradation, snapshot subscription, the
cog's build_link factory, and the A2S/RCON wrapper logic (against fakes —
no real network, and no dependency on berconpy actually being installed).
"""

from __future__ import annotations

import sys
import types

import a2s
import pytest

from adjutant.cogs.serverlink import build_link
from adjutant.serverlink import (
    NotSupported,
    NullLink,
    PlayerInfo,
    ServerLink,
    ServerStatus,
    Snapshot,
)
from adjutant.serverlink.a2s_link import A2SLink
from adjutant.serverlink.feed import FeedLink, feed_host, feed_port
from adjutant.serverlink.null import NOT_LINKED_DETAIL
from adjutant.serverlink.rcon_link import RconLink

# -- base ServerLink: default capability degradation ------------------------


class _MinimalLink(ServerLink):
    """The smallest possible concrete ServerLink — only `status()` is
    required by the ABC. Every other method should fall back to the base
    class's NotSupported-raising default."""

    async def status(self) -> ServerStatus:
        return ServerStatus(name="x", scenario="", players=0, max_players=0, reachable=True)


async def test_serverlink_default_players_raises_notsupported():
    with pytest.raises(NotSupported):
        await _MinimalLink().players()


async def test_serverlink_default_kick_raises_notsupported():
    with pytest.raises(NotSupported):
        await _MinimalLink().kick("0")


async def test_serverlink_default_ban_raises_notsupported():
    with pytest.raises(NotSupported):
        await _MinimalLink().ban("0")


async def test_serverlink_default_restart_raises_notsupported():
    with pytest.raises(NotSupported):
        await _MinimalLink().restart()


async def test_serverlink_default_shutdown_raises_notsupported():
    with pytest.raises(NotSupported):
        await _MinimalLink().shutdown_server()


async def test_serverlink_default_open_and_close_are_noops():
    link = _MinimalLink()
    await link.open()
    await link.close()  # must not raise, and must be safe to call twice
    await link.close()


# -- snapshot subscription ---------------------------------------------------


async def test_on_snapshot_delivers_to_sync_and_async_subscribers():
    link = _MinimalLink()
    received = []

    def sync_cb(snap):
        received.append(("sync", snap))

    async def async_cb(snap):
        received.append(("async", snap))

    link.on_snapshot(sync_cb)
    link.on_snapshot(async_cb)

    snap = Snapshot(players=[], bases=[], timestamp=1.0)
    await link._emit_snapshot(snap)

    assert received == [("sync", snap), ("async", snap)]


async def test_remove_snapshot_listener_stops_delivery():
    link = _MinimalLink()
    received = []

    def cb(snap):
        received.append(snap)

    link.on_snapshot(cb)
    link.remove_snapshot_listener(cb)
    await link._emit_snapshot(Snapshot(players=[], bases=[], timestamp=1.0))
    assert received == []


def test_remove_snapshot_listener_is_a_noop_for_unknown_callback():
    _MinimalLink().remove_snapshot_listener(lambda snap: None)  # must not raise


# -- NullLink: the always-on default ----------------------------------------


async def test_nulllink_status_is_always_unreachable_with_courteous_detail():
    status = await NullLink().status()
    assert status.reachable is False
    assert status.detail == NOT_LINKED_DETAIL


def test_nulllink_has_no_capabilities():
    link = NullLink()
    assert link.can_players is False
    assert link.can_admin is False
    assert link.can_live_state is False


# -- cog's build_link factory -------------------------------------------------


def test_build_link_null_backend_returns_nulllink():
    assert isinstance(build_link("null", None, None, None), NullLink)


def test_build_link_a2s_without_host_falls_back_to_null():
    assert isinstance(build_link("a2s", None, None, None), NullLink)


def test_build_link_a2s_with_host_returns_a2slink_with_default_port():
    link = build_link("a2s", "1.2.3.4", None, None)
    assert isinstance(link, A2SLink)
    assert (link.host, link.port) == ("1.2.3.4", 17777)


def test_build_link_a2s_with_explicit_port_overrides_default():
    link = build_link("a2s", "1.2.3.4", 27777, None)
    assert link.port == 27777


def test_build_link_rcon_without_secret_falls_back_to_null():
    assert isinstance(build_link("rcon", "1.2.3.4", 19999, None), NullLink)


def test_build_link_rcon_without_host_falls_back_to_null():
    assert isinstance(build_link("rcon", None, 19999, "pw"), NullLink)


def test_build_link_rcon_with_host_and_secret_returns_rconlink_with_default_port():
    link = build_link("rcon", "1.2.3.4", None, "pw")
    assert isinstance(link, RconLink)
    assert (link.host, link.port, link.password) == ("1.2.3.4", 19999, "pw")


def test_build_link_feed_without_secret_falls_back_to_null():
    assert isinstance(build_link("feed", None, None, None), NullLink)


def test_build_link_feed_with_secret_returns_feedlink():
    assert isinstance(build_link("feed", None, None, "some-token"), FeedLink)


def test_build_link_unknown_backend_falls_back_to_null():
    assert isinstance(build_link("carrier-pigeon", "1.2.3.4", 1, "x"), NullLink)


# -- A2SLink: status() against a2s.ainfo -------------------------------------


class _FakeSourceInfo:
    server_name = "Test Server"
    map_name = "Everon"
    player_count = 3
    max_players = 16


async def test_a2slink_status_reachable_on_success(monkeypatch):
    async def fake_ainfo(address, timeout=5.0):
        return _FakeSourceInfo()

    monkeypatch.setattr(a2s, "ainfo", fake_ainfo)
    status = await A2SLink("1.2.3.4", 17777).status()
    assert status.reachable is True
    assert (status.name, status.scenario, status.players, status.max_players) == (
        "Test Server",
        "Everon",
        3,
        16,
    )


async def test_a2slink_status_unreachable_on_timeout(monkeypatch):
    async def fake_ainfo(address, timeout=5.0):
        raise TimeoutError()

    monkeypatch.setattr(a2s, "ainfo", fake_ainfo)
    status = await A2SLink("1.2.3.4", 17777).status()
    assert status.reachable is False
    assert status.detail


async def test_a2slink_status_unreachable_on_connection_refused(monkeypatch):
    async def fake_ainfo(address, timeout=5.0):
        raise ConnectionRefusedError()

    monkeypatch.setattr(a2s, "ainfo", fake_ainfo)
    status = await A2SLink("1.2.3.4", 17777).status()
    assert status.reachable is False


# -- RconLink: wrapper logic against a fake berconpy -------------------------


class _FakeConnectCtx:
    def __init__(self, client, fail: bool = False):
        self._client = client
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            raise RuntimeError("login refused")
        self._client._connected = True
        return self._client

    async def __aexit__(self, *exc_info):
        self._client._connected = False
        return False


class _FakeRCONClient:
    """Stands in for berconpy.RCONClient: same connect()/send_command()/
    is_connected() surface, driven entirely by an in-memory response map."""

    def __init__(self, *, fail_connect: bool = False):
        self._connected = False
        self._fail_connect = fail_connect
        self.responses: dict[str, str] = {}
        self.sent_commands: list[str] = []

    def connect(self, ip, port, password):
        return _FakeConnectCtx(self, fail=self._fail_connect)

    def is_connected(self) -> bool:
        return self._connected

    async def send_command(self, command: str) -> str:
        self.sent_commands.append(command)
        return self.responses.get(command, "")


def _install_fake_berconpy(monkeypatch, *, fail_connect: bool = False):
    """Registers a fake `berconpy` module in sys.modules for the duration of
    the test and returns the single client instance rcon_link will create."""
    client = _FakeRCONClient(fail_connect=fail_connect)
    fake_module = types.ModuleType("berconpy")
    fake_module.RCONClient = lambda: client
    monkeypatch.setitem(sys.modules, "berconpy", fake_module)
    return client


async def test_rconlink_open_without_berconpy_installed_degrades_gracefully(monkeypatch):
    # berconpy is genuinely not installed in this project's environment —
    # exercises the real "dependency missing" path, not a fake.
    monkeypatch.delitem(sys.modules, "berconpy", raising=False)
    link = RconLink("1.2.3.4", 19999, "pw")
    with pytest.raises(NotSupported):
        await link.open()
    assert link.is_connected() is False
    status = await link.status()
    assert status.reachable is False


async def test_rconlink_open_connects_and_players_parses_response(monkeypatch):
    client = _install_fake_berconpy(monkeypatch)
    link = RconLink("1.2.3.4", 19999, "pw")
    await link.open()
    try:
        assert link.is_connected() is True
        client.responses["#players"] = "0;abc123;Alice\n1;def456;Bob"
        roster = await link.players()
        assert [p.name for p in roster] == ["Alice", "Bob"]
    finally:
        await link.close()


async def test_rconlink_open_failure_leaves_link_disconnected_not_raising(monkeypatch):
    _install_fake_berconpy(monkeypatch, fail_connect=True)
    link = RconLink("1.2.3.4", 19999, "wrong-password")
    await link.open()  # login failure is caught internally, not raised
    assert link.is_connected() is False
    status = await link.status()
    assert status.reachable is False


async def test_rconlink_players_raises_notsupported_when_not_connected():
    link = RconLink("1.2.3.4", 19999, "pw")
    with pytest.raises(NotSupported):
        await link.players()


async def test_rconlink_close_sends_logout_command(monkeypatch):
    client = _install_fake_berconpy(monkeypatch)
    link = RconLink("1.2.3.4", 19999, "pw")
    await link.open()
    await link.close()
    assert "@logout" in client.sent_commands
    assert link.is_connected() is False


async def test_rconlink_kick_sends_kick_command(monkeypatch):
    client = _install_fake_berconpy(monkeypatch)
    link = RconLink("1.2.3.4", 19999, "pw")
    await link.open()
    try:
        await link.kick("3")
        assert "#kick 3" in client.sent_commands
    finally:
        await link.close()


async def test_rconlink_kick_includes_reason_when_given(monkeypatch):
    client = _install_fake_berconpy(monkeypatch)
    link = RconLink("1.2.3.4", 19999, "pw")
    await link.open()
    try:
        await link.kick("3", "teamkilling")
        assert "#kick 3 teamkilling" in client.sent_commands
    finally:
        await link.close()


async def test_rconlink_downgrades_can_admin_on_permission_denial_response(monkeypatch):
    client = _install_fake_berconpy(monkeypatch)
    link = RconLink("1.2.3.4", 19999, "pw")
    await link.open()
    try:
        assert link.can_admin is True
        client.responses["#restart"] = "You do not have permission to execute that command."
        with pytest.raises(NotSupported):
            await link.restart()
        assert link.can_admin is False

        # A second admin call shouldn't even hit the wire once downgraded.
        sent_before = len(client.sent_commands)
        with pytest.raises(NotSupported):
            await link.kick("0")
        assert len(client.sent_commands) == sent_before
    finally:
        await link.close()


async def test_rconlink_restart_and_shutdown_send_expected_commands(monkeypatch):
    client = _install_fake_berconpy(monkeypatch)
    link = RconLink("1.2.3.4", 19999, "pw")
    await link.open()
    try:
        await link.restart()
        await link.shutdown_server()
        assert "#restart" in client.sent_commands
        assert "#shutdown" in client.sent_commands
    finally:
        await link.close()


# -- feed_host / feed_port: env parsing --------------------------------------


def test_feed_host_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("FEED_HOST", raising=False)
    assert feed_host() == "127.0.0.1"


def test_feed_host_reads_env_override(monkeypatch):
    monkeypatch.setenv("FEED_HOST", "0.0.0.0")
    assert feed_host() == "0.0.0.0"


def test_feed_port_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("FEED_PORT", raising=False)
    assert feed_port() == 8390


def test_feed_port_reads_env_override(monkeypatch):
    monkeypatch.setenv("FEED_PORT", "9001")
    assert feed_port() == 9001


def test_feed_port_falls_back_to_default_on_non_numeric_env(monkeypatch):
    monkeypatch.setenv("FEED_PORT", "not-a-port")
    assert feed_port() == 8390


# -- FeedLink: push-driven status/players/snapshot fan-out -------------------


def _snapshot(*, players=(), bases=(), timestamp=1.0) -> Snapshot:
    return Snapshot(players=list(players), bases=list(bases), timestamp=timestamp)


def test_feedlink_has_live_state_and_player_capability_but_no_admin():
    link = FeedLink()
    assert link.can_live_state is True
    assert link.can_players is True
    assert link.can_admin is False


async def test_feedlink_status_unreachable_before_any_ingest():
    status = await FeedLink().status()
    assert status.reachable is False
    assert status.detail


async def test_feedlink_status_reachable_after_recent_ingest():
    link = FeedLink()
    await link.ingest(_snapshot(players=[PlayerInfo(name="Alice", player_id="a")]))
    status = await link.status()
    assert status.reachable is True
    assert status.players == 1


async def test_feedlink_status_goes_unreachable_once_stale(monkeypatch):
    link = FeedLink()
    await link.ingest(_snapshot())
    # Simulate the clock moving past the staleness window without waiting for real time.
    link._last_received_at -= FeedLink.STALE_AFTER_SECONDS + 1
    status = await link.status()
    assert status.reachable is False


async def test_feedlink_players_raises_notsupported_before_any_ingest():
    with pytest.raises(NotSupported):
        await FeedLink().players()


async def test_feedlink_players_returns_latest_snapshots_roster():
    link = FeedLink()
    roster = [PlayerInfo(name="Alice", player_id="a"), PlayerInfo(name="Bob", player_id="b")]
    await link.ingest(_snapshot(players=roster))
    assert [p.name for p in await link.players()] == ["Alice", "Bob"]


async def test_feedlink_ingest_fans_snapshot_out_to_subscribers():
    link = FeedLink()
    received = []
    link.on_snapshot(lambda snap: received.append(snap))
    snap = _snapshot(players=[PlayerInfo(name="Alice", player_id="a")])
    await link.ingest(snap)
    assert received == [snap]
