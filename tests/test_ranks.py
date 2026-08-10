"""Rank ladder resolution and permission gating: pure logic, Discord-free."""

import pytest

from adjutant.services import ranks


def _ladder():
    return [
        ranks.RankEntry(role_id=100, position=0, name="Recruit"),
        ranks.RankEntry(role_id=101, position=1, name="Private"),
        ranks.RankEntry(role_id=102, position=2, name="NCO"),
        ranks.RankEntry(role_id=103, position=3, name="Officer"),
        ranks.RankEntry(role_id=104, position=4, name="Command"),
    ]


def test_resolve_rank_returns_highest_position_held():
    assert ranks.resolve_rank([100, 102, 101], _ladder()) == 2


def test_resolve_rank_returns_none_when_no_ranked_roles_held():
    assert ranks.resolve_rank([999, 888], _ladder()) is None


def test_resolve_rank_ignores_roles_not_on_the_ladder():
    assert ranks.resolve_rank([100, 55555], _ladder()) == 0


def test_resolve_rank_handles_empty_ladder():
    assert ranks.resolve_rank([100, 101], []) is None


def test_has_permission_true_for_admin_regardless_of_rank():
    assert ranks.has_permission(None, required_min_rank=4, is_admin=True) is True


def test_has_permission_false_when_member_is_unranked_and_not_admin():
    assert ranks.has_permission(None, required_min_rank=0, is_admin=False) is False


def test_has_permission_true_when_rank_meets_minimum():
    assert ranks.has_permission(2, required_min_rank=2, is_admin=False) is True


def test_has_permission_true_when_rank_exceeds_minimum():
    assert ranks.has_permission(3, required_min_rank=2, is_admin=False) is True


def test_has_permission_false_when_rank_below_minimum():
    assert ranks.has_permission(1, required_min_rank=2, is_admin=False) is False


def test_get_min_rank_uses_guild_override_when_present():
    overrides = {"map.edit": 1}
    assert ranks.get_min_rank("map.edit", overrides) == 1


def test_get_min_rank_falls_back_to_default_when_no_override():
    assert ranks.get_min_rank("map.edit", {}) == ranks.DEFAULT_PERMISSIONS["map.edit"]


def test_get_min_rank_falls_back_to_default_for_unlisted_permission_key():
    # unknown permission with no override and no built-in default: treat as
    # admin-only (unreachable by rank) rather than silently open.
    assert ranks.get_min_rank("something.unknown", {}) == ranks.UNKNOWN_PERMISSION_MIN_RANK
