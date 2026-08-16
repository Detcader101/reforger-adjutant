"""A richer fake-Discord layer for in-process cog integration tests.

Builds on (but does not modify) tests/conftest.py's MagicMock-based fixtures
— those stay exactly as other test files depend on them. This module exists
because cogs touch a lot more surface than the services do: mutable guild
role/channel state, `isinstance(x, discord.Member)` / `isinstance(x,
discord.CategoryChannel)` checks scattered through roles.py/teams.py/
events.py/admin.py, and Discord's own HTTPException/Forbidden/NotFound
exception types on the failure paths.

Design notes
------------
- Objects that get `isinstance()`-checked by cog code (Member, CategoryChannel,
  TextChannel, VoiceChannel) are `unittest.mock.MagicMock(spec=discord.X)`.
  `spec=` is the load-bearing bit: it's what makes `isinstance()` pass — a
  bare `MagicMock()` (what conftest.py's mock_guild/mock_member use) does
  NOT pass isinstance, which is fine for the services-only tests but would
  silently no-op half of every cog if used here.
- Everything else (Guild, Bot, Interaction, Message, Role) is a plain
  hand-rolled class — no cog code ever does `isinstance(guild, discord.Guild)`
  etc, so a real class is simpler to read and debug than mock plumbing.
- discord.PermissionOverwrite is used unmodified (real class, no network
  I/O) so permission-overwrite assertions check the exact objects the cogs
  built, not a re-implementation of them.
- discord.Forbidden / discord.NotFound / discord.HTTPException are
  constructed for real via the helpers below, so error-handling paths run
  against the actual exception types view_util.py checks with isinstance.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import discord

# --------------------------------------------------------------------------- #
# ids                                                                          #
# --------------------------------------------------------------------------- #

# One counter shared by every id-bearing fake in the whole test session. Two
# things lean on this: (a) guild/user/role/channel ids never collide across
# tests even when tests run in the same process, and (b) admin.py's
# rate-limiter is a MODULE-LEVEL singleton keyed on (guild_id, user_id,
# command_name) — globally-unique ids are what keeps one test's rate-limit
# burst from bleeding into another's.
_id_counter = itertools.count(900_000_001)


def next_id() -> int:
    return next(_id_counter)


# --------------------------------------------------------------------------- #
# Discord exceptions                                                          #
# --------------------------------------------------------------------------- #


def _http_exception(cls: type, status: int, reason: str, message: str):
    response = SimpleNamespace(status=status, reason=reason)
    return cls(response, message)


def forbidden(message: str = "Missing Permissions") -> discord.Forbidden:
    return _http_exception(discord.Forbidden, 403, "Forbidden", message)


def not_found(message: str = "Unknown Message") -> discord.NotFound:
    return _http_exception(discord.NotFound, 404, "Not Found", message)


def http_exception(
    status: int = 500, message: str = "Internal Server Error"
) -> discord.HTTPException:
    return _http_exception(discord.HTTPException, status, "Error", message)


def server_error(message: str = "Service Unavailable") -> discord.DiscordServerError:
    return _http_exception(discord.DiscordServerError, 503, "Service Unavailable", message)


# --------------------------------------------------------------------------- #
# Roles                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class FakeRole:
    id: int
    name: str
    position: int = 1
    mentionable: bool = False

    def __hash__(self) -> int:
        return hash(self.id)

    def __str__(self) -> str:  # cogs interpolate roles into f-strings sometimes
        return self.name

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"


def make_role(name: str, position: int = 1, *, mentionable: bool = False) -> FakeRole:
    return FakeRole(id=next_id(), name=name, position=position, mentionable=mentionable)


def _attach_deletable(role: FakeRole, guild: FakeGuild) -> FakeRole:
    """Real discord.Role has an async delete() — bot-created roles (team
    roles, event Op roles, rank-ladder roles) get deleted by the cogs, so
    roles handed back from guild.create_role need one too. default_role and
    guild.me.top_role are never passed to .delete() by any cog, so they're
    left as plain FakeRole instances without it."""
    role._fail_delete = None  # type: ignore[attr-defined]

    def fail_next_delete(exc: Exception) -> None:
        role._fail_delete = exc  # type: ignore[attr-defined]

    role.fail_next_delete = fail_next_delete  # type: ignore[attr-defined]

    async def _delete(reason: str | None = None) -> None:
        if role._fail_delete is not None:  # type: ignore[attr-defined]
            exc, role._fail_delete = role._fail_delete, None  # type: ignore[attr-defined]
            raise exc
        if role in guild.roles:
            guild.roles.remove(role)

    role.delete = _delete  # type: ignore[attr-defined]
    return role


# --------------------------------------------------------------------------- #
# Messages                                                                     #
# --------------------------------------------------------------------------- #


class FakeMessage:
    """Stand-in for discord.Message. Tracks edits and deletes so tests can
    assert on them directly, and supports scripted failures
    (`fail_next_edit` / `fail_delete`) for error-path tests."""

    def __init__(
        self,
        *,
        id: int | None = None,
        channel: Any = None,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
    ):
        self.id = id if id is not None else next_id()
        self.channel = channel
        self.content = content
        self.embed = embed
        self.embeds = embeds or ([embed] if embed else [])
        self.view = view
        self.attachments: list[Any] = list(files or ([file] if file else []))
        self.deleted = False
        self.edits: list[dict[str, Any]] = []
        self._fail_edit: Exception | None = None
        self._fail_delete: Exception | None = None

    def fail_next_edit(self, exc: Exception) -> None:
        self._fail_edit = exc

    def fail_delete(self, exc: Exception) -> None:
        self._fail_delete = exc

    async def edit(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = "__unset__",  # type: ignore[assignment]
        attachments: list[Any] | None = None,
        **kwargs: Any,
    ) -> FakeMessage:
        if self._fail_edit is not None:
            exc, self._fail_edit = self._fail_edit, None
            raise exc
        self.edits.append(
            {
                "content": content,
                "embed": embed,
                "embeds": embeds,
                "view": view,
                "attachments": attachments,
            }
        )
        if content is not None:
            self.content = content
        if embed is not None:
            self.embed = embed
            self.embeds = [embed]
        if view != "__unset__":
            self.view = view
        if attachments is not None:
            self.attachments = list(attachments)
        return self

    async def delete(self, *, reason: str | None = None) -> None:
        if self._fail_delete is not None:
            exc, self._fail_delete = self._fail_delete, None
            raise exc
        self.deleted = True


# --------------------------------------------------------------------------- #
# Channels / categories                                                       #
# --------------------------------------------------------------------------- #


def _make_text_channel(
    guild: FakeGuild,
    *,
    name: str,
    category: Any = None,
    overwrites: dict | None = None,
    id: int | None = None,
) -> Any:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = id if id is not None else next_id()
    ch.name = name
    ch.guild = guild
    ch.category = category
    ch.overwrites = dict(overwrites or {})
    ch.deleted = False
    ch._kind = "text"
    ch._messages = {}
    ch.sent = []
    ch._fail_send = None

    def fail_next_send(exc: Exception) -> None:
        ch._fail_send = exc

    ch.fail_next_send = fail_next_send

    async def _send(
        content=None, *, embed=None, embeds=None, view=None, file=None, files=None, **kwargs
    ):
        if ch._fail_send is not None:
            exc, ch._fail_send = ch._fail_send, None
            raise exc
        msg = FakeMessage(
            channel=ch,
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            file=file,
            files=files,
        )
        ch._messages[msg.id] = msg
        ch.sent.append(msg)
        return msg

    ch.send = _send

    async def _fetch_message(message_id):
        msg = ch._messages.get(message_id)
        if msg is None or msg.deleted:
            raise not_found(f"Unknown Message {message_id}")
        return msg

    ch.fetch_message = _fetch_message

    async def _delete(reason: str | None = None):
        ch.deleted = True
        if category is not None and ch in category.channels:
            category.channels.remove(ch)
        guild._channels.pop(ch.id, None)

    ch.delete = _delete

    return ch


def _make_voice_channel(
    guild: FakeGuild,
    *,
    name: str,
    category: Any = None,
    overwrites: dict | None = None,
    id: int | None = None,
) -> Any:
    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id = id if id is not None else next_id()
    ch.name = name
    ch.guild = guild
    ch.category = category
    ch.overwrites = dict(overwrites or {})
    ch.deleted = False
    ch._kind = "voice"

    async def _delete(reason: str | None = None):
        ch.deleted = True
        if category is not None and ch in category.channels:
            category.channels.remove(ch)
        guild._channels.pop(ch.id, None)

    ch.delete = _delete

    return ch


def _make_category(
    guild: FakeGuild, *, name: str, overwrites: dict | None = None, id: int | None = None
) -> Any:
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.id = id if id is not None else next_id()
    cat.name = name
    cat.guild = guild
    cat.overwrites = dict(overwrites or {})
    cat.channels = []
    cat.deleted = False
    cat._kind = "category"
    cat._fail_create_text = None
    cat._fail_create_voice = None

    def fail_next_create_text_channel(exc: Exception) -> None:
        cat._fail_create_text = exc

    cat.fail_next_create_text_channel = fail_next_create_text_channel

    async def _create_text_channel(name, *, overwrites=None, reason=None, **kwargs):
        if cat._fail_create_text is not None:
            exc, cat._fail_create_text = cat._fail_create_text, None
            raise exc
        ch = _make_text_channel(guild, name=name, category=cat, overwrites=overwrites)
        cat.channels.append(ch)
        guild._channels[ch.id] = ch
        return ch

    cat.create_text_channel = _create_text_channel

    async def _create_voice_channel(name, *, overwrites=None, reason=None, **kwargs):
        if cat._fail_create_voice is not None:
            exc, cat._fail_create_voice = cat._fail_create_voice, None
            raise exc
        ch = _make_voice_channel(guild, name=name, category=cat, overwrites=overwrites)
        cat.channels.append(ch)
        guild._channels[ch.id] = ch
        return ch

    cat.create_voice_channel = _create_voice_channel

    async def _delete(reason: str | None = None):
        cat.deleted = True
        guild._channels.pop(cat.id, None)

    cat.delete = _delete

    return cat


class _GuildMe:
    """guild.me stand-in — plain object (identity-hashable) rather than a
    SimpleNamespace, since it's used as a discord.PermissionOverwrite dict
    key in teams.py's category creation."""

    def __init__(self, *, id: int):
        self.id = id
        self.top_role: FakeRole | None = None


# --------------------------------------------------------------------------- #
# Guild                                                                        #
# --------------------------------------------------------------------------- #


class FakeGuild:
    """No isinstance(guild, discord.Guild) check exists anywhere in the cogs,
    so this is a plain class rather than a MagicMock(spec=...)."""

    def __init__(
        self,
        *,
        id: int | None = None,
        name: str = "Adjutant Test Guild",
        owner_id: int | None = None,
    ):
        self.id = id if id is not None else next_id()
        self.name = name
        self.owner_id = owner_id if owner_id is not None else next_id()
        self.roles: list[FakeRole] = []
        self.default_role = make_role("@everyone", position=0)
        self.roles.append(self.default_role)
        # Not a SimpleNamespace: it defines __eq__, which implicitly sets
        # __hash__ to None — and teams.py uses guild.me as a dict key when
        # building the category's permission overwrites.
        self.me = _GuildMe(id=next_id())
        self.me.top_role = make_role("bot-top", position=100)
        self._member_map: dict[int, Any] = {}
        self._channels: dict[int, Any] = {}
        self._fail_create_role: Exception | None = None
        self._fail_create_category: Exception | None = None

    # -- roles ------------------------------------------------------------
    def get_role(self, role_id: int) -> FakeRole | None:
        return next((r for r in self.roles if r.id == role_id), None)

    def fail_next_create_role(self, exc: Exception) -> None:
        self._fail_create_role = exc

    async def create_role(
        self, name: str, *, reason: str | None = None, mentionable: bool = False, **kwargs: Any
    ) -> FakeRole:
        if self._fail_create_role is not None:
            exc, self._fail_create_role = self._fail_create_role, None
            raise exc
        role = make_role(name, mentionable=mentionable)
        self.roles.append(role)
        return _attach_deletable(role, self)

    async def edit_role_positions(self, positions: dict, reason: str | None = None) -> None:
        for role, pos in positions.items():
            role.position = pos

    # -- members ------------------------------------------------------------
    def get_member(self, user_id: int) -> Any:
        return self._member_map.get(user_id)

    # -- channels ------------------------------------------------------------
    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    @property
    def text_channels(self) -> list[Any]:
        return [c for c in self._channels.values() if getattr(c, "_kind", None) == "text"]

    @property
    def categories(self) -> list[Any]:
        return [c for c in self._channels.values() if getattr(c, "_kind", None) == "category"]

    def fail_next_create_category(self, exc: Exception) -> None:
        self._fail_create_category = exc

    async def create_category(
        self, name: str, *, overwrites: dict | None = None, reason: str | None = None
    ) -> Any:
        if self._fail_create_category is not None:
            exc, self._fail_create_category = self._fail_create_category, None
            raise exc
        category = _make_category(self, name=name, overwrites=overwrites)
        self._channels[category.id] = category
        return category

    def create_standalone_text_channel(self, name: str = "general") -> Any:
        """A text channel not nested in any category — e.g. an audit-log
        channel, or the channel a slash command / event was posted in."""
        ch = _make_text_channel(self, name=name, category=None)
        self._channels[ch.id] = ch
        return ch


# --------------------------------------------------------------------------- #
# Member                                                                       #
# --------------------------------------------------------------------------- #


def make_member(
    guild: FakeGuild,
    *,
    member_id: int | None = None,
    display_name: str = "Tester",
    roles: Iterable[FakeRole] = (),
    is_admin: bool = False,
) -> Any:
    """MagicMock(spec=discord.Member) — the spec= is what makes
    `isinstance(member, discord.Member)` pass, which roles.py/admin.py/
    events.py all rely on."""
    member = MagicMock(spec=discord.Member)
    mid = member_id if member_id is not None else next_id()
    member.id = mid
    member.guild = guild
    member.bot = False
    member.roles = list(roles)
    member.mention = f"<@{mid}>"
    member.display_name = display_name
    member.name = display_name
    member.top_role = member.roles[-1] if member.roles else guild.default_role
    member.guild_permissions = discord.Permissions(administrator=is_admin)
    member.__str__ = lambda self=None: display_name  # type: ignore[assignment]

    async def _add_roles(*new_roles: FakeRole, reason: str | None = None) -> None:
        for r in new_roles:
            if r not in member.roles:
                member.roles.append(r)

    member.add_roles = _add_roles

    async def _remove_roles(*to_remove: FakeRole, reason: str | None = None) -> None:
        member.roles = [r for r in member.roles if r not in to_remove]

    member.remove_roles = _remove_roles

    async def _send(*args: Any, **kwargs: Any) -> FakeMessage:
        return FakeMessage(content=args[0] if args else kwargs.get("content"))

    member.send = _send

    guild._member_map[mid] = member
    return member


# --------------------------------------------------------------------------- #
# Bot                                                                          #
# --------------------------------------------------------------------------- #


class FakeBot:
    """Exposes exactly what cogs touch on `self.bot` / `interaction.client`:
    `.db` (a real migrated aiosqlite connection), `get_guild`, `get_channel`,
    `wait_until_ready` (never actually awaited by tests — background loops
    are invoked via their `.coro`, not started), and a no-op `add_view`."""

    def __init__(self, db: Any):
        self.db = db
        self._guilds: dict[int, FakeGuild] = {}
        self._cogs: dict[str, Any] = {}

    def register_guild(self, guild: FakeGuild) -> FakeGuild:
        self._guilds[guild.id] = guild
        return guild

    def register_cog(self, cog: Any) -> Any:
        """Registers `cog` under its `__cog_name__` (what real discord.py
        keys get_cog() by — the class name unless overridden via the
        `name=` kwarg passed to the Cog base). Needed by tests exercising
        cross-cog forwarding: admin.py's /admin fallbacks and hub.py's
        panel buttons both reach other cogs via `bot.get_cog(...)`."""
        name = getattr(type(cog), "__cog_name__", type(cog).__name__)
        self._cogs[name] = cog
        return cog

    def get_cog(self, name: str) -> Any:
        return self._cogs.get(name)

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self._guilds.get(guild_id)

    def get_channel(self, channel_id: int) -> Any:
        for guild in self._guilds.values():
            channel = guild._channels.get(channel_id)
            if channel is not None:
                return channel
        return None

    async def wait_until_ready(self) -> None:
        return None

    def add_view(self, view: discord.ui.View, *, message_id: int | None = None) -> None:
        pass


# --------------------------------------------------------------------------- #
# Interaction                                                                  #
# --------------------------------------------------------------------------- #


class FakeInteractionResponse:
    def __init__(self, interaction: FakeInteraction):
        self._interaction = interaction
        self._done = False
        self.deferred = False
        self.deferred_ephemeral: bool | None = None
        self.messages: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.modal: Any = None
        self.message: FakeMessage | None = None

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = True) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]
        self._done = True
        self.deferred = True
        self.deferred_ephemeral = ephemeral

    async def send_message(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]
        self._done = True
        msg = FakeMessage(
            channel=self._interaction.channel,
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            file=file,
            files=files,
        )
        self.messages.append(
            {
                "content": content,
                "embed": embed,
                "embeds": embeds,
                "view": view,
                "ephemeral": ephemeral,
            }
        )
        self.message = msg

    async def edit_message(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = "__unset__",  # type: ignore[assignment]
        attachments: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]
        self._done = True
        self.edits.append(
            {
                "content": content,
                "embed": embed,
                "embeds": embeds,
                "view": view,
                "attachments": attachments,
            }
        )
        target = self._interaction.message
        if target is not None:
            if content is not None:
                target.content = content
            if embed is not None:
                target.embed = embed
                target.embeds = [embed]
            if view != "__unset__":
                target.view = view

    async def send_modal(self, modal: Any) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]
        self._done = True
        self.modal = modal


class FakeFollowup:
    def __init__(self, interaction: FakeInteraction):
        self._interaction = interaction
        self.messages: list[dict[str, Any]] = []

    async def send(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
        **kwargs: Any,
    ) -> FakeMessage:
        msg = FakeMessage(
            channel=self._interaction.channel,
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            file=file,
            files=files,
        )
        self.messages.append(
            {
                "content": content,
                "embed": embed,
                "embeds": embeds,
                "view": view,
                "ephemeral": ephemeral,
                "message": msg,
            }
        )
        # Mirrors real discord.py: after response.defer(), the FIRST
        # followup.send() is what interaction.original_response() fetches
        # (the deferred ack is just a placeholder, not a message of its
        # own). Only the first — later followups are separate messages.
        if self._interaction.response.message is None:
            self._interaction.response.message = msg
        return msg


class FakeInteraction:
    """Stand-in for discord.Interaction. Not isinstance-checked anywhere in
    the cogs, so a plain class is fine."""

    def __init__(
        self,
        *,
        bot: FakeBot,
        guild: FakeGuild | None = None,
        user: Any = None,
        channel: Any = None,
        message: FakeMessage | None = None,
        command_name: str | None = None,
    ):
        self.client = bot
        self.guild = guild
        self.guild_id = guild.id if guild is not None else None
        self.user = user
        self.channel = channel
        self.channel_id = channel.id if channel is not None else None
        self.message = message
        self.command = SimpleNamespace(
            name=(command_name.rsplit(" ", 1)[-1] if command_name else None),
            qualified_name=command_name,
        )
        self.response = FakeInteractionResponse(self)
        self.followup = FakeFollowup(self)

    async def original_response(self) -> FakeMessage:
        if self.response.message is None:
            raise not_found("original response not found")
        return self.response.message


def make_interaction(
    bot: FakeBot,
    *,
    guild: FakeGuild | None = None,
    user: Any = None,
    channel: Any = None,
    message: FakeMessage | None = None,
    command_name: str | None = None,
) -> FakeInteraction:
    return FakeInteraction(
        bot=bot,
        guild=guild,
        user=user,
        channel=channel,
        message=message,
        command_name=command_name,
    )


# --------------------------------------------------------------------------- #
# Response inspection helpers                                                 #
# --------------------------------------------------------------------------- #


def all_replies(interaction: FakeInteraction) -> list[dict[str, Any]]:
    """Every user-facing reply on this interaction, response first then
    followups, in the order they were sent."""
    return list(interaction.response.messages) + list(interaction.followup.messages)


def last_reply(interaction: FakeInteraction) -> dict[str, Any] | None:
    replies = all_replies(interaction)
    return replies[-1] if replies else None


def reply_text(interaction: FakeInteraction) -> str:
    """Best-effort plain text of the last reply — content if there is any,
    else the embed's title + description."""
    call = last_reply(interaction)
    if call is None:
        return ""
    if call.get("content"):
        return call["content"]
    embed = call.get("embed")
    if embed is not None:
        title = embed.title or ""
        description = embed.description or ""
        return f"{title}\n{description}".strip()
    return ""


def reply_ephemeral(interaction: FakeInteraction) -> bool | None:
    call = last_reply(interaction)
    return call["ephemeral"] if call is not None else None


# --------------------------------------------------------------------------- #
# DB seeding                                                                   #
# --------------------------------------------------------------------------- #


async def seed_guild(
    conn: Any, guild_id: int, *, minimal_mode: bool = False, audit_channel: int | None = None
) -> None:
    """Every guild-scoped table has `guild_id REFERENCES guilds(guild_id)`
    and the connection runs with `PRAGMA foreign_keys=ON` — inserting a
    guilds row first is required, not optional, before writing ranks/teams/
    events/maps/role_grants rows."""
    await conn.execute(
        "INSERT INTO guilds (guild_id, minimal_mode, audit_channel) VALUES (?, ?, ?)",
        (guild_id, int(minimal_mode), audit_channel),
    )
    await conn.commit()


# --------------------------------------------------------------------------- #
# app_commands.check plumbing                                                 #
# --------------------------------------------------------------------------- #


async def run_checks(command: Any, interaction: FakeInteraction) -> bool:
    """Mirrors discord.app_commands.Command._check_can_run: run every
    registered check in order, short-circuiting on the first denial —
    same semantics discord.py itself uses (see discord.utils.async_all),
    so this exercises the real gating a live slash command would hit."""
    for check in command.checks:
        result = check(interaction)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            return False
    return True


def get_check(command: Any, factory_name: str) -> Callable:
    """Pull the predicate registered by one specific check-factory (e.g.
    'require_permission', 'require_admin', 'rate_limited') off a command's
    `.checks` list, identified by the factory's __qualname__ prefix — lets a
    test isolate one gate (e.g. just the rate limiter) without the others
    interfering."""
    for check in command.checks:
        qualname = getattr(check, "__qualname__", "")
        if qualname.startswith(f"{factory_name}."):
            return check
    raise LookupError(f"No check registered by {factory_name!r} on {command!r}")


# --------------------------------------------------------------------------- #
# Cog construction (cancels background loops so tests control them directly)  #
# --------------------------------------------------------------------------- #


async def build_roles_cog(bot: FakeBot):
    from adjutant.cogs.roles import RolesCog

    cog = RolesCog(bot)
    cog.expire_grants.cancel()
    await asyncio.sleep(0)
    return cog


async def build_events_cog(bot: FakeBot):
    from adjutant.cogs.events import EventsCog

    cog = EventsCog(bot)
    cog.event_ticker.cancel()
    await asyncio.sleep(0)
    return cog
