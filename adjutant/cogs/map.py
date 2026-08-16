"""Map cog: Pillow-rendered map messages with markers.

/map show   — post or refresh this channel's map. Open to anyone who can see
              the channel (viewing isn't mutating; no reason to rank-gate it).
/map mark   — place a marker via grid reference. Rank-gated (map.edit) and
              rate-limited, per the SPEC's abuse-resistance requirement.
/map clear  — remove one marker (by id) or every marker. Same gating.

One map row per (guild, channel); mark/clear/refresh edit that message's
attachment in place rather than reposting, so the channel doesn't fill up
with map spam every time someone moves a pin. Coordinate transforms, grid
parsing, and rendering all live in services/mapping.py — this cog only does
Discord plumbing and DB reads/writes.
"""

from __future__ import annotations

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from ..services import mapping as mapping_service
from .admin import check_rate_limit, fetchone, note_audit
from .roles import decline_missing_permission, member_has_permission

log = logging.getLogger(__name__)

_TERRAIN_CHOICES = [
    app_commands.Choice(name=info.display_name, value=slug)
    for slug, info in mapping_service.TERRAINS.items()
]
_DEFAULT_TERRAIN = "everon"


async def _load_map(db, guild_id: int, channel_id: int):
    return await fetchone(
        db, "SELECT * FROM maps WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
    )


async def _load_markers(db, map_id: int) -> list[mapping_service.Marker]:
    rows = await db.execute_fetchall(
        "SELECT kind, label, x, y FROM map_markers WHERE map_id = ?", (map_id,)
    )
    # DB column `y` holds the same value this service calls `z` (north axis) —
    # see services/mapping.py's module docstring for the coordinate convention.
    return [
        mapping_service.Marker(kind=r["kind"], label=r["label"], x=r["x"], z=r["y"]) for r in rows
    ]


async def _render_bytes(db, map_row) -> bytes:
    markers = await _load_markers(db, map_row["id"])
    return mapping_service.render_to_png_bytes(map_row["terrain"], markers)


class MapMarkModal(discord.ui.Modal, title="Place a Marker"):
    """Collects kind/label/grid privately — the button that opens this
    re-checks map.edit, and on_submit re-checks it again since a modal
    submission is its own interaction."""

    kind_input: discord.ui.TextInput = discord.ui.TextInput(
        label=f"Kind ({', '.join(mapping_service.MARKER_KINDS)})",
        placeholder=mapping_service.MARKER_KINDS[0],
    )
    label_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Short label", placeholder="Hilltop OP"
    )
    grid_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Grid reference", placeholder="023 087 (4/6/8/10 digits)"
    )

    def __init__(self, cog: MapCog, channel_id: int):
        super().__init__()
        self.cog = cog
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or interaction.guild is None:
            return
        if not await member_has_permission(self.cog.bot, interaction.guild, actor, "map.edit"):
            await decline_missing_permission(interaction, "map.edit")
            return
        if not await check_rate_limit(interaction, "map.mark"):
            return
        kind = self.kind_input.value.strip().lower()
        if kind not in mapping_service.MARKER_KINDS:
            await interaction.response.send_message(
                voice.decline(
                    f"`{kind}` isn't a marker kind I know. Try one of: {', '.join(mapping_service.MARKER_KINDS)}."
                ),
                ephemeral=True,
            )
            return
        await self.cog._do_mark(
            interaction,
            self.channel_id,
            kind,
            self.label_input.value.strip(),
            self.grid_input.value.strip(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class MapClearModal(discord.ui.Modal, title="Clear Marker(s)"):
    marker_id_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Marker id — leave blank to clear every marker", required=False
    )

    def __init__(self, cog: MapCog, channel_id: int):
        super().__init__()
        self.cog = cog
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or interaction.guild is None:
            return
        if not await member_has_permission(self.cog.bot, interaction.guild, actor, "map.edit"):
            await decline_missing_permission(interaction, "map.edit")
            return
        if not await check_rate_limit(interaction, "map.clear"):
            return
        raw = self.marker_id_input.value.strip()
        marker_id: int | None = None
        if raw:
            try:
                marker_id = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    voice.decline(f"`{raw}` isn't a whole number."), ephemeral=True
                )
                return
        await self.cog._do_clear(interaction, self.channel_id, marker_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await view_util.handle_app_command_error(interaction, error, log)


class MapPanelView(view_util.ErrorHandledView):
    """Attached to every /map reply. Mark opens a modal so nobody has to
    remember a subcommand's argument order; Clear does the same for marker
    removal. Both buttons re-check map.edit before even opening their modal."""

    def __init__(self, cog: MapCog, channel_id: int, invoker_id: int, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.channel_id = channel_id
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This panel belongs to whoever ran `/map` here."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Mark…", style=discord.ButtonStyle.primary)
    async def mark_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or interaction.guild is None:
            return
        if not await member_has_permission(self.cog.bot, interaction.guild, actor, "map.edit"):
            await decline_missing_permission(interaction, "map.edit")
            return
        await interaction.response.send_modal(MapMarkModal(self.cog, self.channel_id))

    @discord.ui.button(label="Clear…", style=discord.ButtonStyle.secondary)
    async def clear_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        actor = interaction.user
        if not isinstance(actor, discord.Member) or interaction.guild is None:
            return
        if not await member_has_permission(self.cog.bot, interaction.guild, actor, "map.edit"):
            await decline_missing_permission(interaction, "map.edit")
            return
        await interaction.response.send_modal(MapClearModal(self.cog, self.channel_id))


class MapCog(commands.Cog, name="MapCog"):
    """/map — live map messages with markers, Mark/Clear behind buttons."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _refresh_message(
        self, channel: discord.abc.Messageable, map_row, png_bytes: bytes
    ) -> None:
        """Edit the map's existing message in place; post + record a fresh one
        if there's no message yet, or the old one is gone."""
        assert self.bot.db is not None
        file = discord.File(io.BytesIO(png_bytes), filename=f"map-{map_row['terrain']}.png")

        message = None
        if map_row["message_id"]:
            try:
                message = await channel.fetch_message(map_row["message_id"])
            except discord.NotFound:
                message = None

        if message is not None:
            try:
                await message.edit(attachments=[file])
                return
            except discord.HTTPException:
                pass  # fall through and post a replacement

        new_message = await channel.send(file=file)
        await self.bot.db.execute(
            "UPDATE maps SET message_id = ? WHERE id = ?", (new_message.id, map_row["id"])
        )
        await self.bot.db.commit()

    @app_commands.command(name="map", description="Post or refresh this channel's map.")
    @app_commands.describe(
        terrain="Terrain to use — only matters the first time this channel gets a map"
    )
    @app_commands.choices(terrain=_TERRAIN_CHOICES)
    async def map_command(
        self, interaction: discord.Interaction, terrain: app_commands.Choice[str] | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        db = self.bot.db
        channel_id = interaction.channel_id
        assert channel_id is not None

        row = await _load_map(db, guild.id, channel_id)
        if row is not None:
            await interaction.response.defer(ephemeral=True)
            data = await _render_bytes(db, row)
            await self._refresh_message(interaction.channel, row, data)
            view = MapPanelView(self, channel_id, interaction.user.id)
            await interaction.followup.send("Refreshed.", view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return

        slug = terrain.value if terrain is not None else _DEFAULT_TERRAIN
        try:
            mapping_service.resolve_terrain(slug)
        except mapping_service.UnknownTerrainError:
            await interaction.response.send_message(
                voice.decline(f"Unknown terrain `{slug}`."), ephemeral=True
            )
            return

        await interaction.response.defer()
        data = mapping_service.render_to_png_bytes(slug, [])
        file = discord.File(io.BytesIO(data), filename=f"map-{slug}.png")
        message = await interaction.channel.send(file=file)
        await db.execute(
            "INSERT INTO maps (guild_id, channel_id, message_id, terrain) VALUES (?, ?, ?, ?)",
            (guild.id, channel_id, message.id, slug),
        )
        await db.commit()
        view = MapPanelView(self, channel_id, interaction.user.id)
        await interaction.followup.send(
            f"Map's up — **{mapping_service.TERRAINS[slug].display_name}**.", view=view
        )
        view.message = await interaction.original_response()

    async def _do_mark(
        self, interaction: discord.Interaction, channel_id: int, kind: str, label: str, grid: str
    ) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        db = self.bot.db
        channel = self.bot.get_channel(channel_id)

        row = await _load_map(db, guild.id, channel_id)
        if row is None:
            await interaction.response.send_message(
                voice.decline("No map here yet. Run `/map` first."), ephemeral=True
            )
            return

        try:
            x, z = mapping_service.parse_grid(grid)
        except mapping_service.InvalidGridReferenceError:
            await interaction.response.send_message(
                voice.decline(
                    f"`{grid}` isn't a grid reference I recognise — try something like `023 087`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        cursor = await db.execute(
            "INSERT INTO map_markers (map_id, kind, label, x, y, placed_by) VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], kind, label, x, z, interaction.user.id),
        )
        await db.commit()
        marker_id = cursor.lastrowid

        data = await _render_bytes(db, row)
        if channel is not None:
            await self._refresh_message(channel, row, data)

        await note_audit(
            self.bot,
            guild.id,
            f'Map marker placed by <@{interaction.user.id}>: {kind} "{label}" @ {grid}',
        )
        await interaction.followup.send(
            f"Marked (#{marker_id}) — **{label}** at `{mapping_service.format_grid(x, z)}`.",
            ephemeral=True,
        )

    async def _do_clear(
        self, interaction: discord.Interaction, channel_id: int, marker_id: int | None = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        db = self.bot.db
        channel = self.bot.get_channel(channel_id)

        row = await _load_map(db, guild.id, channel_id)
        if row is None:
            await interaction.response.send_message(
                voice.decline("No map here yet."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        if marker_id is not None:
            marker = await fetchone(
                db, "SELECT id FROM map_markers WHERE id = ? AND map_id = ?", (marker_id, row["id"])
            )
            if marker is None:
                await interaction.followup.send(
                    voice.decline(f"No marker #{marker_id} on this map."), ephemeral=True
                )
                return
            await db.execute("DELETE FROM map_markers WHERE id = ?", (marker_id,))
            summary = f"Cleared marker #{marker_id}."
        else:
            await db.execute("DELETE FROM map_markers WHERE map_id = ?", (row["id"],))
            summary = "Cleared every marker."
        await db.commit()

        data = await _render_bytes(db, row)
        if channel is not None:
            await self._refresh_message(channel, row, data)

        await note_audit(self.bot, guild.id, f"Map cleared by <@{interaction.user.id}>: {summary}")
        await interaction.followup.send(summary, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MapCog(bot))
