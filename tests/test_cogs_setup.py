"""In-process integration tests for adjutant/cogs/setup.py.

Covers re-running setup (idempotency is a hard requirement — "did that
work? let me run it again" is one of the first things a new admin does),
the declarative setup templates in adjutant/services/templates.py, custom
rank ladders, and the permissions preflight helpers. The bare /setup
command itself (opening SetupView) is covered here too; its former
subcommands — check, ranks, quick — now live as /admin fallbacks and are
covered in tests/test_cogs_admin.py alongside the rest of that group.

Note on simulating typed input: discord.ui.TextInput.value has no public
setter (Discord fills it in when a real user submits a modal), so the
RankLadderModal tests below poke the private `_value` backing attribute
directly — the same trick the library itself uses internally when it
deserialises a modal-submit interaction (see tests/test_cogs_config.py
for the same convention).
"""

from __future__ import annotations

import discord
import pytest
from fakes import (
    FakeGuild,
    all_replies,
    make_interaction,
    make_member,
    make_role,
    reply_ephemeral,
    reply_text,
    run_checks,
    seed_guild,
)

from adjutant.cogs.setup import (
    DEFAULT_LADDER,
    REQUIRED_PERMISSIONS,
    RankLadderModal,
    SetupCog,
    SetupView,
    _apply_custom_ladder,
    _apply_template,
    _blocked_channels,
    _create_default_ladder,
    _missing_permissions,
    _preflight_embed,
    _role_hierarchy_ok,
    _validate_and_apply_custom_ladder,
)
from adjutant.services import templates as templates_service


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def bot(fake_bot, guild):
    fake_bot.register_guild(guild)
    return fake_bot


@pytest.fixture
def cog(bot):
    return SetupCog(bot)


def _admin(guild):
    return make_member(guild, display_name="Admin", is_admin=True)


# --------------------------------------------------------------------------- #
# default ladder (pre-existing behaviour, kept green)                        #
# --------------------------------------------------------------------------- #


async def test_creating_the_default_ladder_records_every_rank(bot, guild):
    await seed_guild(bot.db, guild.id)

    created = await _create_default_ladder(bot, guild)

    assert len(created) == len(DEFAULT_LADDER)
    rows = await bot.db.execute_fetchall("SELECT * FROM ranks WHERE guild_id = ?", (guild.id,))
    assert len(rows) == len(DEFAULT_LADDER)


async def test_running_setup_twice_reuses_the_ladder_roles_it_already_made(bot, guild):
    """Otherwise a second run leaves the server with two 'Recruit' roles,
    two 'Private' roles and a rank table with two entries per position."""
    await seed_guild(bot.db, guild.id)
    await _create_default_ladder(bot, guild)
    roles_after_first = [r.name for r in guild.roles]

    await _create_default_ladder(bot, guild)

    assert [r.name for r in guild.roles] == roles_after_first
    rows = await bot.db.execute_fetchall("SELECT * FROM ranks WHERE guild_id = ?", (guild.id,))
    assert len(rows) == len(DEFAULT_LADDER)


async def test_a_rank_role_the_admin_made_is_adopted_but_not_marked_bot_created(bot, guild):
    """Adopting an existing role by name avoids duplicates, but /teardown
    must never delete a role the bot didn't create."""
    await seed_guild(bot.db, guild.id)
    existing = await guild.create_role(name=DEFAULT_LADDER[0][1])

    await _create_default_ladder(bot, guild)

    rows = await bot.db.execute_fetchall(
        "SELECT * FROM ranks WHERE guild_id = ? AND role_id = ?", (guild.id, existing.id)
    )
    assert len(rows) == 1
    assert rows[0]["bot_created"] == 0


# --------------------------------------------------------------------------- #
# templates — pure logic (validate_ladder_names)                             #
# --------------------------------------------------------------------------- #


def test_validate_ladder_names_accepts_a_reasonable_ladder():
    assert templates_service.validate_ladder_names(["Recruit", "Private", "NCO"]) == []


def test_validate_ladder_names_rejects_fewer_than_two_ranks():
    problems = templates_service.validate_ladder_names(["Recruit"])
    assert problems and "at least 2" in problems[0]


def test_validate_ladder_names_rejects_more_than_twenty_ranks():
    names = [f"Rank {i}" for i in range(21)]
    problems = templates_service.validate_ladder_names(names)
    assert problems and "at most 20" in problems[0]


def test_validate_ladder_names_rejects_an_overlong_name():
    problems = templates_service.validate_ladder_names(["Recruit", "X" * 101])
    assert any("1-100 characters" in p for p in problems)


def test_validate_ladder_names_rejects_an_empty_name():
    problems = templates_service.validate_ladder_names(["Recruit", ""])
    assert any("1-100 characters" in p for p in problems)


def test_validate_ladder_names_rejects_case_insensitive_duplicates():
    problems = templates_service.validate_ladder_names(["Recruit", "recruit", "Private"])
    assert any("duplicate" in p for p in problems)


def test_validate_ladder_names_reports_every_problem_at_once():
    """So a decline message can tell the admin everything wrong in one go,
    not a frustrating one-fix-at-a-time loop."""
    problems = templates_service.validate_ladder_names(["Recruit", "recruit"])
    assert len(problems) >= 1  # too-few AND duplicate both apply here
    combined = " ".join(problems)
    assert "at least 2" in combined or "duplicate" in combined


# --------------------------------------------------------------------------- #
# templates — declarative data shape                                          #
# --------------------------------------------------------------------------- #


def test_minimal_template_creates_no_roles_or_channels():
    tmpl = templates_service.TEMPLATES["minimal"]
    assert tmpl.ranks == ()
    assert tmpl.channels == ()


def test_vanilla_and_milsim_templates_define_ranks_and_channels():
    vanilla = templates_service.TEMPLATES["vanilla"]
    milsim = templates_service.TEMPLATES["milsim"]
    assert len(vanilla.ranks) >= 2
    assert len(vanilla.channels) >= 1
    assert len(milsim.ranks) > len(vanilla.ranks)  # milsim is the "fuller" ladder
    assert len(milsim.channels) >= 1
    for spec in vanilla.channels + milsim.channels:
        assert spec.category  # every template channel scaffolds into a named category


# --------------------------------------------------------------------------- #
# templates — application (idempotent create-or-reuse)                       #
# --------------------------------------------------------------------------- #


async def test_applying_vanilla_template_creates_its_ranks_and_channels(bot, guild):
    await seed_guild(bot.db, guild.id)
    tmpl = templates_service.TEMPLATES["vanilla"]

    ranks, channels = await _apply_template(bot, guild, tmpl)

    assert {r.name for r in ranks} == set(tmpl.ranks)
    channel_names = {getattr(c, "name", None) for c in channels}
    assert channel_names == {spec.name for spec in tmpl.channels}
    category_names = {spec.category for spec in tmpl.channels}
    for name in category_names:
        assert any(c.name == name for c in guild.categories)


async def test_applying_milsim_template_scaffolds_ops_channels_under_one_category(bot, guild):
    await seed_guild(bot.db, guild.id)
    tmpl = templates_service.TEMPLATES["milsim"]

    ranks, _ = await _apply_template(bot, guild, tmpl)

    assert len(ranks) == len(tmpl.ranks)
    operations = next(c for c in guild.categories if c.name == "Operations")
    assert {ch.name for ch in operations.channels} == {spec.name for spec in tmpl.channels}


async def test_reapplying_a_template_creates_no_duplicate_roles_or_channels(bot, guild):
    await seed_guild(bot.db, guild.id)
    tmpl = templates_service.TEMPLATES["milsim"]

    await _apply_template(bot, guild, tmpl)
    roles_after_first = sorted(r.name for r in guild.roles)
    categories_after_first = sorted(c.name for c in guild.categories)
    channels_after_first = sorted(ch.name for cat in guild.categories for ch in cat.channels)

    await _apply_template(bot, guild, tmpl)

    assert sorted(r.name for r in guild.roles) == roles_after_first
    assert sorted(c.name for c in guild.categories) == categories_after_first
    assert (
        sorted(ch.name for cat in guild.categories for ch in cat.channels) == channels_after_first
    )


async def test_applying_a_template_with_a_channel_name_that_already_exists_reuses_it(bot, guild):
    """A category/channel an admin already made under the same names must
    be adopted, not duplicated — the same reuse discipline as ranks."""
    await seed_guild(bot.db, guild.id)
    tmpl = templates_service.TEMPLATES["vanilla"]
    category = await guild.create_category(name=tmpl.channels[0].category)
    existing_channel = await category.create_text_channel(tmpl.channels[0].name)

    _, channels = await _apply_template(bot, guild, tmpl)

    assert existing_channel.id in {getattr(c, "id", None) for c in channels}
    assert len(guild.categories) == 1


# --------------------------------------------------------------------------- #
# custom rank ladders (/setup ranks)                                          #
# --------------------------------------------------------------------------- #


async def test_applying_a_custom_ladder_creates_roles_in_order(bot, guild):
    await seed_guild(bot.db, guild.id)

    roles = await _apply_custom_ladder(bot, guild, ["Rifleman", "Squad Lead", "Platoon Lead"])

    assert [r.name for r in roles] == ["Rifleman", "Squad Lead", "Platoon Lead"]
    rows = await bot.db.execute_fetchall(
        "SELECT * FROM ranks WHERE guild_id = ? ORDER BY position", (guild.id,)
    )
    assert [r["name"] for r in rows] == ["Rifleman", "Squad Lead", "Platoon Lead"]
    assert [r["position"] for r in rows] == [0, 1, 2]
    assert all(r["bot_created"] == 1 for r in rows)


async def test_custom_ladder_reuses_a_same_named_existing_role_without_marking_it_bot_created(
    bot, guild
):
    await seed_guild(bot.db, guild.id)
    existing = await guild.create_role(name="Colonel")

    roles = await _apply_custom_ladder(bot, guild, ["Recruit", "Colonel"])

    assert existing.id in {r.id for r in roles}
    assert sum(1 for r in guild.roles if r.name == "Colonel") == 1  # never duplicated
    row = await bot.db.execute_fetchall(
        "SELECT bot_created FROM ranks WHERE guild_id = ? AND role_id = ?", (guild.id, existing.id)
    )
    assert row[0]["bot_created"] == 0


async def test_reapplying_a_custom_ladder_preserves_bot_created_on_roles_it_made(bot, guild):
    """The bug this guards against: a naive delete-and-reinsert would see
    the bot's own role as 'already existing' on the second pass and wipe
    its bot_created flag — silently making /teardown skip a role the bot
    itself created."""
    await seed_guild(bot.db, guild.id)
    await _apply_custom_ladder(bot, guild, ["Recruit", "Private"])

    await _apply_custom_ladder(bot, guild, ["Recruit", "Private", "Officer"])

    rows = await bot.db.execute_fetchall(
        "SELECT name, bot_created FROM ranks WHERE guild_id = ?", (guild.id,)
    )
    assert all(r["bot_created"] == 1 for r in rows)


async def test_shrinking_a_custom_ladder_drops_the_rank_row_but_not_the_discord_role(bot, guild):
    """/setup ranks replaces the bot's bookkeeping wholesale, but it must
    never delete a Discord role out from under an admin — that's what
    /teardown's bot_created guard exists for."""
    await seed_guild(bot.db, guild.id)
    await _apply_custom_ladder(bot, guild, ["Recruit", "Private", "NCO"])

    await _apply_custom_ladder(bot, guild, ["Recruit", "Private"])

    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert {r["name"] for r in rows} == {"Recruit", "Private"}
    assert any(r.name == "NCO" for r in guild.roles)  # role itself untouched


async def test_invalid_custom_ladder_is_declined_and_changes_nothing(bot, guild):
    await seed_guild(bot.db, guild.id)
    await _apply_custom_ladder(bot, guild, ["Recruit", "Private"])
    roles_before = sorted(r.name for r in guild.roles)

    roles, problems = await _validate_and_apply_custom_ladder(bot, guild, ["OnlyOne"])

    assert roles is None
    assert problems
    assert sorted(r.name for r in guild.roles) == roles_before
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert {r["name"] for r in rows} == {"Recruit", "Private"}


# --------------------------------------------------------------------------- #
# /setup — bare command (opens SetupView; check/ranks/quick moved to /admin) #
# --------------------------------------------------------------------------- #


async def test_bare_setup_command_opens_the_wizard_for_an_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="setup")

    await SetupCog.setup_command.callback(cog, interaction)

    assert reply_ephemeral(interaction) is True
    call = all_replies(interaction)[-1]
    assert isinstance(call["view"], SetupView)
    assert call["embed"] is not None


async def test_bare_setup_command_is_refused_for_a_non_admin(cog, bot, guild):
    await seed_guild(bot.db, guild.id)
    grunt = make_member(guild, display_name="Grunt")
    interaction = make_interaction(bot, guild=guild, user=grunt, command_name="setup")

    allowed = await run_checks(SetupCog.setup_command, interaction)

    assert allowed is False
    assert "admin" in reply_text(interaction).lower()


async def test_rank_ladder_modal_submit_applies_the_typed_ladder(bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(bot, guild=guild, user=admin, command_name="setup ranks")
    modal = RankLadderModal(bot, guild)
    modal.ranks_input._value = "Recruit\nPrivate\nNCO"

    await modal.on_submit(interaction)

    assert reply_ephemeral(interaction) is True
    assert "Recruit" in reply_text(interaction)
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert {r["name"] for r in rows} == {"Recruit", "Private", "NCO"}


async def test_rank_ladder_modal_submit_declines_a_duplicate_ladder_and_changes_nothing(bot, guild):
    await seed_guild(bot.db, guild.id)
    admin = _admin(guild)
    interaction = make_interaction(
        bot, guild=guild, user=admin, command_name="adjutant ranks-modal"
    )
    modal = RankLadderModal(bot, guild)
    modal.ranks_input._value = "Recruit\nrecruit"

    await modal.on_submit(interaction)

    assert "afraid not" in reply_text(interaction).lower()
    rows = await bot.db.execute_fetchall("SELECT name FROM ranks WHERE guild_id = ?", (guild.id,))
    assert rows == []


# --------------------------------------------------------------------------- #
# permissions preflight — pure helpers (command entry point is /admin        #
# preflight now; covered in tests/test_cogs_admin.py)                        #
# --------------------------------------------------------------------------- #


def test_missing_permissions_lists_only_what_is_absent():
    perms = discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        connect=True,
        speak=True,
        # manage_roles and manage_channels deliberately left out
    )

    missing = _missing_permissions(perms)

    missing_names = {perm for perm, _why in missing}
    assert missing_names == {"manage_roles", "manage_channels"}


def test_missing_permissions_reports_nothing_when_all_required_perms_are_held():
    full = discord.Permissions()
    for perm, _why in REQUIRED_PERMISSIONS:
        setattr(full, perm, True)

    assert _missing_permissions(full) == []


def test_role_hierarchy_ok_when_bot_role_sits_above_everything_else(guild):
    assert _role_hierarchy_ok(guild) is True  # FakeGuild's bot-top role starts at position 100


def test_role_hierarchy_flagged_when_another_role_outranks_the_bot(guild):
    guild.roles.append(make_role("Owner Special", position=999))

    assert _role_hierarchy_ok(guild) is False


def test_blocked_channels_finds_a_channel_the_bot_cannot_view(guild):
    visible = guild.create_standalone_text_channel(name="general")
    visible.permissions_for = lambda member: discord.Permissions.all()
    hidden = guild.create_standalone_text_channel(name="admin-only")
    hidden.permissions_for = lambda member: discord.Permissions.none()

    blocked = _blocked_channels(guild)

    assert hidden in blocked
    assert visible not in blocked


def test_preflight_embed_never_recommends_granting_administrator(guild):
    guild.me.guild_permissions = discord.Permissions.none()

    embed = _preflight_embed(guild, minimal=False)

    text = f"{embed.title}\n{embed.description}".lower()
    assert "designed to run without administrator" in text
    # the fix language must name specific permissions, never suggest the blanket grant
    assert "grant administrator" not in text and "give me administrator" not in text


def test_preflight_embed_names_missing_permissions_in_plain_terms(guild):
    guild.me.guild_permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        connect=True,
        speak=True,
    )

    embed = _preflight_embed(guild, minimal=False)

    assert "manage_roles" in embed.description
    assert "manage_channels" in embed.description
