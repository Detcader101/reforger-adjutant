"""parse_telemetry (pure) and the aiohttp feed listener (Tier 3)."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from adjutant.serverlink.base import BaseInfo, PlayerInfo, Snapshot
from adjutant.serverlink.feed import create_feed_app, parse_telemetry

# -- parse_telemetry: pure parsing -----------------------------------------


def test_parse_telemetry_parses_full_snapshot():
    payload = {
        "timestamp": 1000.5,
        "players": [
            {
                "name": "Alice",
                "uuid": "abc-123",
                "faction": "BLUFOR",
                "position": {"x": 100.0, "y": 5.0, "z": 200.0},
                "alive": True,
                "vehicle": None,
            }
        ],
        "bases": [
            {
                "name": "Camp Nowhere",
                "faction": "OPFOR",
                "position": {"x": 500.0, "y": 0.0, "z": 300.0},
                "flags": {"capturable": True},
            }
        ],
    }
    snapshot = parse_telemetry(payload)
    assert snapshot.timestamp == 1000.5
    assert snapshot.players == [
        PlayerInfo(name="Alice", player_id="abc-123", uuid="abc-123", faction="BLUFOR", position=(100.0, 5.0, 200.0))
    ]
    assert snapshot.bases == [
        BaseInfo(name="Camp Nowhere", faction="OPFOR", position=(500.0, 0.0, 300.0), flags={"capturable": True})
    ]


def test_parse_telemetry_handles_empty_lists():
    snapshot = parse_telemetry({"players": [], "bases": [], "timestamp": 1.0})
    assert snapshot.players == []
    assert snapshot.bases == []


def test_parse_telemetry_handles_missing_players_and_bases_keys():
    snapshot = parse_telemetry({})
    assert snapshot.players == []
    assert snapshot.bases == []
    assert isinstance(snapshot.timestamp, float)


def test_parse_telemetry_drops_player_missing_name_but_keeps_others():
    payload = {"players": [{"uuid": "x"}, {"name": "Bob", "uuid": "y"}]}
    snapshot = parse_telemetry(payload)
    assert [p.name for p in snapshot.players] == ["Bob"]


def test_parse_telemetry_drops_base_missing_position():
    payload = {"bases": [{"name": "No Coords"}, {"name": "Has Coords", "position": {"x": 1, "y": 2, "z": 3}}]}
    snapshot = parse_telemetry(payload)
    assert [b.name for b in snapshot.bases] == ["Has Coords"]


def test_parse_telemetry_ignores_unknown_extra_fields():
    payload = {
        "players": [{"name": "Alice", "future_field": "whatever"}],
        "server_version": "99.9.9",
        "totally_new_top_level_key": {"nested": True},
    }
    snapshot = parse_telemetry(payload)
    assert snapshot.players[0].name == "Alice"


def test_parse_telemetry_tolerates_wrong_types_for_players_and_bases():
    payload = {"players": "not a list", "bases": {"also": "not a list"}}
    snapshot = parse_telemetry(payload)
    assert snapshot.players == []
    assert snapshot.bases == []


def test_parse_telemetry_tolerates_wrong_type_position():
    payload = {"players": [{"name": "Alice", "position": "not a dict"}]}
    snapshot = parse_telemetry(payload)
    assert snapshot.players[0].position is None


def test_parse_telemetry_tolerates_non_numeric_position_values():
    payload = {"players": [{"name": "Alice", "position": {"x": "nope", "y": 1, "z": 1}}]}
    snapshot = parse_telemetry(payload)
    assert snapshot.players[0].position is None


def test_parse_telemetry_handles_non_dict_payload():
    snapshot = parse_telemetry(["not", "a", "dict"])
    assert snapshot.players == []
    assert snapshot.bases == []


# -- aiohttp feed listener --------------------------------------------------
#
# No pytest-aiohttp fixtures in this project's dependency set, so tests
# drive aiohttp.test_utils.TestServer/TestClient directly against an app
# built by create_feed_app(), per docs/SERVER_INTEGRATION.md's guidance.

GUILD_ID = 42
VALID_TOKEN = "secret-token"


@pytest.fixture
def snapshots_received():
    """(list of (guild_id, Snapshot) received, the on_snapshot callback to pass in)."""
    received: list[tuple[int, Snapshot]] = []

    async def on_snapshot(guild_id, snapshot):
        received.append((guild_id, snapshot))

    return received, on_snapshot


async def _lookup(token: str) -> int | None:
    return GUILD_ID if token == VALID_TOKEN else None


class _FeedClient:
    """Async context manager wrapping TestServer+TestClient lifecycle for
    an app built from `create_feed_app(_lookup, on_snapshot)`."""

    def __init__(self, on_snapshot):
        self._app = create_feed_app(_lookup, on_snapshot)
        self._server = TestServer(self._app)
        self._client = TestClient(self._server)

    async def __aenter__(self) -> TestClient:
        await self._client.start_server()
        return self._client

    async def __aexit__(self, *exc_info) -> None:
        await self._client.close()


async def test_feed_accepts_valid_token_and_dispatches_snapshot(snapshots_received):
    received, on_snapshot = snapshots_received
    async with _FeedClient(on_snapshot) as client:
        resp = await client.post(
            "/telemetry",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            data=json.dumps({"players": [{"name": "Alice"}], "bases": []}),
        )
        assert resp.status == 200

    assert len(received) == 1
    guild_id, snapshot = received[0]
    assert guild_id == GUILD_ID
    assert snapshot.players[0].name == "Alice"


async def test_feed_rejects_missing_authorization_header(snapshots_received):
    received, on_snapshot = snapshots_received
    async with _FeedClient(on_snapshot) as client:
        resp = await client.post("/telemetry", data=json.dumps({}))
        assert resp.status == 401
    assert received == []


async def test_feed_rejects_wrong_token(snapshots_received):
    received, on_snapshot = snapshots_received
    async with _FeedClient(on_snapshot) as client:
        resp = await client.post(
            "/telemetry",
            headers={"Authorization": "Bearer wrong-token"},
            data=json.dumps({}),
        )
        assert resp.status == 401
    assert received == []


async def test_feed_rejects_malformed_json(snapshots_received):
    received, on_snapshot = snapshots_received
    async with _FeedClient(on_snapshot) as client:
        resp = await client.post(
            "/telemetry",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            data="{not valid json",
        )
        assert resp.status == 400
    assert received == []


async def test_feed_rejects_json_that_isnt_an_object(snapshots_received):
    received, on_snapshot = snapshots_received
    async with _FeedClient(on_snapshot) as client:
        resp = await client.post(
            "/telemetry",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            data=json.dumps(["not", "an", "object"]),
        )
        assert resp.status == 400
    assert received == []


async def test_feed_rejects_oversized_payload(snapshots_received):
    received, on_snapshot = snapshots_received
    huge_name = "x" * 2_000_000  # well past the 1MB cap
    async with _FeedClient(on_snapshot) as client:
        resp = await client.post(
            "/telemetry",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            data=json.dumps({"players": [{"name": huge_name}]}),
        )
        assert resp.status == 413
    assert received == []
