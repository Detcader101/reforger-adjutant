"""A2S (Valve Source query protocol) polling backend — Tier 1.

Only `status()` is implemented, from `A2S_INFO` (name, map, counts) — this
is a standard, trustworthy query. Per docs/SERVER_INTEGRATION.md, player
*names* from `A2S_PLAYER` are unverified for Reforger, so `can_players`
stays False here deliberately; player listing is an RCON-tier (`rcon_link`)
feature only.
"""

from __future__ import annotations

import asyncio
import logging

import a2s

from .base import ServerLink, ServerStatus

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0
UNREACHABLE_DETAIL = (
    "Couldn't reach the server on its query port — it may be offline, "
    "starting up, or the port isn't open."
)


class A2SLink(ServerLink):
    can_players = False
    can_admin = False
    can_live_state = False

    def __init__(self, host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT):
        super().__init__()
        self.host = host
        self.port = port
        self.timeout = timeout

    async def status(self) -> ServerStatus:
        try:
            info = await a2s.ainfo((self.host, self.port), timeout=self.timeout)
        except (OSError, asyncio.TimeoutError, a2s.BrokenMessageError) as exc:
            log.info("A2S query to %s:%s failed: %s", self.host, self.port, exc)
            return ServerStatus(
                name="",
                scenario="",
                players=0,
                max_players=0,
                reachable=False,
                detail=UNREACHABLE_DETAIL,
            )
        return ServerStatus(
            name=info.server_name,
            scenario=info.map_name,
            players=info.player_count,
            max_players=info.max_players,
            reachable=True,
            detail="",
        )
