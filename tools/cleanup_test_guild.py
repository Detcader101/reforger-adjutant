"""Remove leftovers from self-test and diagnostic runs in a test guild.

Only touches objects whose names match a known harness prefix, so it can
never delete a community's real roles or channels. Dry-run by default:

    .venv\\Scripts\\python.exe tools\\cleanup_test_guild.py            # show
    .venv\\Scripts\\python.exe tools\\cleanup_test_guild.py --delete   # do it
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord

from adjutant.config import Config

# Names created by adjutant/selftest.py and ad-hoc diagnostics. Anything not
# matching one of these is left strictly alone.
PREFIXES = ("selftest-", "diag-", "Selftest ", "Team selftest", "Op: selftest")


def is_harness_object(name: str) -> bool:
    return any(name.startswith(p) for p in PREFIXES)


async def main() -> int:
    delete = "--delete" in sys.argv
    config = Config.load()
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            for guild in client.guilds:
                targets: list = []
                for channel in list(guild.channels):
                    if is_harness_object(channel.name):
                        targets.append(channel)
                for role in list(guild.roles):
                    if not role.is_default() and is_harness_object(role.name):
                        targets.append(role)

                print(f"\n=== {guild.name} ({guild.id}) ===")
                if not targets:
                    print("  nothing to clean")
                    continue
                for obj in targets:
                    print(
                        f"  {'deleting' if delete else 'would delete'}: {type(obj).__name__} {obj.name!r}"
                    )
                    if delete:
                        try:
                            await obj.delete(reason="Adjutant: self-test cleanup")
                        except discord.Forbidden:
                            # A leftover channel may deny the bot view_channel,
                            # which makes it undeletable (50001 Missing Access).
                            # Grant ourselves access first, then retry.
                            if isinstance(obj, discord.abc.GuildChannel):
                                try:
                                    await obj.set_permissions(
                                        guild.me,
                                        view_channel=True,
                                        reason="Adjutant: regaining access to clean up",
                                    )
                                    await obj.delete(reason="Adjutant: self-test cleanup")
                                    print("    (recovered access, deleted)")
                                    continue
                                except discord.HTTPException as retry_exc:
                                    exc = retry_exc  # type: ignore[assignment]
                            print(f"    failed: {exc}")
                            print("    -> delete this one by hand in Discord")
                        except discord.HTTPException as exc:
                            print(f"    failed: {exc}")
                if not delete:
                    print("  (dry run — pass --delete to remove these)")
        finally:
            await client.close()

    await client.start(config.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
