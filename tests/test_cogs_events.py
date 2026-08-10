"""In-process integration tests for adjutant/cogs/events.py: event creation,
the persistent SignupView, teardown, and the reminder/auto-teardown ticker.
"""
from __future__ import annotations

import pytest

from adjutant.cogs.events import EventsCog, SignupView
from adjutant.services import events as events_service
from adjutant.services import grants as grants_service
from fakes import (
    FakeGuild,
    build_events_cog,
    make_interaction,
    make_member,
    reply_ephemeral,
    reply_text,
    seed_guild,
)


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
async def cog(bot):
    return await build_events_cog(bot)


@pytest.fixture
def channel(guild):
    return guild.create_standalone_text_channel("ops")


async def _create_event(cog, bot, guild, channel, creator, *, name="Op Flashpoint", start="2h", description=""):
    interaction = make_interaction(bot, guild=guild, user=creator, channel=channel, command_name="event create")
    await EventsCog.create.callback(cog, interaction, name=name, start=start, description=description)
    row = (
        await bot.db.execute_fetchall("SELECT * FROM events WHERE guild_id = ? AND name = ?", (guild.id, name))
    )[0]
    return row, interaction


# --------------------------------------------------------------------------- #
# /event create                                                               #
# --------------------------------------------------------------------------- #

async def test_create_writes_row_creates_op_role_and_posts_announce_with_signup_view(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")

    row, interaction = await _create_event(cog, bot, guild, channel, creator, description="Push the ridge.")

    role = next(r for r in guild.roles if r.name == "Op: Op Flashpoint")
    assert row["event_role_id"] == role.id
    assert role.mentionable is True

    posted = channel.sent[-1]
    assert posted.view is cog.signup_view
    assert row["announce_channel"] == channel.id
    assert row["announce_message"] == posted.id
    assert row["status"] == "open"
    assert reply_ephemeral(interaction) is True
    assert "#" + str(row["id"]) in reply_text(interaction) or str(row["id"]) in reply_text(interaction)


# --------------------------------------------------------------------------- #
# SignupView                                                                   #
# --------------------------------------------------------------------------- #

async def test_signup_grants_role_records_signup_and_refreshes_the_embed(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    row, _ = await _create_event(cog, bot, guild, channel, creator)
    announce = channel.sent[-1]
    role = guild.get_role(row["event_role_id"])
    recruit = make_member(guild, display_name="Recruit")

    click = make_interaction(bot, guild=guild, user=recruit, message=announce)
    await SignupView.signup(cog.signup_view, click, None)

    assert role in recruit.roles
    signups = await events_service.signups_for_event(bot.db, row["id"])
    assert recruit.id in signups
    assert "signed up" in reply_text(click).lower()
    assert announce.edits, "the announce embed should have been edited in place"
    assert "1" in announce.embed.description


async def test_signup_is_idempotent_on_a_second_click(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    row, _ = await _create_event(cog, bot, guild, channel, creator)
    announce = channel.sent[-1]
    recruit = make_member(guild, display_name="Recruit")

    await SignupView.signup(cog.signup_view, make_interaction(bot, guild=guild, user=recruit, message=announce), None)
    second_click = make_interaction(bot, guild=guild, user=recruit, message=announce)
    await SignupView.signup(cog.signup_view, second_click, None)

    assert "already signed up" in reply_text(second_click).lower()
    signups = await events_service.signups_for_event(bot.db, row["id"])
    assert signups.count(recruit.id) == 1


async def test_withdraw_reverses_signup_and_removes_the_role(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    row, _ = await _create_event(cog, bot, guild, channel, creator)
    announce = channel.sent[-1]
    role = guild.get_role(row["event_role_id"])
    recruit = make_member(guild, display_name="Recruit")
    await SignupView.signup(cog.signup_view, make_interaction(bot, guild=guild, user=recruit, message=announce), None)

    withdraw_click = make_interaction(bot, guild=guild, user=recruit, message=announce)
    await SignupView.withdraw(cog.signup_view, withdraw_click, None)

    assert role not in recruit.roles
    signups = await events_service.signups_for_event(bot.db, row["id"])
    assert recruit.id not in signups
    assert "withdrawn" in reply_text(withdraw_click).lower()
    assert "0" in announce.embed.description


async def test_withdraw_when_not_signed_up_declines_without_erroring(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    _, _ = await _create_event(cog, bot, guild, channel, creator)
    announce = channel.sent[-1]
    bystander = make_member(guild, display_name="Bystander")

    click = make_interaction(bot, guild=guild, user=bystander, message=announce)
    await SignupView.withdraw(cog.signup_view, click, None)

    assert "not signed up" in reply_text(click).lower()


# --------------------------------------------------------------------------- #
# teardown                                                                     #
# --------------------------------------------------------------------------- #

async def test_teardown_releases_every_grant_strips_roles_and_deletes_the_op_role(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    row, _ = await _create_event(cog, bot, guild, channel, creator)
    announce = channel.sent[-1]
    role = guild.get_role(row["event_role_id"])
    recruits = [make_member(guild, display_name=f"Recruit{i}") for i in range(3)]
    for recruit in recruits:
        await SignupView.signup(cog.signup_view, make_interaction(bot, guild=guild, user=recruit, message=announce), None)
    for recruit in recruits:
        assert role in recruit.roles

    teardown_interaction = make_interaction(bot, guild=guild, user=creator, command_name="event teardown")
    await EventsCog.teardown.callback(cog, teardown_interaction, event_id=row["id"])

    for recruit in recruits:
        assert role not in recruit.roles
    assert role.id not in {r.id for r in guild.roles}, "the Op role should have been deleted"
    assert await grants_service.grants_for_event(bot.db, row["id"]) == []
    updated = await events_service.get_event(bot.db, row["id"])
    assert updated.status == "done"
    assert reply_ephemeral(teardown_interaction) is True


# --------------------------------------------------------------------------- #
# background ticker: reminders                                                #
# --------------------------------------------------------------------------- #

async def test_reminder_ticker_reminds_only_due_events_and_will_not_double_remind(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    due_row, _ = await _create_event(cog, bot, guild, channel, creator, name="Due Soon", start="20m")
    later_row, _ = await _create_event(cog, bot, guild, channel, creator, name="Much Later", start="6h")
    channel.sent.clear()

    await EventsCog.event_ticker.coro(cog)

    due_after = await events_service.get_event(bot.db, due_row["id"])
    later_after = await events_service.get_event(bot.db, later_row["id"])
    assert due_after.reminded == 1
    assert later_after.reminded == 0
    reminder_titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert any("Due Soon" in t for t in reminder_titles)
    assert not any("Much Later" in t for t in reminder_titles)

    channel.sent.clear()
    await EventsCog.event_ticker.coro(cog)

    due_after_second_tick = await events_service.get_event(bot.db, due_row["id"])
    assert due_after_second_tick.reminded == 1
    assert channel.sent == [], "an already-reminded event must not be reminded again"


# --------------------------------------------------------------------------- #
# /event cancel — permission gating                                           #
# --------------------------------------------------------------------------- #

async def test_cancel_from_a_non_creator_without_permission_is_refused(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    row, _ = await _create_event(cog, bot, guild, channel, creator)
    outsider = make_member(guild, display_name="Outsider")  # no rank, not admin, not the creator

    interaction = make_interaction(bot, guild=guild, user=outsider, command_name="event cancel")
    await EventsCog.cancel.callback(cog, interaction, event_id=row["id"])

    assert reply_ephemeral(interaction) is True
    assert "organiser" in reply_text(interaction).lower() or "events staff" in reply_text(interaction).lower()
    updated = await events_service.get_event(bot.db, row["id"])
    assert updated.status == "open"
    incidents = await bot.db.execute_fetchall("SELECT * FROM incidents WHERE guild_id = ?", (guild.id,))
    assert any(i["kind"] == "permission_denied" for i in incidents)


async def test_cancel_by_the_creator_releases_grants_and_clears_the_view(cog, bot, guild, channel):
    await seed_guild(bot.db, guild.id)
    creator = make_member(guild, display_name="Creator")
    row, _ = await _create_event(cog, bot, guild, channel, creator)
    announce = channel.sent[-1]
    role = guild.get_role(row["event_role_id"])
    recruit = make_member(guild, display_name="Recruit")
    await SignupView.signup(cog.signup_view, make_interaction(bot, guild=guild, user=recruit, message=announce), None)

    interaction = make_interaction(bot, guild=guild, user=creator, command_name="event cancel")
    await EventsCog.cancel.callback(cog, interaction, event_id=row["id"])

    assert role not in recruit.roles
    updated = await events_service.get_event(bot.db, row["id"])
    assert updated.status == "cancelled"
    assert announce.edits[-1]["view"] is None
    assert reply_ephemeral(interaction) is True
