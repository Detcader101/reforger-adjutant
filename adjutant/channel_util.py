"""Channel lookup helpers robust to emoji prefixes.

A guild's channel plan may brand names with emoji prefixes like
`🗺️-map`. Code that wants to find a channel by its semantic base name
("map", "audit-log", etc.) should go through these helpers so a rename
in the channel plan doesn't shatter lookups everywhere else.
"""
from __future__ import annotations

import discord


def find_text_channel(
    guild: discord.Guild, base_name: str,
) -> discord.TextChannel | None:
    """Return the text channel whose name equals `base_name` or ends
    with `-{base_name}` (i.e. has an emoji prefix like `🗺️-`). None if
    no such channel exists in this guild."""
    for ch in guild.text_channels:
        if ch.name == base_name:
            return ch
        if ch.name.endswith(f"-{base_name}"):
            return ch
    return None


def base_name_of(channel: discord.abc.GuildChannel) -> str:
    """Strip a leading emoji-dash prefix to recover the semantic base
    name. `🗺️-map` -> `map`. If the channel has no prefix the name is
    returned unchanged."""
    name = channel.name
    if "-" not in name:
        return name
    # The prefix is considered "emoji" if it contains any non-ASCII
    # character — plain-ascii channel names like `off-topic` keep their
    # dashes intact.
    prefix, _, rest = name.partition("-")
    if prefix and any(ord(c) > 127 for c in prefix):
        return rest
    return name
