import logging

import discord

from .bot import AdjutantBot
from .config import Config


def main() -> None:
    discord.utils.setup_logging(level=logging.INFO)
    config = Config.load()
    bot = AdjutantBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
