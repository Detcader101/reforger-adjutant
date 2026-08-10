"""Shared pytest fixtures for the Adjutant test suite.

Fixtures in here are auto-discovered by pytest, so tests can just request
them by name without explicit imports.

Two groups:
  - DB isolation: `tmp_conn` opens a real aiosqlite connection (migrated
    fresh) against a per-test tmp_path database.
  - Discord mocks: `mock_guild`, `mock_member`, `make_role` build
    MagicMock objects that quack like discord.py's Guild/Member/Role for
    the narrow set of attributes cogs touch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from adjutant import db as database


# --------------------------------------------------------------------------- #
# DB isolation                                                                 #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def tmp_conn(tmp_path):
    """A real aiosqlite connection against a throwaway per-test database,
    migrated fresh by `adjutant.db.connect`. Tests that touch guild-scoped
    tables (panels, bot_state, posted_messages, role_grants, ...) still
    need to insert their own `guilds` row before writing child rows —
    this fixture only guarantees the schema is in place."""
    conn = await database.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# Discord mocks                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class FakeRole:
    """Minimal stand-in for discord.Role — only the attributes cogs
    read. `id` is auto-assigned from an incrementing counter so equality-
    by-id works between fixtures."""
    id: int
    name: str
    position: int = 1

    def __hash__(self) -> int:
        return hash(self.id)


class _RoleFactory:
    """Hands out FakeRole instances with unique ids."""

    def __init__(self) -> None:
        self._next_id = 1000

    def __call__(self, name: str, position: int = 1) -> FakeRole:
        self._next_id += 1
        return FakeRole(id=self._next_id, name=name, position=position)


@pytest.fixture
def make_role() -> _RoleFactory:
    return _RoleFactory()


@pytest.fixture
def mock_guild(make_role):
    """A MagicMock guild with a mutable `roles` list and a configurable
    `get_member` return-map. Tests push FakeRole objects into guild.roles
    so that `discord.utils.get(guild.roles, name=...)` finds them."""
    guild = MagicMock()
    guild.id = 999_000_001
    guild.name = "Adjutant (test)"
    guild.roles = []

    guild.default_role = make_role("@everyone", position=0)
    guild.roles.append(guild.default_role)

    # get_member is populated by tests to simulate "this user is / isn't
    # in the guild right now". Default: everyone is absent.
    guild._member_map: dict[int, MagicMock] = {}
    guild.get_member = lambda uid: guild._member_map.get(uid)

    # Role creation — present so any helper that ensures a role exists
    # doesn't blow up if a test leaves it out of guild.roles by mistake.
    async def _create_role(name, reason=None, mentionable=False):
        role = make_role(name)
        guild.roles.append(role)
        return role
    guild.create_role = _create_role

    async def _edit_role_positions(positions, reason=None):
        for role, pos in positions.items():
            role.position = pos
    guild.edit_role_positions = _edit_role_positions

    guild.text_channels = []  # channel_util lookups get nothing by default
    guild.get_channel = lambda cid: None  # audit's channel_id path, ditto
    guild.me = MagicMock()
    guild.me.top_role = make_role("bot-top", position=100)

    return guild


def _make_member(guild, *, member_id: int, display_name: str = "Tester",
                 roles: Iterable[FakeRole] = ()) -> MagicMock:
    member = MagicMock()
    member.id = member_id
    member.guild = guild
    member.bot = False
    member.roles = list(roles)
    member.mention = f"<@{member_id}>"
    member.__str__ = lambda self=None: display_name  # type: ignore[assignment]

    async def _add_roles(*new_roles, reason=None):
        for r in new_roles:
            if r not in member.roles:
                member.roles.append(r)
    member.add_roles = _add_roles

    async def _remove_roles(*to_remove, reason=None):
        member.roles = [r for r in member.roles if r not in to_remove]
    member.remove_roles = _remove_roles

    async def _send(*args, **kwargs):
        return MagicMock()
    member.send = _send

    guild._member_map[member_id] = member
    return member


@pytest.fixture
def mock_member(mock_guild):
    """Default member fixture — present in guild, no roles. Tests that
    need a custom setup should use the `make_member` factory fixture."""
    return _make_member(mock_guild, member_id=42, display_name="Tester")


@pytest.fixture
def make_member(mock_guild):
    """Factory form: `make_member(member_id=..., roles=[...])` in a test."""
    def _factory(*, member_id: int, display_name: str = "Tester",
                 roles: Iterable[FakeRole] = ()) -> MagicMock:
        return _make_member(
            mock_guild, member_id=member_id,
            display_name=display_name, roles=roles,
        )
    return _factory
