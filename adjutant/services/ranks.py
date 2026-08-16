"""Rank ladder resolution and permission gating.

Pure logic, Discord-free: takes plain role-id iterables and dataclasses, so
it can be tested without a bot, a guild, or a database connection. Cogs load
the ladder/overrides from the DB and Discord role ids from the member, then
hand them here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RankEntry:
    """One rung of a guild's rank ladder. Higher `position` = more senior."""

    role_id: int
    position: int
    name: str


# Default minimum rank position required per bot permission. Guilds can
# override any of these via the `permissions` table. Assumes the SPEC's
# default five-rung ladder (Recruit=0 .. Command=4), but any ladder works —
# these are just sensible starting minimums.
DEFAULT_PERMISSIONS: Mapping[str, int] = {
    "map.edit": 2,  # NCO
    "events.create": 2,  # NCO
    "teams.manage": 3,  # Officer
    "setup.run": 4,  # Command (setup is also admin-gated separately)
    "roles.manage": 4,  # Command — granting/revoking rank roles, editing the ladder
}

# A permission key with neither a guild override nor a built-in default is
# treated as unreachable by rank (admins only) rather than silently open.
UNKNOWN_PERMISSION_MIN_RANK = 2**31 - 1


def resolve_rank(member_role_ids: Iterable[int], ladder: Iterable[RankEntry]) -> int | None:
    """Highest ladder position among the roles a member holds, or None if
    they hold no ranked role."""
    held = set(member_role_ids)
    positions = [entry.position for entry in ladder if entry.role_id in held]
    return max(positions) if positions else None


def get_min_rank(permission: str, overrides: Mapping[str, int]) -> int:
    """Minimum rank position required for `permission`, honouring a
    per-guild override before falling back to the built-in default."""
    if permission in overrides:
        return overrides[permission]
    return DEFAULT_PERMISSIONS.get(permission, UNKNOWN_PERMISSION_MIN_RANK)


def has_permission(member_rank: int | None, required_min_rank: int, is_admin: bool) -> bool:
    """Admins always pass. An unranked (None) member never passes a rank
    check. Otherwise the member's rank must meet or exceed the minimum."""
    if is_admin:
        return True
    if member_rank is None:
        return False
    return member_rank >= required_min_rank
