"""One-shot diagnostic: log in, report what the bot can see and do in a guild.

Read-only. Creates nothing, changes nothing. Run:
    .venv\\Scripts\\python.exe tools\\probe_guild.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

from adjutant.config import Config  # noqa: E402

NEEDED = (
    "manage_roles",
    "manage_channels",
    "send_messages",
    "embed_links",
    "attach_files",
    "read_message_history",
)


async def main() -> int:
    config = Config.load()
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(client)
    result = {"code": 1}

    @client.event
    async def on_ready() -> None:
        try:
            print(f"Logged in as {client.user} ({client.user.id})")
            print(f"Guilds visible: {len(client.guilds)}")
            for guild in client.guilds:
                me = guild.me
                print(f"\n=== {guild.name} ({guild.id}) ===")
                print(f"  members cached: {len(guild.members)}  owner: {guild.owner_id}")
                print(f"  bot top role: {me.top_role.name!r} position={me.top_role.position}")
                highest_other = max(
                    (r.position for r in guild.roles if r != me.top_role and not r.is_default()),
                    default=0,
                )
                print(f"  highest other role position: {highest_other}")
                if me.top_role.position <= highest_other:
                    print("  !! bot role is NOT above all other roles — it cannot manage the ones above it")
                perms = me.guild_permissions
                missing = [p for p in NEEDED if not getattr(perms, p)]
                print(f"  administrator: {perms.administrator}")
                print(f"  missing needed perms: {missing or 'none'}")
                print(f"  text channels: {[c.name for c in guild.text_channels]}")
                print(f"  categories: {[c.name for c in guild.categories]}")
                print(f"  roles: {[(r.name, r.position) for r in sorted(guild.roles, key=lambda r: -r.position)]}")
                cmds = await tree.fetch_commands(guild=discord.Object(id=guild.id))
                names = sorted(c.name for c in cmds)
                print(f"  guild slash commands registered: {len(cmds)} {names}")
            result["code"] = 0
        finally:
            await client.close()

    await client.start(config.token)
    return result["code"]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
