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
from .admin import fetchone, note_audit, rate_limited
from .roles import require_permission

log = logging.getLogger(__name__)

_TERRAIN_CHOICES = [
    app_commands.Choice(name=info.display_name, value=slug) for slug, info in mapping_service.TERRAINS.items()
]
_MARKER_KIND_CHOICES = [
    app_commands.Choice(name=kind.capitalize(), value=kind) for kind in mapping_service.MARKER_KINDS
]
_DEFAULT_TERRAIN = "everon"


async def _load_map(db, guild_id: int, channel_id: int):
    return await fetchone(db, "SELECT * FROM maps WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))


async def _load_markers(db, map_id: int) -> list[mapping_service.Marker]:
    rows = await db.execute_fetchall(
        "SELECT kind, label, x, y FROM map_markers WHERE map_id = ?", (map_id,)
    )
    # DB column `y` holds the same value this service calls `z` (north axis) —
    # see services/mapping.py's module docstring for the coordinate convention.
    return [mapping_service.Marker(kind=r["kind"], label=r["label"], x=r["x"], z=r["y"]) for r in rows]


async def _render_bytes(db, map_row) -> bytes:
    markers = await _load_markers(db, map_row["id"])
    return mapping_service.render_to_png_bytes(map_row["terrain"], markers)


class MapCog(commands.GroupCog, group_name="map"):
    """/map — live map messages with markers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _refresh_message(self, channel: discord.abc.Messageable, map_row, png_bytes: bytes) -> None:
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
        await self.bot.db.execute("UPDATE maps SET message_id = ? WHERE id = ?", (new_message.id, map_row["id"]))
        await self.bot.db.commit()

    @app_commands.command(name="show", description="Post or refresh this channel's map.")
    @app_commands.describe(terrain="Terrain to use — only matters the first time this channel gets a map")
    @app_commands.choices(terrain=_TERRAIN_CHOICES)
    async def show(self, interaction: discord.Interaction, terrain: app_commands.Choice[str] | None = None) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        db = self.bot.db

        row = await _load_map(db, guild.id, interaction.channel_id)
        if row is not None:
            await interaction.response.defer(ephemeral=True)
            data = await _render_bytes(db, row)
            await self._refresh_message(interaction.channel, row, data)
            await interaction.followup.send("Refreshed.", ephemeral=True)
            return

        slug = terrain.value if terrain is not None else _DEFAULT_TERRAIN
        try:
            mapping_service.resolve_terrain(slug)
        except mapping_service.UnknownTerrainError:
            await interaction.response.send_message(voice.decline(f"Unknown terrain `{slug}`."), ephemeral=True)
            return

        await interaction.response.defer()
        data = mapping_service.render_to_png_bytes(slug, [])
        file = discord.File(io.BytesIO(data), filename=f"map-{slug}.png")
        message = await interaction.channel.send(file=file)
        await db.execute(
            "INSERT INTO maps (guild_id, channel_id, message_id, terrain) VALUES (?, ?, ?, ?)",
            (guild.id, interaction.channel_id, message.id, slug),
        )
        await db.commit()
        await interaction.followup.send(f"Map's up — **{mapping_service.TERRAINS[slug].display_name}**.")

    @app_commands.command(name="mark", description="Place a marker on this channel's map.")
    @app_commands.describe(
        kind="Marker type",
        label="Short label",
        grid="Grid reference, e.g. \"023 087\" (4/6/8/10 digits)",
    )
    @app_commands.choices(kind=_MARKER_KIND_CHOICES)
    @require_permission("map.edit")
    @rate_limited()
    async def mark(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        label: str,
        grid: str,
    ) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        db = self.bot.db

        row = await _load_map(db, guild.id, interaction.channel_id)
        if row is None:
            await interaction.response.send_message(
                voice.decline("No map here yet. Run `/map show` first."), ephemeral=True
            )
            return

        try:
            x, z = mapping_service.parse_grid(grid)
        except mapping_service.InvalidGridReferenceError:
            await interaction.response.send_message(
                voice.decline(f"`{grid}` isn't a grid reference I recognise — try something like `023 087`."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        cursor = await db.execute(
            "INSERT INTO map_markers (map_id, kind, label, x, y, placed_by) VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], kind.value, label, x, z, interaction.user.id),
        )
        await db.commit()
        marker_id = cursor.lastrowid

        data = await _render_bytes(db, row)
        await self._refresh_message(interaction.channel, row, data)

        await note_audit(
            self.bot, guild.id, f"Map marker placed by <@{interaction.user.id}>: {kind.value} \"{label}\" @ {grid}"
        )
        await interaction.followup.send(
            f"Marked (#{marker_id}) — **{label}** at `{mapping_service.format_grid(x, z)}`.", ephemeral=True
        )

    @app_commands.command(name="clear", description="Remove one marker (by id) or every marker from this channel's map.")
    @app_commands.describe(marker_id="Marker id to remove; omit to clear every marker")
    @require_permission("map.edit")
    @rate_limited()
    async def clear(self, interaction: discord.Interaction, marker_id: int | None = None) -> None:
        guild = interaction.guild
        assert guild is not None and self.bot.db is not None
        db = self.bot.db

        row = await _load_map(db, guild.id, interaction.channel_id)
        if row is None:
            await interaction.response.send_message(voice.decline("No map here yet."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if marker_id is not None:
            marker = await fetchone(
                db, "SELECT id FROM map_markers WHERE id = ? AND map_id = ?", (marker_id, row["id"])
            )
            if marker is None:
                await interaction.followup.send(voice.decline(f"No marker #{marker_id} on this map."), ephemeral=True)
                return
            await db.execute("DELETE FROM map_markers WHERE id = ?", (marker_id,))
            summary = f"Cleared marker #{marker_id}."
        else:
            await db.execute("DELETE FROM map_markers WHERE map_id = ?", (row["id"],))
            summary = "Cleared every marker."
        await db.commit()

        data = await _render_bytes(db, row)
        await self._refresh_message(interaction.channel, row, data)

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
