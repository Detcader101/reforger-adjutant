"""GameServerLink — capability-tiered game-server integration.

Public surface consumers should import from this package rather than
reaching into individual modules:

    from adjutant.serverlink import (
        ServerLink, ServerStatus, PlayerInfo, BaseInfo, Snapshot, NotSupported,
        NullLink, FeedLink, A2SLink, RconLink,
    )

See `base.ServerLink` for the interface every backend implements, and
`docs/SERVER_INTEGRATION.md` for the tier design this mirrors.
"""

from __future__ import annotations

from .base import (
    BaseInfo,
    NotSupported,
    PlayerInfo,
    ServerLink,
    ServerStatus,
    Snapshot,
)
from .feed import FeedLink
from .null import NullLink

__all__ = [
    "BaseInfo",
    "NotSupported",
    "PlayerInfo",
    "ServerLink",
    "ServerStatus",
    "Snapshot",
    "NullLink",
    "FeedLink",
    "A2SLink",
    "RconLink",
]


def __getattr__(name: str):
    # A2SLink / RconLink are imported lazily so that importing this package
    # never requires a2s or berconpy to be installed unless a guild actually
    # uses that backend. FeedLink is imported eagerly above since it only
    # needs aiohttp, already a hard dependency of discord.py itself.
    if name == "A2SLink":
        from .a2s_link import A2SLink

        return A2SLink
    if name == "RconLink":
        from .rcon_link import RconLink

        return RconLink
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
