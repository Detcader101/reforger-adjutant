"""Bot health HTTP endpoint.

Closes the operational gap where host-level monitoring catches "the box
is down" but not "the bot's Python process is alive but disconnected
from Discord" — a gateway crashloop, a stuck heartbeat, or a missing
DISCORD_TOKEN won't trip a host-level exporter.

Endpoints (all GET, no auth — this listener is bound to localhost by
default so only a local monitoring probe or an admin SSH'd in can reach
it):

  /healthz   — 200 OK when ready + gateway latency under 5s,
               503 SERVICE UNAVAILABLE otherwise. Body is a JSON
               object the human can read at a glance.

  /metrics   — Plain-text counters (guild count, teams, events, role
               grants). Intended for quick `curl` debugging more than
               Prometheus scraping; we deliberately don't ship a
               metrics-format library.

Disabled by default. Set BOT_HEALTH_PORT=<int> in the env to enable;
BOT_HEALTH_HOST overrides the bind address (defaults to 127.0.0.1 since
exposing the bot's internals to the LAN is unnecessary risk for a local
liveness probe).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


def _enabled_port() -> int | None:
    raw = os.environ.get("BOT_HEALTH_PORT")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "BOT_HEALTH_PORT=%r isn't an int — health endpoint disabled", raw,
        )
        return None


def _bind_host() -> str:
    return os.environ.get("BOT_HEALTH_HOST", "127.0.0.1")


def _build_status(bot: Any) -> dict:
    """Return the structured status used by /healthz. Separated so the
    metrics endpoint can reuse the same readiness signal."""
    is_ready = bool(bot.is_ready())
    latency_seconds = bot.latency  # discord.py exposes seconds-as-float
    healthy = is_ready and latency_seconds is not None and latency_seconds < 5.0
    return {
        "healthy": healthy,
        "ready": is_ready,
        "latency_ms": (
            round(latency_seconds * 1000, 1)
            if latency_seconds is not None and latency_seconds == latency_seconds
            else None
        ),
        "guilds": len(bot.guilds) if is_ready else None,
        "user": str(bot.user) if bot.user else None,
        "git_sha": os.environ.get("BOT_GIT_SHA"),
    }


async def _healthz(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    payload = _build_status(bot)
    status = 200 if payload["healthy"] else 503
    return web.json_response(payload, status=status)


async def _table_count(bot: Any, table: str) -> int:
    if bot.db is None:
        return 0
    try:
        async with bot.db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            row = await cur.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception:
        log.exception("metrics: count on %s failed", table)
        return 0


async def _metrics(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    status = _build_status(bot)

    # Small COUNT(*) queries against the bot's own shared connection.
    # This endpoint is for liveness spot-checks, not analytics, so we
    # keep it to a handful of cheap counts rather than anything heavier.
    guilds_total = await _table_count(bot, "guilds")
    teams_total = await _table_count(bot, "teams")
    events_total = await _table_count(bot, "events")
    role_grants_total = await _table_count(bot, "role_grants")

    lines = [
        f'adjutant_ready {1 if status["ready"] else 0}',
        f'adjutant_healthy {1 if status["healthy"] else 0}',
        f'adjutant_latency_ms {status["latency_ms"] or 0}',
        f'adjutant_guilds {status["guilds"] or 0}',
        f'adjutant_guilds_configured_total {guilds_total}',
        f'adjutant_teams_total {teams_total}',
        f'adjutant_events_total {events_total}',
        f'adjutant_role_grants_total {role_grants_total}',
    ]
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


class BotHealthServer:
    """Lifecycle manager for the aiohttp listener. Owned by the bot's
    setup_hook so a graceful shutdown stops the listener with everything
    else."""

    def __init__(self, bot: Any, *, host: str, port: int):
        self.bot = bot
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application()
        app["bot"] = self.bot
        app.router.add_get("/healthz", _healthz)
        app.router.add_get("/metrics", _metrics)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=self.host, port=self.port)
        await site.start()
        self._runner = runner
        self._site = site
        log.info(
            "[health] listening on http://%s:%d (healthz, metrics)",
            self.host, self.port,
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        log.info("[health] stopped")


async def maybe_start_health_server(bot: Any) -> BotHealthServer | None:
    """Boots the listener if BOT_HEALTH_PORT is set, returning the
    server handle for shutdown. Returns None when disabled — caller
    should treat that as "feature off, skip teardown."""
    port = _enabled_port()
    if port is None:
        return None
    server = BotHealthServer(bot, host=_bind_host(), port=port)
    try:
        await server.start()
    except OSError as e:
        log.warning(
            "[health] couldn't bind %s:%d (%s) — endpoint disabled",
            _bind_host(), port, e,
        )
        return None
    return server
