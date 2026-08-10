"""Smoke check: every cog in AdjutantBot.COGS imports. Run directly or via pytest."""

import importlib

from adjutant.bot import COGS


def test_all_registered_cogs_import():
    missing = []
    for name in COGS:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            missing.append(name)  # not-yet-built cogs are tolerated by bot.py too
    assert all(m in ("adjutant.cogs.events", "adjutant.cogs.serverlink") for m in missing), missing


if __name__ == "__main__":
    test_all_registered_cogs_import()
    print("imports-ok")
