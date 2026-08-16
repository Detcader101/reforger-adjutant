"""ServerLink — the interface every game-server backend implements.

A guild's link is one of `null` / `a2s` / `rcon` / `feed` (see
docs/SERVER_INTEGRATION.md for the tier table). Consumers — the serverlink
cog, and eventually the map cog for live positions — code against this ABC
and the `can_*` capability flags, never against a concrete backend type, so
every feature degrades gracefully when its tier is absent.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


class NotSupported(Exception):
    """Raised when a capability isn't available on this backend/tier.

    Callers (the cog) are expected to catch this and decline politely via
    `voice.decline`/`voice.broken` rather than let it surface as an error.
    """


@dataclass(slots=True)
class ServerStatus:
    """Result of `ServerLink.status()`. Always returned, never raised —
    unreachable servers report `reachable=False` with a courteous `detail`."""

    name: str
    scenario: str
    players: int
    max_players: int
    reachable: bool
    detail: str = ""


@dataclass(slots=True)
class PlayerInfo:
    """One row of `ServerLink.players()` or a live snapshot."""

    name: str
    player_id: str
    uuid: str | None = None
    faction: str | None = None
    position: tuple[float, float, float] | None = None


@dataclass(slots=True)
class BaseInfo:
    """One Conflict base in a live snapshot."""

    name: str
    faction: str | None
    position: tuple[float, float, float]
    flags: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class Snapshot:
    """Normalized live-state push — from the Tier-3 feed today, potentially
    other live-capable backends later. `timestamp` is Unix epoch seconds."""

    players: list[PlayerInfo]
    bases: list[BaseInfo]
    timestamp: float


SnapshotCallback = Callable[[Snapshot], Awaitable[None] | None]


class ServerLink(ABC):
    """Capability flags default closed; a backend opts in by overriding the
    class attribute. `can_players` gates `/server players`, `can_admin`
    gates kick/ban/restart/shutdown, `can_live_state` gates snapshot
    subscription (only the feed backend sets this True today)."""

    can_players: bool = False
    can_admin: bool = False
    can_live_state: bool = False

    def __init__(self) -> None:
        self._snapshot_subscribers: list[SnapshotCallback] = []

    # -- lifecycle ----------------------------------------------------------

    async def open(self) -> None:
        """Establish any long-lived connection. Default no-op; backends
        without one (null, a2s) don't need to override this."""
        return

    async def close(self) -> None:
        """Tear down any long-lived connection. Default no-op. Must be
        idempotent — the cog may call it on a link that never opened."""
        return

    # -- reads ----------------------------------------------------------

    @abstractmethod
    async def status(self) -> ServerStatus: ...

    async def players(self) -> list[PlayerInfo]:
        raise NotSupported("Player listing isn't available on this link.")

    # -- admin actions --------------------------------------------------

    async def kick(self, player_id: str, reason: str = "") -> None:
        raise NotSupported("Kicking isn't available on this link.")

    async def ban(self, player_id: str, reason: str = "") -> None:
        raise NotSupported("Banning isn't available on this link.")

    async def restart(self) -> None:
        raise NotSupported("Restarting isn't available on this link.")

    async def shutdown_server(self) -> None:
        raise NotSupported("Shutting the server down isn't available on this link.")

    # -- live state -------------------------------------------------------

    def on_snapshot(self, callback: SnapshotCallback) -> None:
        """Register a callback invoked with a `Snapshot` whenever live state
        arrives. Safe to call on any backend — it's a no-op registration on
        backends that never emit (`can_live_state` stays False), so
        consumers don't need to branch on backend type."""
        self._snapshot_subscribers.append(callback)

    def remove_snapshot_listener(self, callback: SnapshotCallback) -> None:
        try:
            self._snapshot_subscribers.remove(callback)
        except ValueError:
            pass

    async def _emit_snapshot(self, snapshot: Snapshot) -> None:
        """Backends call this to fan a normalized Snapshot out to subscribers."""
        for callback in list(self._snapshot_subscribers):
            result = callback(snapshot)
            if inspect.isawaitable(result):
                await result
