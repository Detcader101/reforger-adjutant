"""Process-level configuration from environment / .env.

Per-guild settings do NOT live here — they belong in the database and are
managed through /setup. This module is only for secrets and deploy metadata.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    db_path: Path
    dev_guild_ids: tuple[int, ...] = field(default=())
    git_sha: str = ""
    git_subject: str = ""

    @classmethod
    def load(cls, env_file: str | os.PathLike | None = None) -> Config:
        load_dotenv(env_file)
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and add the "
                "bot token from the Discord developer portal."
            )
        raw_guilds = os.environ.get("DEV_GUILD_IDS", "").strip()
        dev_guild_ids = tuple(int(g) for g in (p.strip() for p in raw_guilds.split(",")) if g)
        db_path = Path(os.environ.get("ADJUTANT_DB", "").strip() or "data/adjutant.db")
        return cls(
            token=token,
            db_path=db_path,
            dev_guild_ids=dev_guild_ids,
            git_sha=os.environ.get("BOT_GIT_SHA", ""),
            git_subject=os.environ.get("BOT_GIT_SUBJECT", ""),
        )
