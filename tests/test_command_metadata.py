"""Discord's limits on command metadata, checked before a live sync.

These are easy to breach by accident: for a GroupCog, discord.py falls back
to the class docstring as the group's description, so an ordinary
documentation edit can exceed Discord's 100-character cap. Discord then
rejects the ENTIRE command upload with a 400 — every command silently fails
to register, not just the offending one. That happened once; this exists so
it can't happen again.

Inspects the cog *classes* rather than instantiating them, since discord.py
records this metadata at class-creation time. No bot, database or event loop
needed, so it runs in milliseconds on every test run.
"""

from __future__ import annotations

import importlib
import inspect

import pytest
from discord import app_commands
from discord.ext import commands

from adjutant.bot import COGS

MAX_NAME = 32
MAX_DESCRIPTION = 100


def _cog_classes(module_name: str):
    module = importlib.import_module(module_name)
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, commands.Cog) and obj.__module__ == module_name:
            yield obj


def _is_group(cog) -> bool:
    return bool(getattr(cog, "__cog_is_app_commands_group__", False))


def described():
    """Every (label, name, description) pair Discord will receive — including
    each GroupCog's own group entry, which is the one that broke sync."""
    for module_name in COGS:
        for cog in _cog_classes(module_name):
            if _is_group(cog):
                yield (
                    f"{cog.__name__} group",
                    getattr(cog, "__cog_group_name__", "?"),
                    getattr(cog, "__cog_group_description__", "") or "",
                )
            for command in getattr(cog, "__cog_app_commands__", ()):
                yield (cog.__name__, command.name, command.description or "")
                if isinstance(command, app_commands.Group):
                    for sub in command.walk_commands():
                        yield (cog.__name__, sub.name, sub.description or "")


def top_level_names() -> set[str]:
    """What a user sees after typing "/". A GroupCog contributes its group
    name only. `.parent` can't tell us this — the class isn't bound to a bot
    yet, so subcommands report no parent."""
    names: set[str] = set()
    for module_name in COGS:
        for cog in _cog_classes(module_name):
            if _is_group(cog):
                names.add(getattr(cog, "__cog_group_name__", "?"))
            else:
                names.update(c.name for c in getattr(cog, "__cog_app_commands__", ()))
    return names


@pytest.fixture(scope="module")
def entries():
    found = list(described())
    assert found, "no commands discovered — the walker is broken, not the commands"
    return found


def test_every_description_is_within_discords_limit(entries):
    too_long = [
        (owner, name, len(description))
        for owner, name, description in entries
        if len(description) > MAX_DESCRIPTION
    ]
    assert not too_long, f"Discord rejects the whole command upload for these: {too_long}"


def test_every_command_has_a_non_empty_description(entries):
    missing = [(owner, name) for owner, name, description in entries if not description.strip()]
    assert not missing, f"commands with no description: {missing}"


def test_every_name_is_within_discords_limit(entries):
    too_long = [(owner, name) for owner, name, _ in entries if len(name) > MAX_NAME]
    assert not too_long, f"names too long: {too_long}"


def test_every_parameter_description_is_within_discords_limit():
    too_long = []
    for module_name in COGS:
        for cog in _cog_classes(module_name):
            for command in getattr(cog, "__cog_app_commands__", ()):
                for param in getattr(command, "parameters", ()):
                    if len(param.description or "") > MAX_DESCRIPTION:
                        too_long.append(f"{command.name}:{param.name}")
    assert not too_long, f"parameter descriptions too long: {too_long}"


def test_the_typed_command_surface_stays_small():
    """The product owner asked for a handful of memorable commands with
    buttons for the rest. This guards against drifting back into a wall of
    subcommands without someone deciding to."""
    expected = {"adjutant", "admin", "event", "map", "rank", "server", "setup", "team"}
    names = set(top_level_names())
    assert names == expected, f"top-level command surface changed: {sorted(names)}"
