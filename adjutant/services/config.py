"""Pure logic backing /config: feature-flag JSON handling and permission-
threshold validation.

Discord-free so it's directly testable; adjutant/cogs/config.py is the thin
adapter that reads/writes the DB and talks to Discord.
"""

from __future__ import annotations

import json

from . import ranks as ranks_service

# The four opt-in features a guild can toggle. Kept here (not imported from
# cogs/setup.py) so this module has zero Discord dependency and config.py
# doesn't couple to the wizard's internals.
FEATURE_KEYS: tuple[str, ...] = ("teams", "events", "map", "serverlink")


def parse_features(raw: str | None) -> dict[str, bool]:
    """Parse the guilds.features JSON column. Missing or malformed JSON is
    treated as no features enabled, never as an error surfaced to the user —
    the column is bot-written, but a hand-edited DB or a future schema
    change shouldn't be able to crash /config."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: bool(v) for k, v in data.items() if isinstance(k, str)}


def enabled_features(raw: str | None) -> set[str]:
    return {k for k, v in parse_features(raw).items() if v}


def set_feature(raw: str | None, feature: str, enabled: bool) -> str:
    """Return updated features JSON with `feature` set to `enabled`,
    preserving any other keys already present (including unrecognised ones
    from a future feature this build doesn't know about)."""
    features = parse_features(raw)
    features[feature] = enabled
    return json.dumps(features)


def valid_permission_key(key: str) -> bool:
    return key in ranks_service.DEFAULT_PERMISSIONS


def valid_rank_position(position: int, ladder) -> bool:
    """Whether `position` matches an actual rung on this guild's ladder —
    ladders aren't guaranteed contiguous or zero-based, so this can't just
    range-check."""
    return any(entry.position == position for entry in ladder)


def rank_name_for_position(ladder, position: int) -> str:
    """Display name for a ladder position, falling back to the raw number
    if no rank occupies it (e.g. an override predating a ladder edit)."""
    entry = next((e for e in ladder if e.position == position), None)
    return entry.name if entry is not None else f"position {position}"
