"""Events cog: create/announce/signup ops, reminders, and teardown.

One persistent SignupView instance handles every live event message — which
event a button press belongs to is resolved at interaction time by looking
up the clicked message's id against events.announce_message, the same
"one view, many messages" pattern the SPEC calls for. Everything that
mutates state (signup, withdraw, close, cancel, teardown) replies
ephemerally; the /event list roster and the announce message itself are the
only public surfaces.

Background loop (`event_ticker`, 60s) does two jobs: nudge events starting
within 30 minutes that haven't been reminded yet, and auto-teardown events
whose start has passed by more than 24 hours — same shape as roles.py's
expire_grants loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import view_util, voice
from ..services import events as events_service
from ..services import grants as grants_service
from ..services import ranks as ranks_service
from .admin import log_incident, minimal_mode, note_audit, rate_limited
from .roles import is_guild_admin, load_ladder, load_permission_overrides, require_permission

log = logging.getLogger(__name__)

_REMINDER_LEAD = timedelta(minutes=30)
_TEARDOWN_GRACE = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _unix(stored_starts_at: str) -> int:
    return int(events_service.stored_to_datetime(stored_starts_at).replace(tzinfo=timezone.utc).timestamp())


def build_announce_embed(event: events_service.Event, signup_count: int, *, minimal: bool = False) -> discord.Embed:
    """The event's announce-message embed. Shared by create, the live
    signup/withdraw button handlers, and the close/cancel/teardown edits, so
    the message always reflects current state."""
    unix = _unix(event.starts_at)
    lines: list[str] = []
    if event.status == "cancelled":
        lines.append("**This op has been cancelled.**")
    body = event.description.strip()
    if body:
        lines.append(body)
    lines.append(f"**Starts:** <t:{unix}:F> (<t:{unix}:R>)")
    if event.status != "cancelled":
        lines.append(f"**Signed up:** {signup_count}")
    if event.status == "closed":
        lines.append("_Signups are closed._")
    colour = voice.COLOUR_ALERT if event.status == "cancelled" else voice.COLOUR_PRIMARY
    return voice.embed(event.name, "\n".join(lines), colour=colour, minimal=minimal)


async def _can_manage_event(bot: commands.Bot, guild: discord.Guild, member: discord.Member, event: events_service.Event) -> bool:
    """Creator, or anyone holding events.create rank/permission (admins
    always pass via has_permission)."""
    if member.id == event.created_by:
        return True
    assert bot.db is not None
    ladder = await load_ladder(bot.db, guild.id)
    overrides = await load_permission_overrides(bot.db, guild.id)
    member_rank = ranks_service.resolve_rank((r.id for r in member.roles), ladder)
    min_rank = ranks_service.get_min_rank("events.create", overrides)
    return ranks_service.has_permission(member_rank, min_rank, is_guild_admin(member))


class SignupView(view_util.ErrorHandledView):
    """Persistent — registered once in cog_load, serves every live event
    announce message. Resolves which event a click belongs to via the
    clicked message's id."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _resolve_event(self, interaction: discord.Interaction) -> events_service.Event | None:
        assert self.bot.db is not None
        if interaction.message is None:
            return None
        return await events_service.get_event_by_announce_message(self.bot.db, interaction.message.id)

    async def _refresh_embed(self, interaction: discord.Interaction, event: events_service.Event) -> None:
        assert self.bot.db is not None
        count = len(await events_service.signups_for_event(self.bot.db, event.id))
        minimal = await minimal_mode(self.bot.db, event.guild_id)
        embed = build_announce_embed(event, count, minimal=minimal)
        try:
            await interaction.message.edit(embed=embed)
        except discord.HTTPException:
            log.warning("Failed to refresh announce message %s for event %s", interaction.message.id, event.id)

    @discord.ui.button(label="Sign up", style=discord.ButtonStyle.success, custom_id="event:signup")
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        member = interaction.user
        assert self.bot.db is not None
        if guild is None or not isinstance(member, discord.Member):
            return
        event = await self._resolve_event(interaction)
        if event is None:
            await interaction.response.send_message(
                voice.broken("Couldn't find that event.", "It may have been torn down — check `/event list`."),
                ephemeral=True,
            )
            return
        if event.status != "open":
            await interaction.response.send_message(voice.decline("Signups aren't open for this event."), ephemeral=True)
            return

        added = await events_service.add_signup(self.bot.db, event.id, member.id)
        if not added:
            await interaction.response.send_message("You're already signed up.", ephemeral=True)
            return

        if event.event_role_id is not None:
            role = guild.get_role(event.event_role_id)
            if role is not None:
                try:
                    await member.add_roles(role, reason="Adjutant: event signup")
                    await grants_service.record_grant(
                        self.bot.db, guild_id=guild.id, user_id=member.id, role_id=role.id,
                        kind="event", granted_by=member.id, event_id=event.id,
                    )
                except discord.Forbidden:
                    log.warning("Failed to grant event role %s to %s in guild %s", role.id, member.id, guild.id)

        await interaction.response.send_message(f"You're signed up for **{event.name}**.", ephemeral=True)
        await self._refresh_embed(interaction, event)

    @discord.ui.button(label="Withdraw", style=discord.ButtonStyle.secondary, custom_id="event:withdraw")
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        member = interaction.user
        assert self.bot.db is not None
        if guild is None or not isinstance(member, discord.Member):
            return
        event = await self._resolve_event(interaction)
        if event is None:
            await interaction.response.send_message(
                voice.broken("Couldn't find that event.", "It may have been torn down — check `/event list`."),
                ephemeral=True,
            )
            return

        removed = await events_service.remove_signup(self.bot.db, event.id, member.id)
        if not removed:
            await interaction.response.send_message("You're not signed up for this one.", ephemeral=True)
            return

        if event.event_role_id is not None:
            role = guild.get_role(event.event_role_id)
            if role is not None:
                try:
                    await member.remove_roles(role, reason="Adjutant: event withdraw")
                except discord.Forbidden:
                    log.warning("Failed to remove event role %s from %s in guild %s", role.id, member.id, guild.id)
            await grants_service.revoke_grant(self.bot.db, guild_id=guild.id, user_id=member.id, role_id=event.event_role_id)

        await interaction.response.send_message(f"Withdrawn from **{event.name}**.", ephemeral=True)
        await self._refresh_embed(interaction, event)


class EventsCog(commands.GroupCog, group_name="event"):
    """/event — ops/events: create, announce, signup, reminders, teardown."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.signup_view = SignupView(bot)
        self.event_ticker.start()

    async def cog_load(self) -> None:
        self.bot.add_view(self.signup_view)

    async def cog_unload(self) -> None:
        self.event_ticker.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)

    # -- shared plumbing -----------------------------------------------------

    async def _refresh_message(self, event: events_service.Event, *, clear_view: bool = False) -> None:
        assert self.bot.db is not None
        if event.announce_channel is None or event.announce_message is None:
            return
        channel = self.bot.get_channel(event.announce_channel)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(event.announce_message)
        except discord.NotFound:
            return
        count = len(await events_service.signups_for_event(self.bot.db, event.id))
        minimal = await minimal_mode(self.bot.db, event.guild_id)
        embed = build_announce_embed(event, count, minimal=minimal)
        try:
            if clear_view:
                await message.edit(embed=embed, view=None)
            else:
                await message.edit(embed=embed)
        except discord.HTTPException:
            log.warning("Failed to refresh announce message %s for event %s", message.id, event.id)

    async def _release_event_grants(self, guild: discord.Guild, event: events_service.Event) -> None:
        assert self.bot.db is not None
        for grant in await grants_service.grants_for_event(self.bot.db, event.id):
            member = guild.get_member(grant.user_id)
            role = guild.get_role(grant.role_id)
            if member is not None and role is not None:
                try:
                    await member.remove_roles(role, reason="Adjutant: event grants released")
                except discord.HTTPException:
                    log.warning("Failed to remove event role %s from %s in guild %s", role.id, member.id, guild.id)
            await grants_service.revoke_grant_by_id(self.bot.db, grant.id)
        if event.event_role_id is not None:
            role = guild.get_role(event.event_role_id)
            if role is not None:
                try:
                    await role.delete(reason="Adjutant: event grants released")
                except discord.HTTPException:
                    log.warning("Failed to delete event role %s in guild %s", event.event_role_id, guild.id)

    async def _teardown(self, guild: discord.Guild, event: events_service.Event) -> events_service.Event:
        assert self.bot.db is not None
        await self._release_event_grants(guild, event)
        try:
            return await events_service.set_status(self.bot.db, event.id, "done")
        except ValueError:
            return event  # already terminal (done/cancelled) — grants still got released above

    # -- commands --------------------------------------------------------------

    @app_commands.command(name="create", description="Create an event and open signups.")
    @app_commands.describe(
        name="Event name",
        start="When it starts — relative like '2h'/'3d', or absolute UTC 'YYYY-MM-DD HH:MM'",
        description="Optional details shown on the announcement",
    )
    @require_permission("events.create")
    @rate_limited()
    async def create(self, interaction: discord.Interaction, name: str, start: str, description: str = "") -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None

        try:
            start_dt = events_service.parse_start(start, _now())
        except ValueError as exc:
            await interaction.response.send_message(voice.decline(str(exc)), ephemeral=True)
            return

        channel = interaction.channel
        if channel is None:
            await interaction.response.send_message(
                voice.broken("I can't tell what channel this is.", "Try again from a normal text channel."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            role = await guild.create_role(
                name=f"Op: {name}", mentionable=True, reason=f"Adjutant: event create by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                voice.broken("I can't create roles.", "Check my role sits high enough and has Manage Roles."),
                ephemeral=True,
            )
            return

        event_id = await events_service.create_event(
            self.bot.db,
            guild_id=guild.id,
            name=name,
            description=description,
            starts_at=events_service.format_timestamp(start_dt),
            created_by=interaction.user.id,
            event_role_id=role.id,
        )
        event = await events_service.get_event(self.bot.db, event_id)
        assert event is not None
        minimal = await minimal_mode(self.bot.db, guild.id)
        embed = build_announce_embed(event, 0, minimal=minimal)

        try:
            message = await channel.send(embed=embed, view=self.signup_view)
        except discord.Forbidden:
            await interaction.followup.send(
                voice.broken("I can't post in this channel.", "Check my permissions here."), ephemeral=True
            )
            return

        await events_service.set_announce_message(self.bot.db, event_id, channel_id=channel.id, message_id=message.id)
        await note_audit(self.bot, guild.id, f"Event created by <@{interaction.user.id}>: **{name}** (#{event_id}).")
        await interaction.followup.send(f"Stood up **{name}** (#{event_id}) — announced above.", ephemeral=True)

    @app_commands.command(name="list", description="Show upcoming events.")
    async def list_events(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        upcoming = await events_service.list_upcoming(self.bot.db, guild.id, _now())
        minimal = await minimal_mode(self.bot.db, guild.id)
        if not upcoming:
            await interaction.response.send_message(embed=voice.embed("Events", "Nothing on the books.", minimal=minimal))
            return

        lines = []
        for event in upcoming:
            unix = _unix(event.starts_at)
            count = len(await events_service.signups_for_event(self.bot.db, event.id))
            note = " *(closed)*" if event.status == "closed" else ""
            lines.append(f"**#{event.id} {event.name}**{note} — <t:{unix}:F> (<t:{unix}:R>) — {count} signed up")
        await interaction.response.send_message(embed=voice.embed("Upcoming Events", "\n".join(lines), minimal=minimal))

    @app_commands.command(name="close", description="Close signups for an event.")
    @app_commands.describe(event_id="Event id, from /event list")
    async def close(self, interaction: discord.Interaction, event_id: int) -> None:
        guild = interaction.guild
        member = interaction.user
        assert guild is not None and self.bot.db is not None
        if not isinstance(member, discord.Member):
            return

        event = await events_service.get_event(self.bot.db, event_id)
        if event is None or event.guild_id != guild.id:
            await interaction.response.send_message(voice.decline(f"No event #{event_id} on record."), ephemeral=True)
            return
        if not await _can_manage_event(self.bot, guild, member, event):
            await log_incident(self.bot, guild.id, member.id, "permission_denied", detail="event close")
            await interaction.response.send_message(
                voice.decline("Only the organiser or events staff can close this."), ephemeral=True
            )
            return

        try:
            updated = await events_service.set_status(self.bot.db, event_id, "closed")
        except ValueError as exc:
            await interaction.response.send_message(voice.decline(str(exc)), ephemeral=True)
            return

        await self._refresh_message(updated)
        await note_audit(self.bot, guild.id, f"Event closed by <@{member.id}>: #{event_id} {event.name}.")
        await interaction.response.send_message(f"Closed signups for **{event.name}** (#{event_id}).", ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel an event and release its role grants.")
    @app_commands.describe(event_id="Event id, from /event list")
    async def cancel(self, interaction: discord.Interaction, event_id: int) -> None:
        guild = interaction.guild
        member = interaction.user
        assert guild is not None and self.bot.db is not None
        if not isinstance(member, discord.Member):
            return

        event = await events_service.get_event(self.bot.db, event_id)
        if event is None or event.guild_id != guild.id:
            await interaction.response.send_message(voice.decline(f"No event #{event_id} on record."), ephemeral=True)
            return
        if not await _can_manage_event(self.bot, guild, member, event):
            await log_incident(self.bot, guild.id, member.id, "permission_denied", detail="event cancel")
            await interaction.response.send_message(
                voice.decline("Only the organiser or events staff can cancel this."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._release_event_grants(guild, event)
        try:
            updated = await events_service.set_status(self.bot.db, event_id, "cancelled")
        except ValueError as exc:
            await interaction.followup.send(voice.decline(str(exc)), ephemeral=True)
            return

        await self._refresh_message(updated, clear_view=True)
        await note_audit(self.bot, guild.id, f"Event cancelled by <@{member.id}>: #{event_id} {event.name}.")
        await interaction.followup.send(f"Cancelled **{event.name}** (#{event_id}) — grants released.", ephemeral=True)

    @app_commands.command(name="teardown", description="Fully tear down an event: release grants, remove its role, mark done.")
    @app_commands.describe(event_id="Event id, from /event list")
    async def teardown(self, interaction: discord.Interaction, event_id: int) -> None:
        guild = interaction.guild
        member = interaction.user
        assert guild is not None and self.bot.db is not None
        if not isinstance(member, discord.Member):
            return

        event = await events_service.get_event(self.bot.db, event_id)
        if event is None or event.guild_id != guild.id:
            await interaction.response.send_message(voice.decline(f"No event #{event_id} on record."), ephemeral=True)
            return
        if not (is_guild_admin(member) or member.id == event.created_by):
            await log_incident(self.bot, guild.id, member.id, "permission_denied", detail="event teardown")
            await interaction.response.send_message(
                voice.decline("Only the organiser or an admin can tear this down."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        updated = await self._teardown(guild, event)
        await self._refresh_message(updated, clear_view=True)
        await note_audit(self.bot, guild.id, f"Event torn down by <@{member.id}>: #{event_id} {event.name}.")
        await interaction.followup.send(
            f"Torn down **{event.name}** (#{event_id}) — grants released, role removed.", ephemeral=True
        )

    # -- background loop ------------------------------------------------------

    @tasks.loop(seconds=60)
    async def event_ticker(self) -> None:
        assert self.bot.db is not None
        now = _now()

        for event in await events_service.events_due_reminder(self.bot.db, now, _REMINDER_LEAD):
            await self._send_reminder(event)
            await events_service.mark_reminded(self.bot.db, event.id)

        for event in await events_service.events_due_teardown(self.bot.db, now - _TEARDOWN_GRACE):
            guild = self.bot.get_guild(event.guild_id)
            if guild is None:
                continue
            updated = await self._teardown(guild, event)
            await self._refresh_message(updated, clear_view=True)
            await note_audit(self.bot, event.guild_id, f"Event auto-torn-down (24h past start): #{event.id} {event.name}.")

    @event_ticker.before_loop
    async def before_event_ticker(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_reminder(self, event: events_service.Event) -> None:
        if event.announce_channel is None:
            return
        channel = self.bot.get_channel(event.announce_channel)
        if channel is None:
            return
        unix = _unix(event.starts_at)
        mention = f"<@&{event.event_role_id}>" if event.event_role_id is not None else None
        minimal = await minimal_mode(self.bot.db, event.guild_id)
        embed = voice.embed(f"Reminder: {event.name}", f"Starts <t:{unix}:R> — <t:{unix}:F>.", minimal=minimal)
        try:
            await channel.send(content=mention, embed=embed)
        except discord.HTTPException:
            log.warning("Failed to send reminder for event %s in guild %s", event.id, event.guild_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
