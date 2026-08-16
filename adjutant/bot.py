"""AdjutantBot — core bot class, cog loading, command sync, shared services."""

from __future__ import annotations

import logging

import aiosqlite
import discord
from discord.ext import commands

from . import bot_health
from . import db as database
from .config import Config
from .services import guilds as guilds_service

log = logging.getLogger(__name__)

COGS = (
    "adjutant.cogs.setup",
    "adjutant.cogs.config",
    "adjutant.cogs.roles",
    "adjutant.cogs.teams",
    "adjutant.cogs.events",
    "adjutant.cogs.map",
    "adjutant.cogs.admin",
    "adjutant.cogs.serverlink",
    "adjutant.cogs.hub",
)


class AdjutantBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True  # role/rank management needs the member cache
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.db: aiosqlite.Connection | None = None
        self.health_server: bot_health.BotHealthServer | None = None

    async def setup_hook(self) -> None:
        self.db = await database.connect(self.config.db_path)
        self.health_server = await bot_health.maybe_start_health_server(self)
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded %s", cog)
            except commands.ExtensionNotFound:
                log.warning("Cog %s not present yet, skipping", cog)
        if self.config.dev_guild_ids:
            for gid in self.config.dev_guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guilds %s", self.config.dev_guild_ids)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    async def on_ready(self) -> None:
        assert self.user is not None
        sha = self.config.git_sha[:7] if self.config.git_sha else "local"
        log.info("Reporting for duty as %s (%s) [%s]", self.user, self.user.id, sha)
        if self.db is not None:
            created = await guilds_service.ensure_many(self.db, (g.id for g in self.guilds))
            if created:
                log.info("Registered %d guild(s) not seen before", created)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self.db is not None:
            await guilds_service.ensure_guild(self.db, guild.id)
        log.info("Joined guild %s (%s)", guild.name, guild.id)

    async def close(self) -> None:
        if self.health_server is not None:
            await self.health_server.stop()
        if self.db is not None:
            await self.db.close()
        await super().close()
