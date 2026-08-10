"""Inbound HTTPS listener for the PlayerTelemetry mod — Tier 3.

TLS termination is a reverse proxy's job (see docs/SERVER_INTEGRATION.md);
this speaks plain HTTP, bound to FEED_HOST/FEED_PORT (env, default
127.0.0.1:8390). The cog only starts this listener when at least one guild
is configured on the `feed` backend.

`parse_telemetry` is the pure, version-tolerant core: the mod's schema is
young (~2k downloads at time of writing) so every field is validated
defensively and a bad/missing field drops just that row, never the whole
snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Awaitable, Callable

from aiohttp import web

from .base import BaseInfo, NotSupported, PlayerInfo, ServerLink, ServerStatus, Snapshot

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8390
MAX_BODY_BYTES = 1_000_000  # ~1MB; PlayerTelemetry snapshots are small JSON
TELEMETRY_PATH = "/telemetry"

TokenLookup = Callable[[str], Awaitable[int | None]]
SnapshotHandler = Callable[[int, Snapshot], Awaitable[None]]


def feed_host() -> str:
    return os.environ.get("FEED_HOST", "").strip() or DEFAULT_HOST


def feed_port() -> int:
    raw = os.environ.get("FEED_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        log.warning("FEED_PORT=%r isn't a valid port, falling back to %s", raw, DEFAULT_PORT)
        return DEFAULT_PORT


def _coerce_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_float(value: object, default: float) -> float:
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_position(raw: object) -> tuple[float, float, float] | None:
    if not isinstance(raw, dict) or not all(k in raw for k in ("x", "y", "z")):
        return None
    try:
        return (float(raw["x"]), float(raw["y"]), float(raw["z"]))
    except (TypeError, ValueError):
        return None


def _parse_player(raw: object) -> PlayerInfo | None:
    if not isinstance(raw, dict):
        return None
    name = _coerce_str(raw.get("name"))
    if not name:
        return None
    uuid = _coerce_str(raw.get("uuid")) or None
    return PlayerInfo(
        name=name,
        player_id=uuid or name,
        uuid=uuid,
        faction=_coerce_str(raw.get("faction")) or None,
        position=_parse_position(raw.get("position")),
    )


def _parse_base(raw: object) -> BaseInfo | None:
    if not isinstance(raw, dict):
        return None
    name = _coerce_str(raw.get("name"))
    position = _parse_position(raw.get("position"))
    if not name or position is None:
        return None
    flags = raw.get("flags")
    return BaseInfo(
        name=name,
        faction=_coerce_str(raw.get("faction")) or None,
        position=position,
        flags=flags if isinstance(flags, dict) else {},
    )


def parse_telemetry(payload: object) -> Snapshot:
    """Normalize a PlayerTelemetry snapshot POST body into a `Snapshot`.

    Version-tolerant: unknown extra fields are ignored, missing/malformed
    fields drop just the affected player/base row, and a payload that isn't
    even a dict (or has no players/bases lists) yields an empty snapshot
    rather than raising.
    """
    if not isinstance(payload, dict):
        return Snapshot(players=[], bases=[], timestamp=time.time())

    raw_players = payload.get("players")
    players: list[PlayerInfo] = []
    if isinstance(raw_players, list):
        players = [p for p in (_parse_player(r) for r in raw_players) if p is not None]

    raw_bases = payload.get("bases")
    bases: list[BaseInfo] = []
    if isinstance(raw_bases, list):
        bases = [b for b in (_parse_base(r) for r in raw_bases) if b is not None]

    timestamp = _coerce_float(payload.get("timestamp"), default=time.time())

    return Snapshot(players=players, bases=bases, timestamp=timestamp)


def create_feed_app(token_lookup: TokenLookup, on_snapshot: SnapshotHandler) -> web.Application:
    """Build the aiohttp app for the telemetry listener.

    `token_lookup(token) -> guild_id | None` resolves the bearer token from
    `server_links.secret` to a guild; `on_snapshot(guild_id, Snapshot)` is
    awaited with the normalized snapshot for that guild.
    """
    app = web.Application(client_max_size=MAX_BODY_BYTES)

    async def handle_snapshot(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": "missing bearer token"}, status=401)
        token = auth[len("Bearer "):].strip()
        if not token:
            return web.json_response({"error": "missing bearer token"}, status=401)

        guild_id = await token_lookup(token)
        if guild_id is None:
            return web.json_response({"error": "invalid token"}, status=401)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "malformed json"}, status=400)
        # web.HTTPRequestEntityTooLarge from exceeding client_max_size
        # propagates as-is; aiohttp turns it into a 413 response.

        if not isinstance(payload, dict):
            return web.json_response({"error": "expected a JSON object"}, status=400)

        snapshot = parse_telemetry(payload)
        await on_snapshot(guild_id, snapshot)
        return web.json_response({"status": "ok"})

    app.router.add_post(TELEMETRY_PATH, handle_snapshot)
    return app


class FeedLink(ServerLink):
    """Tier 3 — live state arrives as an HTTPS push from the PlayerTelemetry
    mod rather than being polled, so this link has no connection of its own
    to open/close. The cog's aiohttp handler calls `ingest()` with each
    normalized snapshot for this guild; `status()`/`players()` are derived
    from whatever arrived most recently, and `_emit_snapshot` fans it out to
    subscribers (the map cog) exactly like any other backend would.
    """

    can_players = True  # PlayerInfo rows are already on hand from the last snapshot
    can_admin = False  # the mod is a one-way push; there's no command channel back
    can_live_state = True

    # The mod posts roughly every 5s by default; treat a longer silence as
    # the server (or the mod) having gone away rather than trusting stale data.
    STALE_AFTER_SECONDS = 30.0

    def __init__(self) -> None:
        super().__init__()
        self._last_snapshot: Snapshot | None = None
        self._last_received_at: float = 0.0

    async def ingest(self, snapshot: Snapshot) -> None:
        """Called by the feed listener when a POST for this guild arrives."""
        self._last_snapshot = snapshot
        self._last_received_at = time.time()
        await self._emit_snapshot(snapshot)

    def _is_stale(self) -> bool:
        if self._last_snapshot is None:
            return True
        return (time.time() - self._last_received_at) > self.STALE_AFTER_SECONDS

    async def status(self) -> ServerStatus:
        if self._is_stale():
            return ServerStatus(
                name="", scenario="", players=0, max_players=0,
                reachable=False,
                detail="No telemetry received recently — check the PlayerTelemetry mod is "
                "running and still pointed at this bot's feed endpoint.",
            )
        assert self._last_snapshot is not None
        return ServerStatus(
            name="",
            scenario="",
            players=len(self._last_snapshot.players),
            max_players=0,
            reachable=True,
            detail="Live feed — figures reflect the most recent telemetry push, not a live query.",
        )

    async def players(self) -> list[PlayerInfo]:
        if self._is_stale():
            raise NotSupported("No recent telemetry to list players from.")
        assert self._last_snapshot is not None
        return list(self._last_snapshot.players)
