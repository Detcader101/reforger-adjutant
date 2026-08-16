"""BattlEye RCON backend — Tier 2 (`berconpy` v3's generic `RCONClient`,
not the Arma-3-specific `ArmaClient` — see docs/SERVER_INTEGRATION.md).

`berconpy` is a small third-party dependency, so every import of it is
wrapped inside a function/try block: this module — and in particular
`parse_players`, the pure parser tested hard in tests/test_players_parse.py
— must import cleanly with `berconpy` absent.

There is no say/broadcast command in Bohemia's RCON; this module never
implements or promises one.
"""

from __future__ import annotations

import asyncio
import logging
import re

from .base import NotSupported, PlayerInfo, ServerLink, ServerStatus

log = logging.getLogger(__name__)

# Backoff schedule (seconds) for reconnect attempts after the connection
# drops; holds at the last value rather than growing unbounded.
_RECONNECT_BACKOFFS = (1, 2, 5, 10, 20, 30)
_KEEPALIVE_CHECK_INTERVAL = 15  # seconds between "are we still connected" checks

_HEADER_RE = re.compile(r"^players\b", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^[\s\-=_]+$")
_FOOTER_COUNT_RE = re.compile(r"^\(?\d+\s+players?\b", re.IGNORECASE)

# Best-effort heuristic for "the server accepted our login but refused this
# command because we're only a monitor connection". Bohemia doesn't publish
# exact wording for Reforger's RCON, so this matches on common phrasing
# rather than a verified string — see the serverlink report for the caveat.
_PERMISSION_DENIAL_RE = re.compile(r"permission|not allowed|denied|unknown command", re.IGNORECASE)

# Reforger's maxClients caps at 16, so a genuine player-slot id is always a
# small non-negative integer. Requiring digits-only is what lets the
# whitespace-column fallback below stay tolerant of format drift without
# also matching arbitrary garbled text that happens to have 3+ words.
_PLAYER_ID_RE = re.compile(r"^\d+$")


def parse_players(text: str) -> list[PlayerInfo]:
    """Parse a raw `#players` RCON response into `PlayerInfo` rows.

    Deliberately tolerant: skips header/separator/footer-count lines and
    silently drops any row that doesn't look like `id;uid;name` (or its
    whitespace-column variant) rather than raising — a single garbled or
    unexpected line from the server should never take out `/server players`.
    """
    if not text:
        return []
    players: list[PlayerInfo] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _HEADER_RE.match(line) or _SEPARATOR_RE.match(line) or _FOOTER_COUNT_RE.match(line):
            continue
        row = _parse_player_row(line)
        if row is not None:
            players.append(row)
    return players


def _parse_player_row(line: str) -> PlayerInfo | None:
    if ";" in line:
        parts = line.split(";", 2)
        if len(parts) < 3:
            return None
        player_id, uid, name = (p.strip() for p in parts)
    else:
        tokens = line.split(None, 2)
        if len(tokens) < 3:
            return None
        player_id, uid, name = tokens[0].strip(), tokens[1].strip(), tokens[2].strip()

    if not _PLAYER_ID_RE.match(player_id) or not name:
        return None
    return PlayerInfo(name=name, player_id=player_id, uuid=uid or None)


def _import_berconpy():
    try:
        import berconpy
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise NotSupported(
            "The RCON backend needs the `berconpy` package, which isn't installed on this bot."
        ) from exc
    return berconpy


class RconLink(ServerLink):
    """One long-lived RCON connection per guild. `can_admin` starts
    optimistic and is downgraded the first time a state-changing command
    comes back looking like a permission denial (see `_PERMISSION_DENIAL_RE`)
    — the BE RCon protocol has no separate "what's my permission" query, so
    this is a behavioural probe, not a declared fact from the server."""

    can_players = True
    can_admin = True
    can_live_state = False

    def __init__(self, host: str, port: int, password: str):
        super().__init__()
        self.host = host
        self.port = port
        self.password = password
        self._client = None
        self._connect_ctx = None
        self._connected = False
        self._closing = False
        self._watchdog_task: asyncio.Task | None = None

    def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected()

    # -- lifecycle ------------------------------------------------------

    async def open(self) -> None:
        self._closing = False
        await self._connect()
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def close(self) -> None:
        self._closing = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        await self._disconnect(logout=True)

    async def _connect(self) -> None:
        berconpy = _import_berconpy()
        client = berconpy.RCONClient()
        ctx = client.connect(self.host, self.port, self.password)
        try:
            await ctx.__aenter__()
        except Exception as exc:
            log.warning("RCON connect to %s:%s failed: %s", self.host, self.port, exc)
            self._client = None
            self._connect_ctx = None
            self._connected = False
            return
        self._client = client
        self._connect_ctx = ctx
        self._connected = True

    async def _disconnect(self, *, logout: bool) -> None:
        if self._client is not None and self._connected and logout:
            try:
                # BI's custom logout command (added 1.2.1) frees the client
                # slot immediately instead of squatting it for 30-45s.
                await self._client.send_command("@logout")
            except Exception:
                pass
        if self._connect_ctx is not None:
            try:
                await self._connect_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._client = None
        self._connect_ctx = None
        self._connected = False

    async def _watchdog_loop(self) -> None:
        """Watches the connection and reconnects with backoff on drop.
        berconpy's protocol handles its own wire-level keepalive; this loop
        only needs to notice a dead connection and repair it."""
        backoff_index = 0
        while not self._closing:
            await asyncio.sleep(_KEEPALIVE_CHECK_INTERVAL)
            if self._closing:
                return
            if self.is_connected():
                backoff_index = 0
                continue
            delay = _RECONNECT_BACKOFFS[min(backoff_index, len(_RECONNECT_BACKOFFS) - 1)]
            backoff_index += 1
            log.info("RCON link to %s:%s is down, retrying in %ss", self.host, self.port, delay)
            await asyncio.sleep(delay)
            if self._closing:
                return
            await self._disconnect(logout=False)
            await self._connect()

    # -- reads ------------------------------------------------------------

    async def status(self) -> ServerStatus:
        if not self.is_connected():
            return ServerStatus(
                name="",
                scenario="",
                players=0,
                max_players=0,
                reachable=False,
                detail="RCON connection isn't currently up.",
            )
        try:
            roster = await self.players()
        except NotSupported:
            roster = []
        return ServerStatus(
            name="",
            scenario="",
            players=len(roster),
            max_players=0,
            reachable=True,
            detail="RCON doesn't expose the server name, scenario, or slot cap — "
            "pair with an A2S link for the full picture.",
        )

    async def players(self) -> list[PlayerInfo]:
        if not self.is_connected() or self._client is None:
            raise NotSupported("RCON isn't currently connected.")
        response = await self._client.send_command("#players")
        return parse_players(response)

    # -- admin actions --------------------------------------------------

    async def _run_admin_command(self, command: str) -> str:
        if not self.is_connected() or self._client is None:
            raise NotSupported("RCON isn't currently connected.")
        if not self.can_admin:
            raise NotSupported(
                "This RCON link authenticated as a monitor, not admin — it can't issue that command."
            )
        try:
            response = await self._client.send_command(command)
        except Exception as exc:
            raise NotSupported(f"RCON declined that command: {exc}") from exc
        if response and _PERMISSION_DENIAL_RE.search(response):
            # Downgrade once and remember it rather than re-probing every call.
            self.can_admin = False
            raise NotSupported("This RCON link doesn't have admin permission for that command.")
        return response

    async def kick(self, player_id: str, reason: str = "") -> None:
        command = f"#kick {player_id} {reason}".strip() if reason else f"#kick {player_id}"
        await self._run_admin_command(command)

    async def ban(self, player_id: str, reason: str = "") -> None:
        command = (
            f"#ban create {player_id} 0 {reason}".strip()
            if reason
            else f"#ban create {player_id} 0"
        )
        await self._run_admin_command(command)

    async def restart(self) -> None:
        await self._run_admin_command("#restart")

    async def shutdown_server(self) -> None:
        await self._run_admin_command("#shutdown")
