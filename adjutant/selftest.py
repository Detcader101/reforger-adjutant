"""Live self-test harness — exercises Adjutant against a real Discord guild.

Run with ``python -m adjutant.selftest [--guild <id>] [--keep]``. See
``docs/SELFTEST.md`` for what each check covers and how to read a failure.

Design in one paragraph: this logs in as the real bot (the same
``AdjutantBot`` class production runs), loads the real cogs, and drives them
against a scratch guild using an isolated sqlite file (never the real
``data/adjutant.db``). Each check is an independent async function that
creates real Discord objects (always named with the ``selftest-`` prefix),
verifies real behaviour, and registers its own cleanup with the shared
``Harness``. Cleanup runs in reverse-creation order from a ``finally`` block,
so a crash mid-run still tidies up. Checks call into the cogs' own module-
level helper functions and cog instance methods wherever those exist
(``_save_guild_config``, ``_create_default_ladder``, ``_disband_team``,
``EventsCog._teardown``, the ``services/*`` modules, ...) rather than
reimplementing their logic, so a real bug in that code shows up here.
Team *creation* has no standalone helper to call (unlike disband) — that
path is replicated inline from ``TeamsCog.create``; see docs/SELFTEST.md.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import discord

# Must happen before Config.load() reads the environment, so the harness
# never touches the real database even if .env points ADJUTANT_DB there.
# python-dotenv's load_dotenv(override=False) default means this wins.
os.environ.setdefault("ADJUTANT_DB", "data/selftest.db")

from . import db as database  # noqa: E402
from .bot import AdjutantBot  # noqa: E402
from .config import Config  # noqa: E402
from .services import events as events_service  # noqa: E402
from .services import grants as grants_service  # noqa: E402
from .services import mapping as mapping_service  # noqa: E402
from . import voice  # noqa: E402

log = logging.getLogger("adjutant.selftest")

HARNESS_PREFIX = "selftest-"
_CREATE_PACE = 0.6  # seconds between bulk create calls, to be gentle on rate limits


# --------------------------------------------------------------------------- #
# Harness plumbing                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    duration: float = 0.0


@dataclass
class Harness:
    """Shared state + cleanup registry passed to every check."""

    bot: AdjutantBot
    guild: discord.Guild
    keep: bool = False
    cleanup: list[tuple[str, Callable[[], Awaitable[None]]]] = field(default_factory=list)
    created_role_ids: set[int] = field(default_factory=set)
    created_channel_ids: set[int] = field(default_factory=set)
    state: dict[str, Any] = field(default_factory=dict)

    def register(self, label: str, coro_fn: Callable[[], Awaitable[None]]) -> None:
        self.cleanup.append((label, coro_fn))

    async def run_cleanup(self) -> list[str]:
        if self.keep:
            return [f"skipped — --keep set ({len(self.cleanup)} item(s) left in place)"]
        summary: list[str] = []
        while self.cleanup:
            label, fn = self.cleanup.pop()  # reverse of creation order
            try:
                await fn()
                summary.append(f"ok    {label}")
            except Exception as exc:
                summary.append(f"FAIL  {label}: {type(exc).__name__}: {exc}")
                log.warning("cleanup failed for %s", label, exc_info=True)
        return summary

    async def delete_role(self, role: discord.Role, *, reason: str = "Adjutant selftest cleanup") -> None:
        assert role.id in self.created_role_ids, (
            f"refusing to delete role {role.id} ({role.name!r}) — not in this harness's created-role registry"
        )
        current = self.guild.get_role(role.id)
        if current is not None:
            await _retry_forbidden(lambda: current.delete(reason=reason), what=f"delete role {role.name!r}")

    async def delete_channel(self, channel: discord.abc.GuildChannel, *, reason: str = "Adjutant selftest cleanup") -> None:
        assert channel.id in self.created_channel_ids, (
            f"refusing to delete channel {channel.id} ({channel.name!r}) — not in this harness's created-channel registry"
        )
        current = self.guild.get_channel(channel.id)
        if current is not None:
            await _retry_forbidden(lambda: current.delete(reason=reason), what=f"delete channel {channel.name!r}")


async def _pace() -> None:
    await asyncio.sleep(_CREATE_PACE)


async def _retry_forbidden(
    coro_factory: Callable[[], Awaitable[Any]], *, attempts: int = 4, base_delay: float = 2.5, what: str = "discord API call"
) -> Any:
    """Discord intermittently 403s ("Missing Access"/"Missing Permissions")
    on channel/role create+delete calls when a lot of them happen in a short
    window — a real, undocumented anti-abuse throttle, confirmed by isolated
    repro (identical calls with full permissions succeed once traffic cools).
    It doesn't carry normal rate-limit headers, so the only reliable
    mitigation is retry-with-backoff. This wraps the harness's own bulk
    create/delete calls so that throttle doesn't masquerade as a cog bug."""
    last_exc: discord.Forbidden | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except discord.Forbidden as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            delay = base_delay * attempt
            log.warning(
                "%s hit a transient Forbidden (attempt %d/%d) — likely Discord's channel/role churn "
                "throttle, not a real permission gap. Retrying in %.1fs: %s",
                what, attempt, attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 6.0, interval: float = 0.3) -> bool:
    """Discord's REST calls return before the gateway event that updates
    the local cache arrives — guild.get_role/get_channel can lag a create
    or delete by a beat. Poll instead of trusting the first read."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return predicate()
        await asyncio.sleep(interval)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Checks                                                                       #
# --------------------------------------------------------------------------- #


async def check_connectivity(h: Harness) -> str:
    guild = h.guild
    assert h.bot.user is not None, "bot has no user — login apparently didn't complete."
    try:
        me = await guild.fetch_member(h.bot.user.id)
    except discord.NotFound:
        raise AssertionError(
            f"Bot user {h.bot.user.id} isn't a member of guild {guild.id}. "
            "Remedy: invite the bot to the test guild (OAuth2 URL from the Discord developer portal, "
            "'bot' + 'applications.commands' scopes) and re-run."
        ) from None
    h.state["me"] = me

    perms = me.guild_permissions
    missing = [name for name in ("manage_roles", "manage_channels") if not getattr(perms, name)]
    role_note = f"bot's top role: {me.top_role.name!r} @ position {me.top_role.position} (of {len(guild.roles)} roles total)"

    if missing:
        raise AssertionError(
            f"Missing permission(s): {', '.join(missing)}. {role_note}. "
            "Remedy: Server Settings > Roles > (the bot's role) > grant Manage Roles and Manage Channels."
        )
    if me.top_role.id == guild.default_role.id:
        raise AssertionError(
            f"The bot holds no role above @everyone ({role_note}) — it has no real position in the hierarchy "
            "even though its permission bits look fine, so role/channel management will fail unpredictably. "
            "Remedy: Server Settings > Roles — drag the bot's own role above at least one other role."
        )
    return f"OK — manage_roles + manage_channels present. {role_note}."


async def check_database(h: Harness) -> str:
    assert h.bot.db is not None, "bot.db is None — setup_hook apparently didn't run."
    migrations = database.discover_migrations()
    assert migrations, "No migration files discovered under adjutant/migrations/."
    highest_file = max(number for number, _ in migrations)
    async with h.bot.db.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
        row = await cur.fetchone()
    applied = row["v"] if row else None
    assert applied == highest_file, (
        f"schema_version reports max version {applied}, but the highest migration file present is "
        f"{highest_file:04d}_*.sql — migrations didn't fully apply."
    )
    return f"schema at version {applied}, {len(migrations)} migration file(s) discovered and applied."


async def check_guild_config(h: Harness) -> str:
    from .cogs.admin import fetchone
    from .cogs.setup import _save_guild_config

    guild_id = h.guild.id
    features = {"teams", "events", "map"}
    h.state["features"] = features
    h.state["minimal_mode"] = False

    async def _cleanup_guild_row() -> None:
        # Cascades (ON DELETE CASCADE) to ranks/permissions/role_grants/teams/
        # events/maps/server_links — a last-resort net for anything an earlier
        # check's own cleanup missed. incidents has no FK here (by design,
        # per teardown's comment) so it's cleaned separately in its own check.
        await h.bot.db.execute("DELETE FROM guilds WHERE guild_id = ?", (guild_id,))
        await h.bot.db.commit()

    # Registered first == runs LAST in the reverse-order cleanup stack, i.e.
    # after every other check's own cleanup has already had a shot.
    h.register(f"guilds row ({guild_id})", _cleanup_guild_row)

    await _save_guild_config(h.bot, guild_id, minimal_mode=False, audit_channel_id=None, features=features)
    row = await fetchone(h.bot.db, "SELECT * FROM guilds WHERE guild_id = ?", (guild_id,))
    assert row is not None, "guilds row wasn't written by _save_guild_config."
    assert bool(row["minimal_mode"]) is False, f"minimal_mode round-tripped wrong: {row['minimal_mode']!r}."
    stored_features = json.loads(row["features"] or "{}")
    enabled = {k for k, v in stored_features.items() if v}
    assert enabled == features, f"features round-tripped wrong: stored {enabled}, expected {features}."
    return f"guilds row written via _save_guild_config and read back; features={sorted(features)}."


async def check_rank_ladder(h: Harness) -> str:
    from .cogs.setup import DEFAULT_LADDER, _create_default_ladder

    # _create_default_ladder swallows discord.Forbidden internally and just
    # `break`s, returning a partial list rather than raising — so a
    # transient Forbidden (Discord's create/delete-churn throttle, see
    # _retry_forbidden's docstring) mid-loop under-provisions the ladder
    # instead of erroring. Retry the whole call once after a cooldown before
    # treating a short result as a real failure.
    created = await _create_default_ladder(h.bot, h.guild)
    if len(created) < len(DEFAULT_LADDER):
        log.warning(
            "_create_default_ladder only returned %d/%d roles — likely the churn throttle rather than a real "
            "permission gap (connectivity check already passed). Cooling down and retrying once.",
            len(created), len(DEFAULT_LADDER),
        )
        for role in created:
            current = h.guild.get_role(role.id)
            if current is not None:
                try:
                    await current.delete(reason="Adjutant selftest: retrying rank_ladder")
                except discord.HTTPException:
                    pass
        await asyncio.sleep(5.0)
        created = await _create_default_ladder(h.bot, h.guild)
    assert len(created) == len(DEFAULT_LADDER), (
        f"only {len(created)}/{len(DEFAULT_LADDER)} default ladder roles got created, even after a retry — "
        "likely a genuine permission wall partway through (see the connectivity check)."
    )
    for role in created:
        h.created_role_ids.add(role.id)
        h.register(f"rank role {role.name!r}", (lambda r=role: h.delete_role(r)))

    for role in created:
        present = await _wait_until(lambda r=role: h.guild.get_role(r.id) is not None)
        assert present, f"role {role.name!r} ({role.id}) never showed up in the guild's role cache."

    rows = await h.bot.db.execute_fetchall(
        "SELECT role_id FROM ranks WHERE guild_id = ? AND bot_created = 1", (h.guild.id,)
    )
    row_role_ids = {r["role_id"] for r in rows}
    expected_ids = {r.id for r in created}
    assert row_role_ids == expected_ids, (
        f"ranks table doesn't match the roles just created — db has {row_role_ids}, expected {expected_ids}."
    )
    return f"created + verified {len(created)} ladder roles: {', '.join(r.name for r in created)}."


async def check_role_grant_lifecycle(h: Harness) -> str:
    guild = h.guild
    bot_member = h.state.get("me") or await guild.fetch_member(h.bot.user.id)

    role = await _retry_forbidden(
        lambda: guild.create_role(name=f"{HARNESS_PREFIX}temp-role", reason="Adjutant selftest"), what="create temp role"
    )
    await _pace()
    h.created_role_ids.add(role.id)
    h.register(f"temp role {role.name!r}", (lambda: h.delete_role(role)))

    await bot_member.add_roles(role, reason="Adjutant selftest: role_grant_lifecycle")

    past = grants_service.format_timestamp(_now() - timedelta(seconds=5))
    grant_id = await grants_service.record_grant(
        h.bot.db,
        guild_id=guild.id,
        user_id=bot_member.id,
        role_id=role.id,
        kind="temp",
        granted_by=bot_member.id,
        expires_at=past,
    )
    h.register(f"role_grants row #{grant_id}", (lambda: grants_service.revoke_grant_by_id(h.bot.db, grant_id)))

    fresh = await guild.fetch_member(bot_member.id)
    assert role.id in {r.id for r in fresh.roles}, "role.add_roles reported success but the member doesn't hold it."

    due = await grants_service.due_expiries(h.bot.db, _now())
    assert any(g.id == grant_id for g in due), (
        "due_expiries didn't return the grant despite an expires_at 5s in the past — "
        "check the timestamp format grants.py expects."
    )

    roles_cog = h.bot.get_cog("RolesCog")
    assert roles_cog is not None, "RolesCog isn't loaded on the bot (check COGS in bot.py / extension load errors)."
    # Calls the exact coroutine the 60s tasks.loop invokes (Loop.__call__ auto-
    # injects the cog as self) — not the loop's scheduling machinery.
    await roles_cog.expire_grants()

    fresh_after = await guild.fetch_member(bot_member.id)
    assert role.id not in {r.id for r in fresh_after.roles}, (
        "expire_grants() ran but the role is still on the member — expiry sweep didn't remove it."
    )
    async with h.bot.db.execute("SELECT id FROM role_grants WHERE id = ?", (grant_id,)) as cur:
        remaining = await cur.fetchone()
    assert remaining is None, "expire_grants() ran but the role_grants row is still present."
    return "temp grant applied, matured (past expiry), swept via expire_grants(), role + DB row confirmed gone."


async def check_team_lifecycle(h: Harness) -> str:
    from .cogs.admin import fetchone
    from .cogs.teams import _disband_team, build_team

    guild = h.guild
    name = f"{HARNESS_PREFIX}team"
    me = h.state.get("me") or await guild.fetch_member(h.bot.user.id)

    # Drives the same build_team() that /team create uses. Replicating its
    # body here once meant the harness kept passing its own copy of a bug
    # the real command still had — so this check is only meaningful while it
    # imports rather than mirrors.
    def _track(built: list) -> None:
        """Register everything created for cleanup, newest deleted first."""
        for obj in built:
            if isinstance(obj, discord.Role):
                h.created_role_ids.add(obj.id)
                h.register(f"team role {obj.name!r}", (lambda o=obj: h.delete_role(o)))
            else:
                h.created_channel_ids.add(obj.id)
                h.register(f"team channel {obj.name!r}", (lambda o=obj: h.delete_channel(o)))

    try:
        role, category, built = await _retry_forbidden(
            lambda: build_team(guild, name, reason="Adjutant selftest"), what="build team"
        )
    except (discord.Forbidden, discord.HTTPException) as error:
        # Whatever got made before the failure still needs cleaning up.
        _track(getattr(error, "built", []))
        raise
    _track(built)
    await _pace()

    text_channel, voice_channel = built[2], built[3]

    cursor = await h.bot.db.execute(
        "INSERT INTO teams (guild_id, name, role_id, category_id) VALUES (?, ?, ?, ?)",
        (guild.id, name, role.id, category.id),
    )
    await h.bot.db.commit()
    team_row_id = cursor.lastrowid

    async def _cleanup_if_still_there() -> None:
        row = await fetchone(h.bot.db, "SELECT * FROM teams WHERE id = ?", (team_row_id,))
        if row is not None:
            assert row["role_id"] in h.created_role_ids and row["category_id"] in h.created_channel_ids
            await _disband_team(h.bot, guild, dict(row))

    h.register(f"team {name!r} (id={team_row_id})", _cleanup_if_still_there)

    # -- verify creation --------------------------------------------------
    everyone_ow = category.overwrites_for(guild.default_role)
    team_ow = category.overwrites_for(role)
    assert everyone_ow.view_channel is False, "category doesn't deny @everyone view_channel — leak risk."
    assert team_ow.view_channel is True, "category doesn't grant the team role view_channel."
    db_row = await fetchone(h.bot.db, "SELECT * FROM teams WHERE id = ?", (team_row_id,))
    assert db_row is not None
    assert db_row["role_id"] == role.id and db_row["category_id"] == category.id, "teams row doesn't match what was created."

    # -- disband via the real shared path ----------------------------------
    assert role.id in h.created_role_ids and category.id in h.created_channel_ids
    await _retry_forbidden(lambda: _disband_team(h.bot, guild, dict(db_row)), what="_disband_team")

    role_gone = await _wait_until(lambda: guild.get_role(role.id) is None)
    category_gone = await _wait_until(lambda: guild.get_channel(category.id) is None)
    text_gone = await _wait_until(lambda: guild.get_channel(text_channel.id) is None)
    voice_gone = await _wait_until(lambda: guild.get_channel(voice_channel.id) is None)
    assert role_gone, "role still present after _disband_team."
    assert category_gone, "category still present after _disband_team."
    assert text_gone, "text channel still present after _disband_team."
    assert voice_gone, "voice channel still present after _disband_team."
    gone_row = await fetchone(h.bot.db, "SELECT id FROM teams WHERE id = ?", (team_row_id,))
    assert gone_row is None, "teams DB row still present after _disband_team."
    return f"team {name!r} created (role + category + 2 channels, overwrites verified), disbanded via _disband_team, confirmed gone."


async def check_event_lifecycle(h: Harness) -> str:
    from .cogs.admin import fetchone
    from .cogs.events import build_announce_embed

    guild = h.guild
    channel = h.state.get("events_channel")
    assert channel is not None, "harness events channel wasn't created — see the harness-setup log line / connectivity check."
    me = h.state.get("me") or await guild.fetch_member(h.bot.user.id)

    role = await _retry_forbidden(
        lambda: guild.create_role(name=f"Op: {HARNESS_PREFIX}event", mentionable=True, reason="Adjutant selftest"),
        what="create event role",
    )
    await _pace()
    h.created_role_ids.add(role.id)
    h.register(f"event role {role.name!r}", (lambda: h.delete_role(role)))

    start_dt = _now() + timedelta(minutes=40)
    event_id = await events_service.create_event(
        h.bot.db,
        guild_id=guild.id,
        name=f"{HARNESS_PREFIX}event",
        description="Adjutant selftest event",
        starts_at=events_service.format_timestamp(start_dt),
        created_by=me.id,
        event_role_id=role.id,
    )

    async def _del_event_row() -> None:
        await h.bot.db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await h.bot.db.commit()

    h.register(f"events row #{event_id}", _del_event_row)

    event = await events_service.get_event(h.bot.db, event_id)
    assert event is not None

    events_cog = h.bot.get_cog("EventsCog")
    assert events_cog is not None, "EventsCog isn't loaded on the bot."

    embed = build_announce_embed(event, 0)
    message = await channel.send(embed=embed, view=events_cog.signup_view)
    await events_service.set_announce_message(h.bot.db, event_id, channel_id=channel.id, message_id=message.id)
    assert len(message.components) > 0, "announce message has no components — the SignupView wasn't attached."

    # Simulates the button press by calling the exact service functions
    # SignupView.signup's callback calls (add_signup + grants_service.record_grant).
    added = await events_service.add_signup(h.bot.db, event_id, me.id)
    assert added, "add_signup reported the bot was already signed up on a brand-new event."
    await me.add_roles(role, reason="Adjutant selftest: event signup")
    await grants_service.record_grant(
        h.bot.db, guild_id=guild.id, user_id=me.id, role_id=role.id, kind="event", granted_by=me.id, event_id=event_id,
    )

    signups = await events_service.signups_for_event(h.bot.db, event_id)
    assert me.id in signups, "event_signups row missing after add_signup."
    fresh = await guild.fetch_member(me.id)
    assert role.id in {r.id for r in fresh.roles}, "event role wasn't actually granted for the simulated signup."

    updated = await _retry_forbidden(lambda: events_cog._teardown(guild, event), what="EventsCog._teardown")
    assert updated.status == "done", f"expected status 'done' after _teardown, got {updated.status!r}."
    remaining_grants = await grants_service.grants_for_event(h.bot.db, event_id)
    assert not remaining_grants, "event-scoped grants still present after _teardown."
    role_gone = await _wait_until(lambda: guild.get_role(role.id) is None)
    assert role_gone, "Op role still present after _teardown."
    row_after = await fetchone(h.bot.db, "SELECT status FROM events WHERE id = ?", (event_id,))
    assert row_after is not None and row_after["status"] == "done"
    return f"event #{event_id} created + announced with SignupView, signed up, torn down via EventsCog._teardown, verified clean."


async def check_map_render(h: Harness) -> str:
    from .cogs.admin import fetchone

    guild = h.guild
    channel = h.state.get("map_channel")
    assert channel is not None, "harness map channel wasn't created — see the harness-setup log line / connectivity check."

    markers = [
        mapping_service.Marker(kind="objective", label="Obj A", x=2300, z=8700),
        mapping_service.Marker(kind="enemy", label="Contact", x=4100, z=3000),
        mapping_service.Marker(kind="friendly", label="FOB", x=1000, z=1000),
    ]
    data = mapping_service.render_to_png_bytes("everon", markers)
    assert len(data) > 0, "render_to_png_bytes returned an empty payload."

    file = discord.File(io.BytesIO(data), filename="map-everon.png")
    message = await channel.send(file=file)
    assert message.attachments and message.attachments[0].size > 0, "uploaded map attachment reports zero/no size."

    cursor = await h.bot.db.execute(
        "INSERT INTO maps (guild_id, channel_id, message_id, terrain) VALUES (?, ?, ?, ?)",
        (guild.id, channel.id, message.id, "everon"),
    )
    await h.bot.db.commit()
    map_id = cursor.lastrowid

    async def _del_map_row() -> None:
        await h.bot.db.execute("DELETE FROM maps WHERE id = ?", (map_id,))
        await h.bot.db.commit()

    h.register(f"maps row #{map_id}", _del_map_row)

    map_row = await fetchone(h.bot.db, "SELECT * FROM maps WHERE id = ?", (map_id,))
    assert map_row is not None

    map_cog = h.bot.get_cog("MapCog")
    assert map_cog is not None, "MapCog isn't loaded on the bot."

    more_markers = markers + [mapping_service.Marker(kind="note", label="Extra", x=6000, z=6000)]
    new_data = mapping_service.render_to_png_bytes("everon", more_markers)
    assert new_data != data, "re-render with an extra marker produced byte-identical output — rendering may be a no-op."
    await map_cog._refresh_message(channel, map_row, new_data)

    refreshed = await channel.fetch_message(message.id)
    assert refreshed.attachments and refreshed.attachments[0].size > 0, "attachment missing/empty after in-place edit."
    return f"rendered {len(markers)}-marker PNG ({len(data)}B), uploaded, re-rendered + edited in place via MapCog._refresh_message."


async def check_serverlink_null(h: Harness) -> str:
    from .serverlink import NullLink
    from .serverlink.null import NOT_LINKED_DETAIL

    serverlink_cog = h.bot.get_cog("ServerLinkCog")
    assert serverlink_cog is not None, "ServerLinkCog isn't loaded on the bot."

    link = serverlink_cog._get_link(h.guild.id)
    assert isinstance(link, NullLink), f"expected NullLink for an unconfigured guild, got {type(link).__name__}."

    status = await link.status()
    assert status.reachable is False, "NullLink reported reachable=True."
    assert status.detail == NOT_LINKED_DETAIL, f"unexpected detail text: {status.detail!r}."

    # /server status's own formatting branch for an unreachable result —
    # the smallest slice of the command not gated behind a live Interaction.
    minimal = await serverlink_cog._minimal(h.guild.id)
    embed = voice.embed("Server Status", status.detail or "Not reachable.", colour=voice.COLOUR_INFO, minimal=minimal)
    assert embed.description, "status formatting path produced an empty embed description."
    return "NullLink reports unreachable courteously; /server status's unreachable-formatting branch didn't raise."


async def check_audit_and_incident(h: Harness) -> str:
    from .cogs.admin import fetchone, log_incident, note_audit
    from .cogs.setup import _save_guild_config

    guild = h.guild
    channel = h.state.get("audit_channel")
    assert channel is not None, "harness audit channel wasn't created — see the harness-setup log line / connectivity check."
    me = h.state.get("me") or await guild.fetch_member(h.bot.user.id)

    features = h.state.get("features", {"teams", "events", "map"})
    minimal_mode = h.state.get("minimal_mode", False)
    await _save_guild_config(h.bot, guild.id, minimal_mode=minimal_mode, audit_channel_id=channel.id, features=features)

    marker = f"{HARNESS_PREFIX}audit-check-{int(time.time())}"
    await note_audit(h.bot, guild.id, marker)
    history = [m async for m in channel.history(limit=10)]
    assert any(marker in (m.content or "") for m in history), "note_audit didn't post the expected message to the audit channel."

    detail = f"{HARNESS_PREFIX}incident-check"

    async def _del_incident_rows() -> None:
        await h.bot.db.execute(
            "DELETE FROM incidents WHERE guild_id = ? AND user_id = ? AND detail = ?", (guild.id, me.id, detail)
        )
        await h.bot.db.commit()

    # incidents has no guilds FK (by design — see cogs/setup.py's _teardown
    # comment), so it survives the guilds-row cascade cleanup and needs its
    # own explicit cleanup here.
    h.register(f"incidents row(s) ({detail!r})", _del_incident_rows)

    await log_incident(h.bot, guild.id, me.id, "permission_denied", detail=detail)
    row = await fetchone(
        h.bot.db,
        "SELECT * FROM incidents WHERE guild_id = ? AND user_id = ? AND detail = ? ORDER BY id DESC",
        (guild.id, me.id, detail),
    )
    assert row is not None, "log_incident didn't write an incidents row."

    history2 = [m async for m in channel.history(limit=10)]
    assert any(str(me.id) in (m.content or "") for m in history2), (
        "log_incident's audit-channel mirror message wasn't found (it should post on 'permission_denied')."
    )
    return "note_audit + log_incident both landed in the harness audit channel; incidents row verified and will be cleaned up."


CHECKS: tuple[Callable[[Harness], Awaitable[str]], ...] = (
    check_database,
    check_guild_config,
    check_rank_ladder,
    check_role_grant_lifecycle,
    check_team_lifecycle,
    check_event_lifecycle,
    check_map_render,
    check_serverlink_null,
    check_audit_and_incident,
)


# --------------------------------------------------------------------------- #
# Harness channel setup                                                       #
# --------------------------------------------------------------------------- #


async def _prepare_harness_channels(h: Harness) -> None:
    """Creates one harness category with three text channels the later
    checks post into. Best-effort: if this fails (e.g. missing
    Manage Channels), later checks that need a channel report that clearly
    rather than crashing on a None."""
    guild = h.guild
    try:
        category = await _retry_forbidden(
            lambda: guild.create_category(name=f"{HARNESS_PREFIX}harness", reason="Adjutant selftest"),
            what="create harness category",
        )
        await _pace()
    except discord.HTTPException as exc:
        log.warning("Couldn't create the harness category — checks needing a channel will fail: %s", exc)
        return

    h.created_channel_ids.add(category.id)
    h.register(f"harness category {category.name!r}", (lambda: h.delete_channel(category)))

    for state_key, short_name in (("events_channel", "events"), ("map_channel", "map"), ("audit_channel", "audit")):
        try:
            ch = await _retry_forbidden(
                lambda short_name=short_name: category.create_text_channel(name=f"{HARNESS_PREFIX}{short_name}"),
                what=f"create harness channel {short_name!r}",
            )
            await _pace()
        except discord.HTTPException as exc:
            log.warning("Couldn't create harness channel %r: %s", short_name, exc)
            continue
        h.created_channel_ids.add(ch.id)
        h.register(f"harness channel #{ch.name}", (lambda c=ch: h.delete_channel(c)))
        h.state[state_key] = ch


# --------------------------------------------------------------------------- #
# Runner + report                                                             #
# --------------------------------------------------------------------------- #


def _short_name(fn: Callable[[Harness], Awaitable[str]]) -> str:
    return fn.__name__.removeprefix("check_")


async def _run_check(fn: Callable[[Harness], Awaitable[str]], h: Harness) -> CheckResult:
    name = _short_name(fn)
    start = time.monotonic()
    try:
        detail = await fn(h)
        return CheckResult(name=name, passed=True, detail=str(detail), duration=time.monotonic() - start)
    except AssertionError as exc:
        return CheckResult(name=name, passed=False, detail=str(exc), duration=time.monotonic() - start)
    except Exception as exc:
        detail = f"{type(exc).__module__}.{type(exc).__name__}: {exc}"
        log.warning("check %s raised", name, exc_info=True)
        return CheckResult(name=name, passed=False, detail=detail, duration=time.monotonic() - start)


def print_report(results: list[CheckResult], cleanup_summary: list[str]) -> None:
    print()
    print("Adjutant self-test report")
    print("=" * 72)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"{status}  {r.name:<24} {r.duration:6.2f}s")
        if not r.passed or r.detail:
            for line in (str(r.detail).splitlines() or [""]):
                print(f"        {line}")
    print("-" * 72)
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"{passed}/{total} checks passed.")
    print()
    print("Cleanup:")
    if not cleanup_summary:
        print("  (nothing registered)")
    for line in cleanup_summary:
        print(f"  {line}")
    print()


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live self-test harness for Adjutant — see docs/SELFTEST.md.")
    parser.add_argument("--guild", type=int, default=None, help="Guild id to run against (default: first DEV_GUILD_IDS entry).")
    parser.add_argument("--keep", action="store_true", help="Skip cleanup — leave everything created for manual inspection.")
    parser.add_argument(
        "--db", default=os.environ.get("ADJUTANT_DB", "data/selftest.db"),
        help="Path to the isolated selftest database (default: data/selftest.db — never the real DB).",
    )
    return parser.parse_args(argv)


async def main() -> int:
    args = parse_args()
    os.environ["ADJUTANT_DB"] = args.db  # must land before Config.load()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)

    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    guild_id = args.guild or (config.dev_guild_ids[0] if config.dev_guild_ids else None)
    if guild_id is None:
        print(
            "No guild id given. Pass --guild <id>, or set DEV_GUILD_IDS in .env.",
            file=sys.stderr,
        )
        return 1

    log.info("Using selftest DB at %s, target guild %s", config.db_path, guild_id)

    bot = AdjutantBot(config)
    ready_event = asyncio.Event()

    @bot.listen("on_ready")
    async def _mark_ready() -> None:
        ready_event.set()

    bot_task = asyncio.create_task(bot.start(config.token))

    try:
        ready_wait = asyncio.ensure_future(ready_event.wait())
        done, _pending = await asyncio.wait({ready_wait, bot_task}, timeout=60, return_when=asyncio.FIRST_COMPLETED)
        if bot_task in done and not ready_event.is_set():
            # start() returned/raised before we ever got READY — surface why.
            exc = bot_task.exception()
            print(f"Bot failed to start: {exc!r}", file=sys.stderr)
            return 1
        if not ready_event.is_set():
            print("Timed out waiting for the bot to log in and become ready (60s).", file=sys.stderr)
            bot_task.cancel()
            return 1
    except discord.LoginFailure as exc:
        print(f"Login failed — check DISCORD_TOKEN in .env: {exc}", file=sys.stderr)
        return 1

    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except discord.HTTPException:
            guild = None

    results: list[CheckResult] = []
    cleanup_summary: list[str] = []

    try:
        if guild is None:
            results.append(CheckResult(
                name="connectivity", passed=False,
                detail=(
                    f"Guild {guild_id} not found or the bot isn't a member of it. "
                    "Remedy: invite the bot to the test guild and confirm DEV_GUILD_IDS / --guild is correct."
                ),
            ))
        else:
            h = Harness(bot=bot, guild=guild, keep=args.keep)
            results.append(await _run_check(check_connectivity, h))
            await _prepare_harness_channels(h)
            for fn in CHECKS:
                results.append(await _run_check(fn, h))
            cleanup_summary = await h.run_cleanup()
    finally:
        await bot.close()
        try:
            await asyncio.wait_for(bot_task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    print_report(results, cleanup_summary)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
