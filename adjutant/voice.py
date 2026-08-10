"""The adjutant's voice — every user-facing string funnels through here.

Persona: a courteous British adjutant. Dry, unflappable, brief. Declines
politely, never snarks, and is plain-spoken when something is broken or
missing. Keep it understated: one good sentence beats three clever ones.

Guilds with minimal_mode enabled get the same information without garnish —
helpers here accept a `minimal` flag and strip flourish, not content.
"""

from __future__ import annotations

import discord

# Embed accent colours (flagged for Jay Jay's design pass — placeholder palette)
COLOUR_PRIMARY = discord.Colour.from_str("#4a5d43")   # olive drab
COLOUR_ALERT = discord.Colour.from_str("#a33c3c")
COLOUR_INFO = discord.Colour.from_str("#3c6e91")


def embed(title: str, description: str = "", *, colour: discord.Colour = COLOUR_PRIMARY,
          minimal: bool = False) -> discord.Embed:
    if minimal:
        return discord.Embed(title=title, description=description)
    return discord.Embed(title=title, description=description, colour=colour)


def decline(reason: str) -> str:
    """A refusal with a stiff upper lip. `reason` should say what rank/permission
    would be required — transparency over mystery."""
    return f"I'm afraid not. {reason}"


def broken(what: str, remedy: str) -> str:
    """Honest breakage notice: what's wrong and what the user can do."""
    return f"Small snag: {what} {remedy}"


def nudge_ingame() -> str:
    return "That one's better handled over in-game comms — keeps the fog of war foggy."
