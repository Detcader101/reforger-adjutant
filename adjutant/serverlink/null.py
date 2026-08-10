"""Null backend — the default. Every guild starts here; `/server unlink`
returns them to it. Always unreachable, no capabilities, and never raises —
this is what makes every other feature safe to build against a link that
might not exist."""

from __future__ import annotations

from .base import ServerLink, ServerStatus

NOT_LINKED_DETAIL = "No server linked — running Discord-side only."


class NullLink(ServerLink):
    can_players = False
    can_admin = False
    can_live_state = False

    async def status(self) -> ServerStatus:
        return ServerStatus(
            name="",
            scenario="",
            players=0,
            max_players=0,
            reachable=False,
            detail=NOT_LINKED_DETAIL,
        )
