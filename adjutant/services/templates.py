"""Setup templates and custom-ladder validation.

Pure logic, Discord-free — same rule as services/ranks.py. Templates are
declarative data (a Template is a rank list plus a channel scaffold);
adding a new one is a data edit here, never a branch in the setup cog.
The cog (adjutant/cogs/setup.py) is the thin adapter that turns a Template
into real Discord roles/channels, and reuses `validate_ladder_names` before
ever touching Discord for a custom /setup ranks submission.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_RANKS = 2
MAX_RANKS = 20
MAX_RANK_NAME_LENGTH = 100


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """One channel a template scaffolds. `category` is required — every
    template-created channel lives in a named category, both so an admin
    can see at a glance what the bot made and so re-running the template
    reuses that category rather than losing track of it."""

    name: str
    kind: str  # "text" or "voice"
    category: str


@dataclass(frozen=True, slots=True)
class Template:
    key: str
    label: str
    description: str
    ranks: tuple[str, ...] = ()  # lowest-first; ladder position is index
    channels: tuple[ChannelSpec, ...] = ()


TEMPLATES: dict[str, Template] = {
    "minimal": Template(
        key="minimal",
        label="Minimal",
        description="Config only — no roles or channels created.",
    ),
    "vanilla": Template(
        key="vanilla",
        label="Vanilla",
        description="A light rank ladder and a couple of shared channels.",
        ranks=("Recruit", "Member", "Veteran"),
        channels=(
            ChannelSpec(name="general", kind="text", category="Community"),
            ChannelSpec(name="Community Voice", kind="voice", category="Community"),
        ),
    ),
    "milsim": Template(
        key="milsim",
        label="Milsim",
        description="A deeper rank ladder plus an ops/briefing channel scaffold.",
        ranks=("Recruit", "Private", "Corporal", "Sergeant", "Lieutenant", "Captain", "Command"),
        channels=(
            ChannelSpec(name="briefing-room", kind="text", category="Operations"),
            ChannelSpec(name="ops-planning", kind="text", category="Operations"),
            ChannelSpec(name="Command Voice", kind="voice", category="Operations"),
        ),
    ),
}

DEFAULT_TEMPLATE_KEY = "minimal"


def validate_ladder_names(names: list[str]) -> list[str]:
    """Check a proposed custom rank ladder (lowest-first). Returns a list of
    plain-English problems — empty means the ladder is good to apply.
    Deliberately collects every problem rather than stopping at the first,
    so a decline message can tell the admin everything at once instead of
    a frustrating one-fix-at-a-time loop."""
    problems: list[str] = []

    if len(names) < MIN_RANKS:
        problems.append(f"needs at least {MIN_RANKS} ranks (got {len(names)})")
    elif len(names) > MAX_RANKS:
        problems.append(f"needs at most {MAX_RANKS} ranks (got {len(names)})")

    for name in names:
        if not (1 <= len(name) <= MAX_RANK_NAME_LENGTH):
            problems.append(f"{name!r} must be 1-{MAX_RANK_NAME_LENGTH} characters")

    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        key = name.casefold()
        if key in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(key)
    if duplicates:
        problems.append(f"duplicate rank name(s): {', '.join(duplicates)}")

    return problems
