"""In-process integration tests for adjutant/cogs/config.py.

/config's old slash surface is gone — its methods are now plain (no
app_commands.command wrapper, no built-in admin check) and are reached
only via /adjutant's Config button (`panel`) or /admin's raw fallbacks
(`feature`/`audit_channel`/`minimal`/`permission`/`reset`); the admin
recheck now lives at those call sites (see test_cogs_admin.py and
test_cogs_hub.py), not inside this cog. Every assertion here checks that a
change actually landed in the DB (not just that the reply sounded right),
and that refusals genuinely change nothing.

Note on simulating typed input: discord.ui.TextInput.value has no public
setter (Discord fills it in when a real user submits a modal), so modal
tests below poke the private `_value` backing attribute directly — the
same trick the library itself uses internally when it deserialises a
modal-submit interaction.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from discord import app_commands

from adjutant.cogs.config import (
    ConfigCog,
    ConfigPanelView,
    PermissionThresholdModal,
    ResetConfirmModal,
    ResetConfirmView,
)
from fakes import (
    FakeGuild,
    make_interaction,
    make_member,
    make_role,
    next_id,
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
def cog(bot):
    return ConfigCog(bot)


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


async def _add_rank(bot, guild, name: str, position: int):
    role = make_role(name)
    guild.roles.append(role)
    await bot.db.execute(
        "INSERT INTO ranks (guild_id, role_id, position, name, bot_created) VALUES (?, ?, ?, ?, 0)",
        (guild.id, role.id, position, name),
    )
    await bot.db.commit()
    return role


async def _open_panel(cog, bot, guild, admin):
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config panel")
    await cog.panel(interaction)
    view = interaction.response.messages[-1]["view"]
    return view, interaction.response.message


# --------------------------------------------------------------------------- #
# show (called from the panel button — no direct slash command any more)      #
# --------------------------------------------------------------------------- #

async def test_show_reflects_stored_config(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _add_rank(bot, guild, "NCO", 2)
    await bot.db.execute(
        "UPDATE guilds SET features = ? WHERE guild_id = ?", (json.dumps({"teams": True}), guild.id)
    )
    await bot.db.commit()
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config show")

    await cog.show(interaction)

    text = reply_text(interaction).lower()
    assert "teams: on" in text
    assert "events: off" in text
    assert "nco" in text
    assert reply_ephemeral(interaction) is True


# --------------------------------------------------------------------------- #
# feature (called from /admin feature)                                       #
# --------------------------------------------------------------------------- #

async def test_toggling_a_feature_on_persists_and_is_idempotent(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    feature = app_commands.Choice(name="Teams", value="teams")
    on = app_commands.Choice(name="on", value="on")

    for _ in range(2):
        interaction = make_interaction(bot, guild=guild, user=admin, command_name="config feature")
        await cog.feature(interaction, feature=feature, state=on)

    rows = await bot.db.execute_fetchall("SELECT features FROM guilds WHERE guild_id = ?", (guild.id,))
    assert json.loads(rows[0]["features"]) == {"teams": True}


async def test_toggling_a_feature_off_persists(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await bot.db.execute(
        "UPDATE guilds SET features = ? WHERE guild_id = ?", (json.dumps({"teams": True}), guild.id)
    )
    await bot.db.commit()
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config feature")

    await cog.feature(
        interaction,
        feature=app_commands.Choice(name="Teams", value="teams"),
        state=app_commands.Choice(name="off", value="off"),
    )

    rows = await bot.db.execute_fetchall("SELECT features FROM guilds WHERE guild_id = ?", (guild.id,))
    assert json.loads(rows[0]["features"]) == {"teams": False}


# --------------------------------------------------------------------------- #
# audit_channel (called from /admin audit-channel)                           #
# --------------------------------------------------------------------------- #

async def test_audit_channel_can_be_set_then_cleared(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    channel = guild.create_standalone_text_channel(name="audit-log")

    set_interaction = make_interaction(bot, guild=guild, user=admin, command_name="config audit-channel")
    await cog.audit_channel(set_interaction, channel=channel)
    rows = await bot.db.execute_fetchall("SELECT audit_channel FROM guilds WHERE guild_id = ?", (guild.id,))
    assert rows[0]["audit_channel"] == channel.id

    clear_interaction = make_interaction(bot, guild=guild, user=admin, command_name="config audit-channel")
    await cog.audit_channel(clear_interaction, channel=None)
    rows2 = await bot.db.execute_fetchall("SELECT audit_channel FROM guilds WHERE guild_id = ?", (guild.id,))
    assert rows2[0]["audit_channel"] is None
    assert "cleared" in reply_text(clear_interaction).lower()


# --------------------------------------------------------------------------- #
# permission (called from /admin permission)                                 #
# --------------------------------------------------------------------------- #

async def test_permission_declines_an_unrecognised_key_and_changes_nothing(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config permission")

    await cog.permission(interaction, key="bogus.permission", min_rank=1)

    assert "recognised" in reply_text(interaction).lower()
    assert reply_ephemeral(interaction) is True
    rows = await bot.db.execute_fetchall("SELECT * FROM permissions WHERE guild_id = ?", (guild.id,))
    assert rows == []


async def test_permission_declines_a_rank_not_on_the_ladder_and_changes_nothing(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _add_rank(bot, guild, "NCO", 2)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config permission")

    await cog.permission(interaction, key="teams.manage", min_rank=99)

    assert "isn't on this guild's ladder" in reply_text(interaction).lower()
    assert reply_ephemeral(interaction) is True
    rows = await bot.db.execute_fetchall("SELECT * FROM permissions WHERE guild_id = ?", (guild.id,))
    assert rows == []


async def test_permission_sets_a_valid_threshold(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _add_rank(bot, guild, "Officer", 3)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config permission")

    await cog.permission(interaction, key="teams.manage", min_rank=3)

    rows = await bot.db.execute_fetchall(
        "SELECT * FROM permissions WHERE guild_id = ? AND permission = ?", (guild.id, "teams.manage")
    )
    assert len(rows) == 1
    assert rows[0]["min_rank"] == 3
    assert "Officer" in reply_text(interaction)


# --------------------------------------------------------------------------- #
# reset (called from /admin reset)                                           #
# --------------------------------------------------------------------------- #

async def test_reset_restores_default_thresholds_but_leaves_ranks_and_teams_intact(cog, bot, guild):
    await seed_guild(bot.db, guild.id, minimal_mode=True)
    role = await _add_rank(bot, guild, "Officer", 3)
    await bot.db.execute(
        "INSERT INTO permissions (guild_id, permission, min_rank) VALUES (?, ?, ?)",
        (guild.id, "teams.manage", 3),
    )
    await bot.db.execute(
        "INSERT INTO teams (guild_id, name, role_id, category_id) VALUES (?, ?, ?, ?)",
        (guild.id, "Alpha", role.id, next_id()),
    )
    await bot.db.commit()
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="config reset")

    await cog.reset(interaction)
    view = interaction.response.messages[-1]["view"]
    assert isinstance(view, ResetConfirmView)

    click = make_interaction(bot, guild=guild, user=admin, message=interaction.response.message)
    await ResetConfirmView.proceed(view, click, None)
    modal = click.response.modal
    assert isinstance(modal, ResetConfirmModal)
    modal.guild_name_input._value = guild.name

    submit_interaction = make_interaction(bot, guild=guild, user=admin)
    await modal.on_submit(submit_interaction)

    assert "reset complete" in reply_text(submit_interaction).lower()
    perm_rows = await bot.db.execute_fetchall("SELECT * FROM permissions WHERE guild_id = ?", (guild.id,))
    assert perm_rows == []
    guild_rows = await bot.db.execute_fetchall("SELECT minimal_mode FROM guilds WHERE guild_id = ?", (guild.id,))
    assert guild_rows[0]["minimal_mode"] == 0
    rank_rows = await bot.db.execute_fetchall("SELECT * FROM ranks WHERE guild_id = ?", (guild.id,))
    assert len(rank_rows) == 1
    team_rows = await bot.db.execute_fetchall("SELECT * FROM teams WHERE guild_id = ?", (guild.id,))
    assert len(team_rows) == 1


async def test_reset_declines_on_a_mismatched_name_and_changes_nothing(cog, bot, guild):
    await seed_guild(bot.db, guild.id, minimal_mode=True)
    await bot.db.execute(
        "INSERT INTO permissions (guild_id, permission, min_rank) VALUES (?, ?, ?)",
        (guild.id, "teams.manage", 3),
    )
    await bot.db.commit()
    admin = _admin(guild)
    modal = ResetConfirmModal(bot, guild)
    modal.guild_name_input._value = "Not The Right Name"

    submit_interaction = make_interaction(bot, guild=guild, user=admin)
    await modal.on_submit(submit_interaction)

    assert "cancelled" in reply_text(submit_interaction).lower()
    perm_rows = await bot.db.execute_fetchall("SELECT * FROM permissions WHERE guild_id = ?", (guild.id,))
    assert len(perm_rows) == 1
    guild_rows = await bot.db.execute_fetchall("SELECT minimal_mode FROM guilds WHERE guild_id = ?", (guild.id,))
    assert guild_rows[0]["minimal_mode"] == 1


# --------------------------------------------------------------------------- #
# /config panel                                                               #
# --------------------------------------------------------------------------- #

async def test_panel_feature_select_toggles_features_live_and_rerenders(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    view, message = await _open_panel(cog, bot, guild, admin)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await ConfigPanelView.feature_select(view, click, SimpleNamespace(values=["teams", "map"]))

    rows = await bot.db.execute_fetchall("SELECT features FROM guilds WHERE guild_id = ?", (guild.id,))
    assert json.loads(rows[0]["features"]) == {"teams": True, "events": False, "map": True, "serverlink": False}
    assert message.embed is not None
    assert "teams: on" in message.embed.description.lower()


async def test_panel_audit_select_sets_channel_and_clear_button_removes_it(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    view, message = await _open_panel(cog, bot, guild, admin)
    channel = guild.create_standalone_text_channel(name="audit-log")

    set_click = make_interaction(bot, guild=guild, user=admin, message=message)
    await ConfigPanelView.audit_select(view, set_click, SimpleNamespace(values=[channel]))
    rows = await bot.db.execute_fetchall("SELECT audit_channel FROM guilds WHERE guild_id = ?", (guild.id,))
    assert rows[0]["audit_channel"] == channel.id

    clear_click = make_interaction(bot, guild=guild, user=admin, message=message)
    await ConfigPanelView.clear_audit_channel(view, clear_click, None)
    rows2 = await bot.db.execute_fetchall("SELECT audit_channel FROM guilds WHERE guild_id = ?", (guild.id,))
    assert rows2[0]["audit_channel"] is None


async def test_panel_minimal_toggle_flips_minimal_mode_and_relabels_the_button(cog, bot, guild):
    await seed_guild(bot.db, guild.id, minimal_mode=False)
    admin = _admin(guild)
    view, message = await _open_panel(cog, bot, guild, admin)

    click = make_interaction(bot, guild=guild, user=admin, message=message)
    await ConfigPanelView.minimal_toggle(view, click, None)

    rows = await bot.db.execute_fetchall("SELECT minimal_mode FROM guilds WHERE guild_id = ?", (guild.id,))
    assert rows[0]["minimal_mode"] == 1
    assert view.minimal_toggle.label == "Minimal mode: ON"


async def test_panel_permission_modal_declines_bad_key_then_saves_a_valid_one(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    await _add_rank(bot, guild, "Officer", 3)
    admin = _admin(guild)
    view, message = await _open_panel(cog, bot, guild, admin)

    open_click = make_interaction(bot, guild=guild, user=admin, message=message)
    await ConfigPanelView.set_permission(view, open_click, None)
    modal = open_click.response.modal
    assert isinstance(modal, PermissionThresholdModal)

    modal.permission_key_input._value = "bogus.key"
    modal.min_rank_input._value = "3"
    bad_interaction = make_interaction(bot, guild=guild, user=admin, message=message)
    await modal.on_submit(bad_interaction)
    assert "recognised" in reply_text(bad_interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT * FROM permissions WHERE guild_id = ?", (guild.id,))
    assert rows == []

    modal.permission_key_input._value = "teams.manage"
    modal.min_rank_input._value = "3"
    good_interaction = make_interaction(bot, guild=guild, user=admin, message=message)
    await modal.on_submit(good_interaction)
    rows2 = await bot.db.execute_fetchall(
        "SELECT * FROM permissions WHERE guild_id = ? AND permission = ?", (guild.id, "teams.manage")
    )
    assert len(rows2) == 1
    assert rows2[0]["min_rank"] == 3


async def test_panel_is_locked_to_the_invoker(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    view, message = await _open_panel(cog, bot, guild, admin)
    someone_else = make_member(guild, display_name="Bystander")

    click = make_interaction(bot, guild=guild, user=someone_else, message=message)
    allowed = await view.interaction_check(click)

    assert allowed is False
    assert reply_ephemeral(click) is True
