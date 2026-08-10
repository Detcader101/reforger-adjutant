"""/server — optional game-server integration.

Thin Discord adapter over adjutant/serverlink/: builds a ServerLink per
guild from the `server_links` table, keeps a live registry + a 60s status
poll, and maps slash commands onto the ServerLink interface. Every command
degrades politely on capability rather than erroring — see
docs/SERVER_INTEGRATION.md for the tier design.

Secret handling: RCON passwords and feed tokens are never taken as a slash-
command argument — Discord doesn't mask option values and the invocation
line is visible in-channel, so a `secret:` parameter would leak it to
anyone who can see the channel. RCON secrets are entered through a
`discord.ui.Modal` (private to the invoker); feed tokens are generated
server-side with `secrets.token_urlsafe` and shown once in the ephemeral
reply. `/server set-secret` re-opens the same flow for an existing link.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks

from .. import view_util, voice
from ..serverlink import NotSupported, NullLink, ServerLink, ServerStatus, Snapshot
from ..serverlink.feed import FeedLink, create_feed_app, feed_host, feed_port
from .admin import check_rate_limit, fetchone, minimal_mode, note_audit, rate_limited
from .roles import decline_admin_only, is_guild_admin

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 60

_BACKEND_CHOICES = [
    app_commands.Choice(name="None (unlink, Discord-only)", value="null"),
    app_commands.Choice(name="A2S (status only)", value="a2s"),
    app_commands.Choice(name="RCON (players + admin)", value="rcon"),
    app_commands.Choice(name="Feed (live map — needs the PlayerTelemetry mod)", value="feed"),
]

_DEFAULT_A2S_PORT = 17777
_DEFAULT_RCON_PORT = 19999


def build_link(backend: str, host: str | None, port: int | None, secret: str | None) -> ServerLink:
    """Construct the right ServerLink for a (backend, host, port, secret)
    tuple. Falls back to NullLink on anything under-specified rather than
    raising — a guild with a half-configured row should degrade, not crash
    the cog at load time."""
    if backend == "a2s" and host:
        from ..serverlink.a2s_link import A2SLink

        return A2SLink(host, port or _DEFAULT_A2S_PORT)
    if backend == "rcon" and host and secret:
        from ..serverlink.rcon_link import RconLink

        return RconLink(host, port or _DEFAULT_RCON_PORT, secret)
    if backend == "feed" and secret:
        return FeedLink()
    return NullLink()


class RconSecretModal(discord.ui.Modal, title="RCON Admin Password"):
    """Private entry point for an RCON password — a modal's field values
    aren't visible in the channel the way a slash-command argument is."""

    secret_input: discord.ui.TextInput = discord.ui.TextInput(
        label="RCON password", placeholder="From this server's config.json rcon block", max_length=200,
    )

    def __init__(self, cog: "ServerLinkCog", host: str, port: int | None):
        super().__init__()
        self.cog = cog
        self.host = host
        self.port = port

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.secret_input.value.strip()
        if not value:
            await interaction.response.send_message(
                voice.decline("That password was empty — nothing changed."), ephemeral=True
            )
            return
        await self.cog._apply_link(interaction, "rcon", self.host, self.port, value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class SecretEntryView(view_util.ErrorHandledView):
    """One-shot ephemeral prompt: a button that opens `RconSecretModal`,
    so the RCON password is entered privately rather than typed as a
    visible slash-command argument."""

    def __init__(self, cog: "ServerLinkCog", host: str, port: int | None, invoker_id: int, timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.host = host
        self.port = port
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(content="Timed out waiting for the password. Nothing changed.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Enter RCON password", style=discord.ButtonStyle.primary)
    async def enter_secret(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.send_modal(RconSecretModal(self.cog, self.host, self.port))


class LinkBackendModal(discord.ui.Modal, title="Link Game Server"):
    """Collects backend/host/port only — never the secret itself, which
    stays behind the existing SecretEntryView -> RconSecretModal flow (or
    is generated server-side for the feed tier). Re-running this with the
    same backend is also how a password/token gets rotated now — there's
    no separate set-secret surface any more."""

    backend_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Backend: null, a2s, rcon, or feed", placeholder="null"
    )
    host_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Host/IP (a2s, rcon only)", required=False
    )
    port_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Port (optional — defaults per tier)", required=False
    )

    def __init__(self, cog: "ServerLinkCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="server link")
            return
        backend_value = self.backend_input.value.strip().lower()
        if backend_value not in {c.value for c in _BACKEND_CHOICES}:
            await interaction.response.send_message(
                voice.decline(f"`{backend_value}` isn't a backend I know — try null, a2s, rcon, or feed."),
                ephemeral=True,
            )
            return
        host = self.host_input.value.strip() or None
        port_raw = self.port_input.value.strip()
        port: int | None = None
        if port_raw:
            try:
                port = int(port_raw)
            except ValueError:
                await interaction.response.send_message(voice.decline(f"`{port_raw}` isn't a whole number."), ephemeral=True)
                return
        if not await check_rate_limit(interaction, "server.link"):
            return
        await self.cog._do_link(interaction, backend_value, host, port)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class KickModal(discord.ui.Modal, title="Kick Player"):
    player_id_input: discord.ui.TextInput = discord.ui.TextInput(label="Player id — see the Players button")
    reason_input: discord.ui.TextInput = discord.ui.TextInput(label="Reason (optional)", required=False)

    def __init__(self, cog: "ServerLinkCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="server kick")
            return
        if not await check_rate_limit(interaction, "server.kick"):
            return
        await self.cog._do_kick(interaction, self.player_id_input.value.strip(), self.reason_input.value.strip() or None)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class ServerPanelView(view_util.ErrorHandledView):
    """Attached to every /server reply. Players is open to anyone, matching
    the original /server players' lack of gating; Link/Unlink/Kick recheck
    admin at click time before doing anything, matching the original
    require_admin()-gated commands they replace."""

    def __init__(self, cog: "ServerLinkCog", timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Players", style=discord.ButtonStyle.primary)
    async def players_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await check_rate_limit(interaction, "server.players"):
            return
        await self.cog._do_players(interaction)

    @discord.ui.button(label="Link…", style=discord.ButtonStyle.secondary)
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="server link")
            return
        await interaction.response.send_modal(LinkBackendModal(self.cog))

    @discord.ui.button(label="Unlink", style=discord.ButtonStyle.secondary)
    async def unlink_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="server unlink")
            return
        if not await check_rate_limit(interaction, "server.unlink"):
            return
        await self.cog._apply_link(interaction, "null", None, None, None)

    @discord.ui.button(label="Kick…", style=discord.ButtonStyle.danger)
    async def kick_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="server kick")
            return
        await interaction.response.send_modal(KickModal(self.cog))


class ServerLinkCog(commands.Cog, name="ServerLinkCog"):
    """/server — bare shows status; buttons cover link/unlink/players/kick."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.links: dict[int, ServerLink] = {}
        self._status_cache: dict[int, ServerStatus] = {}
        self._feed_runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        assert self.bot.db is not None
        rows = await self.bot.db.execute_fetchall("SELECT * FROM server_links")
        for row in rows:
            link = build_link(row["backend"], row["host"], row["port"], row["secret"])
            self.links[row["guild_id"]] = link
            try:
                await link.open()
            except Exception:
                log.warning("Failed to open %s link for guild %s at startup", row["backend"], row["guild_id"])
        self.poll_status.start()
        await self._sync_feed_listener()

    async def cog_unload(self) -> None:
        self.poll_status.cancel()
        await self._stop_feed_listener()
        for link in self.links.values():
            try:
                await link.close()
            except Exception:
                pass

    def _get_link(self, guild_id: int) -> ServerLink:
        return self.links.get(guild_id) or NullLink()

    async def _minimal(self, guild_id: int) -> bool:
        assert self.bot.db is not None
        return await minimal_mode(self.bot.db, guild_id)

    # -- background polling ------------------------------------------------

    @tasks.loop(seconds=POLL_INTERVAL_S)
    async def poll_status(self) -> None:
        for guild_id, link in list(self.links.items()):
            if isinstance(link, (NullLink, FeedLink)):
                continue  # feed's status is push-derived, not worth polling
            try:
                self._status_cache[guild_id] = await link.status()
            except Exception:
                log.warning("Status poll failed for guild %s", guild_id)

    @poll_status.before_loop
    async def before_poll_status(self) -> None:
        await self.bot.wait_until_ready()

    @poll_status.error
    async def on_poll_status_error(self, error: BaseException) -> None:
        # Per-guild failures are already handled above, so reaching here
        # means something unexpected escaped — restart rather than leave
        # every linked server's status frozen at its last poll.
        log.exception("Status poll loop failed; restarting it", exc_info=error)
        await asyncio.sleep(POLL_INTERVAL_S)
        self.poll_status.restart()

    # -- Tier 3 feed listener lifecycle -------------------------------------
    #
    # A single aiohttp listener serves every guild on the feed backend,
    # disambiguated by bearer token. It's only running while at least one
    # guild is configured that way.

    async def _feed_token_lookup(self, token: str) -> int | None:
        assert self.bot.db is not None
        row = await fetchone(
            self.bot.db,
            "SELECT guild_id FROM server_links WHERE backend = 'feed' AND secret = ?",
            (token,),
        )
        return row["guild_id"] if row else None

    async def _feed_on_snapshot(self, guild_id: int, snapshot: Snapshot) -> None:
        link = self.links.get(guild_id)
        if isinstance(link, FeedLink):
            await link.ingest(snapshot)

    def _any_guild_uses_feed(self) -> bool:
        return any(isinstance(link, FeedLink) for link in self.links.values())

    async def _sync_feed_listener(self) -> None:
        """Starts the listener if it's needed and not running; stops it if
        it's running and no longer needed. Call after any change to
        self.links."""
        if self._any_guild_uses_feed():
            if self._feed_runner is None:
                await self._start_feed_listener()
        else:
            await self._stop_feed_listener()

    async def _start_feed_listener(self) -> None:
        app = create_feed_app(self._feed_token_lookup, self._feed_on_snapshot)
        runner = web.AppRunner(app)
        await runner.setup()
        host, port = feed_host(), feed_port()
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except OSError as exc:
            log.warning("Couldn't bind the feed listener on %s:%s: %s", host, port, exc)
            await runner.cleanup()
            return
        self._feed_runner = runner
        log.info("Feed listener up on %s:%s", host, port)

    async def _stop_feed_listener(self) -> None:
        if self._feed_runner is not None:
            await self._feed_runner.cleanup()
            self._feed_runner = None
            log.info("Feed listener stopped")

    # -- link management ----------------------------------------------------

    async def _apply_link(
        self,
        interaction: discord.Interaction,
        backend: str,
        host: str | None,
        port: int | None,
        secret: str | None,
        *,
        reveal_secret: str | None = None,
    ) -> None:
        assert self.bot.db is not None and interaction.guild is not None
        guild_id = interaction.guild.id

        new_link = build_link(backend, host, port, secret)
        try:
            await new_link.open()
        except Exception as exc:
            log.warning("Failed to open %s link for guild %s: %s", backend, guild_id, exc)
            await interaction.response.send_message(
                voice.broken(
                    f"Couldn't establish a {backend} link.",
                    "Double-check the host, port, and secret, then try again.",
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.execute(
            "INSERT INTO server_links (guild_id, backend, host, port, secret) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "backend = excluded.backend, host = excluded.host, port = excluded.port, secret = excluded.secret",
            (guild_id, backend, host, port, secret),
        )
        await self.bot.db.commit()

        old_link = self.links.get(guild_id)
        self.links[guild_id] = new_link
        self._status_cache.pop(guild_id, None)
        if old_link is not None:
            try:
                await old_link.close()
            except Exception:
                pass

        await self._sync_feed_listener()

        await note_audit(self.bot, guild_id, f"Server link set to `{backend}` by <@{interaction.user.id}>.")
        label = "back to Discord-only" if backend == "null" else f"linked on the **{backend}** tier"
        message = f"Done. This server is now {label}."
        if reveal_secret:
            message += (
                f"\n\n**Feed token — shown once, copy it now:**\n`{reveal_secret}`\n"
                "Point the PlayerTelemetry mod's endpoint at this bot's feed URL (ask whoever "
                "manages its hosting for the public address behind the reverse proxy) with this "
                "as the bearer token. Lost it? Run `/server set-secret` to generate a new one."
            )
        await interaction.response.send_message(message, ephemeral=True)

    async def _do_link(
        self,
        interaction: discord.Interaction,
        backend_value: str,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        if backend_value == "null":
            await self._apply_link(interaction, "null", None, None, None)
            return

        if backend_value == "a2s":
            if not host:
                await interaction.response.send_message(
                    voice.decline("A2S needs a host to query."), ephemeral=True
                )
                return
            await self._apply_link(interaction, "a2s", host, port, None)
            return

        if backend_value == "rcon":
            if not host:
                await interaction.response.send_message(
                    voice.decline("RCON needs a host to connect to."), ephemeral=True
                )
                return
            view = SecretEntryView(self, host, port, interaction.user.id)
            await interaction.response.send_message(
                "One more step — the RCON password goes in a private form, never as a typed command argument.",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return

        # feed
        token = secrets.token_urlsafe(32)
        await self._apply_link(interaction, "feed", host, port, token, reveal_secret=token)

    # -- status / players / kick ------------------------------------------

    @app_commands.command(name="server", description="Show this server's game-server status.")
    @rate_limited()
    async def server(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        link = self._get_link(guild_id)
        minimal = await self._minimal(guild_id)

        result = self._status_cache.get(guild_id)
        if result is None:
            try:
                result = await link.status()
            except Exception:
                result = ServerStatus(
                    name="", scenario="", players=0, max_players=0,
                    reachable=False, detail="Couldn't check status just now.",
                )

        view = ServerPanelView(self)
        if not result.reachable:
            await interaction.response.send_message(
                embed=voice.embed("Server Status", result.detail or "Not reachable.", colour=voice.COLOUR_INFO, minimal=minimal),
                view=view,
            )
            view.message = await interaction.original_response()
            return

        lines = [f"**{result.name or 'Unnamed server'}**"]
        if result.scenario:
            lines.append(f"Scenario: {result.scenario}")
        lines.append(f"Players: {result.players}/{result.max_players}" if result.max_players else f"Players: {result.players}")
        if result.detail:
            lines.append(result.detail)
        await interaction.response.send_message(embed=voice.embed("Server Status", "\n".join(lines), minimal=minimal), view=view)
        view.message = await interaction.original_response()

    async def _do_players(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        link = self._get_link(guild_id)
        minimal = await self._minimal(guild_id)

        if not link.can_players:
            reason = "no server is linked" if isinstance(link, NullLink) else "this tier doesn't expose a player list"
            await interaction.response.send_message(
                voice.decline(f"Can't list players — {reason}. An RCON or feed link is needed for that."),
                ephemeral=True,
            )
            return

        try:
            roster = await link.players()
        except NotSupported as exc:
            await interaction.response.send_message(voice.decline(str(exc)), ephemeral=True)
            return
        except Exception:
            await interaction.response.send_message(
                voice.broken("Couldn't fetch the player list.", "The link may have dropped — try again shortly."),
                ephemeral=True,
            )
            return

        if not roster:
            await interaction.response.send_message(
                embed=voice.embed("Players", "Nobody's online.", minimal=minimal), ephemeral=True
            )
            return
        lines = "\n".join(f"`{p.player_id}` {p.name}" for p in roster)
        await interaction.response.send_message(
            embed=voice.embed(f"Players ({len(roster)})", lines, minimal=minimal), ephemeral=True
        )

    async def _do_kick(self, interaction: discord.Interaction, player_id: str, reason: str | None = None) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        link = self._get_link(guild_id)

        if not link.can_admin:
            reason_txt = "no server is linked" if isinstance(link, NullLink) else "this tier doesn't allow admin actions"
            await interaction.response.send_message(
                voice.decline(f"Can't kick — {reason_txt}. That needs an RCON link with admin permission."),
                ephemeral=True,
            )
            return

        try:
            await link.kick(player_id, reason or "")
        except NotSupported as exc:
            await interaction.response.send_message(voice.decline(str(exc)), ephemeral=True)
            return
        except Exception:
            await interaction.response.send_message(
                voice.broken("The kick didn't go through.", "Check the player id and try again."), ephemeral=True
            )
            return

        detail = f"Kicked player `{player_id}`" + (f" ({reason})" if reason else "")
        await note_audit(self.bot, guild_id, f"{detail} — <@{interaction.user.id}>.")
        await interaction.response.send_message(f"Done. {detail}.", ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # already messaged + logged inside the failing check
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLinkCog(bot))
