"""Pure-logic map rendering service for the `map` cog.

Discord-free by design (see docs/SPEC.md): coordinate transforms, grid
references and Pillow rendering all live here so they're directly testable.
Cogs are thin adapters that pull marker rows from the DB, build `Marker`
objects, and hand them to `render_to_png_bytes`.

Coordinate convention: Arma world space is X (east) / Z (north), origin at
the map's south-west corner, extending to (width_m, height_m) at the
north-east corner. Image space is the usual top-left-origin pixel grid, so
the Z axis is flipped on the way to pixels (north = up = smaller pixel y).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MappingError(Exception):
    """Base class for mapping-service errors."""


class UnknownTerrainError(MappingError):
    """Raised when a terrain slug isn't in the TERRAINS registry."""

    def __init__(self, slug: object):
        super().__init__(f"unknown terrain: {slug!r}")
        self.slug = slug


class InvalidGridReferenceError(MappingError):
    """Raised when a grid reference string can't be parsed."""

    def __init__(self, reference: str):
        super().__init__(f"invalid grid reference: {reference!r}")
        self.reference = reference


# ---------------------------------------------------------------------------
# Terrain registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerrainInfo:
    """Static facts about a playable terrain.

    Data, not logic, so adding a terrain or correcting a value later touches
    only the TERRAINS dict below.
    """

    slug: str
    display_name: str
    width_m: float  # world extent, east-west (X)
    height_m: float  # world extent, south-north (Z)
    origin: str = "corner"  # 'corner': world (0,0) at SW corner; 'center': (0,0) at map centre

    def origin_min(self) -> tuple[float, float]:
        """World (x, z) of the image's bottom-left (south-west) pixel."""
        if self.origin == "center":
            return (-self.width_m / 2, -self.height_m / 2)
        return (0.0, 0.0)


# Confirmed extents from the reforger-research pass (official wiki terrain
# pages + the nick.recoil.org Reforger writeup), not placeholders. Enfusion
# world origin is confirmed as the bottom-left (south-west) corner, +X east,
# +Z north, extending to (width_m, height_m) at the north-east corner —
# "corner" origin below. Data, not logic: correcting a value later, or
# adding a terrain, is a one-line edit here, no code change.
TERRAINS: dict[str, TerrainInfo] = {
    "everon": TerrainInfo(
        slug="everon",
        display_name="Everon",
        width_m=12800.0,
        height_m=12800.0,
        origin="corner",
    ),
    "arland": TerrainInfo(
        slug="arland",
        display_name="Arland",
        # NOT 5942 — that figure circulating elsewhere is a screenshot pixel
        # size, not a world extent in metres.
        width_m=4096.0,
        height_m=4096.0,
        origin="corner",
    ),
    "kolguyev": TerrainInfo(
        slug="kolguyev",
        display_name="Kolguyev",
        width_m=12800.0,
        height_m=12800.0,
        origin="corner",
    ),
}


def resolve_terrain(terrain: str | TerrainInfo) -> TerrainInfo:
    """Accept either a registry slug or an already-resolved TerrainInfo."""
    if isinstance(terrain, TerrainInfo):
        return terrain
    try:
        return TERRAINS[terrain]
    except KeyError:
        raise UnknownTerrainError(terrain) from None


# ---------------------------------------------------------------------------
# Coordinate transform
# ---------------------------------------------------------------------------


def _size_tuple(image_size: int | tuple[int, int]) -> tuple[int, int]:
    if isinstance(image_size, tuple):
        return image_size
    return (image_size, image_size)


def world_to_pixel(
    x: float,
    z: float,
    terrain: str | TerrainInfo,
    image_size: int | tuple[int, int],
) -> tuple[float, float]:
    """World (x, z) metres -> image (px, py) pixels. Pure, unclamped linear map.

    North (+Z) moves up the image (smaller py); east (+X) moves right (larger px).
    """
    info = resolve_terrain(terrain)
    width_px, height_px = _size_tuple(image_size)
    min_x, min_z = info.origin_min()
    nx = (x - min_x) / info.width_m
    nz = (z - min_z) / info.height_m
    px = nx * width_px
    py = height_px - (nz * height_px)
    return px, py


def pixel_to_world(
    px: float,
    py: float,
    terrain: str | TerrainInfo,
    image_size: int | tuple[int, int],
) -> tuple[float, float]:
    """Inverse of world_to_pixel: image (px, py) pixels -> world (x, z) metres."""
    info = resolve_terrain(terrain)
    width_px, height_px = _size_tuple(image_size)
    min_x, min_z = info.origin_min()
    nx = px / width_px
    nz = (height_px - py) / height_px
    x = min_x + nx * info.width_m
    z = min_z + nz * info.height_m
    return x, z


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


# ---------------------------------------------------------------------------
# Grid references
# ---------------------------------------------------------------------------
#
# Arma-style numeric grid: an even-length digit string split into two equal
# halves, easting before northing. Half-length determines precision:
#    4 digits (2 + 2) ->   1km precision, e.g. "02 08"    -> (2000, 8000)
#    6 digits (3 + 3) ->  100m precision, e.g. "023 087"  -> (2300, 8700)
#    8 digits (4 + 4) ->   10m precision, e.g. "0231 0873" -> (2310, 8730)
#   10 digits (5 + 5) ->    1m precision, e.g. "02310 08730" -> (2310, 8730)

_SUPPORTED_GRID_DIGITS = (4, 6, 8, 10)


def _grid_multiplier(half_len: int) -> int:
    return 10 ** (5 - half_len)


def parse_grid(reference: str) -> tuple[float, float]:
    """Parse a 4/6/8/10-digit grid reference (with or without a mid space) to world (x, z)."""
    digits = re.sub(r"\s+", "", reference)
    if not digits.isdigit() or len(digits) not in _SUPPORTED_GRID_DIGITS:
        raise InvalidGridReferenceError(reference)
    half = len(digits) // 2
    multiplier = _grid_multiplier(half)
    x = int(digits[:half]) * multiplier
    z = int(digits[half:]) * multiplier
    return float(x), float(z)


def format_grid(x: float, z: float, digits: int = 6) -> str:
    """Format world (x, z) as a grid reference string, e.g. "023 087"."""
    if digits not in _SUPPORTED_GRID_DIGITS:
        raise ValueError(f"digits must be one of {_SUPPORTED_GRID_DIGITS}, got {digits}")
    half = digits // 2
    multiplier = _grid_multiplier(half)
    max_val = 10**half - 1
    easting = int(round(x / multiplier))
    northing = int(round(z / multiplier))
    easting = int(_clamp(easting, 0, max_val))
    northing = int(_clamp(northing, 0, max_val))
    return f"{easting:0{half}d} {northing:0{half}d}"


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    kind: str  # 'objective' | 'friendly' | 'enemy' | 'note' | anything else (falls back to default glyph)
    label: str
    x: float
    z: float


@dataclass(frozen=True)
class _MarkerStyle:
    shape: str  # 'diamond' | 'circle' | 'square'
    color: tuple[int, int, int]
    radius: int


_MARKER_STYLES: dict[str, _MarkerStyle] = {
    "objective": _MarkerStyle(shape="diamond", color=(255, 196, 0), radius=9),
    "friendly": _MarkerStyle(shape="circle", color=(70, 140, 255), radius=8),
    "enemy": _MarkerStyle(shape="diamond", color=(220, 50, 50), radius=9),
    "note": _MarkerStyle(shape="square", color=(230, 230, 230), radius=6),
}
_DEFAULT_MARKER_STYLE = _MarkerStyle(shape="square", color=(200, 200, 200), radius=6)

# Recognised marker kinds, in registry order — for cogs building app_commands
# choices. Any other kind still renders (via the default style, above), it
# just won't appear as a picklist option.
MARKER_KINDS: tuple[str, ...] = tuple(_MARKER_STYLES.keys())

_OUTLINE_COLOR = (20, 20, 20, 255)
_LABEL_COLOR = (255, 255, 255, 255)


def _draw_marker(draw: ImageDraw.ImageDraw, px: float, py: float, marker: Marker, font: ImageFont.ImageFont) -> None:
    style = _MARKER_STYLES.get(marker.kind, _DEFAULT_MARKER_STYLE)
    r = style.radius
    fill = (*style.color, 255)

    if style.shape == "circle":
        draw.ellipse([px - r, py - r, px + r, py + r], fill=fill, outline=_OUTLINE_COLOR, width=2)
    elif style.shape == "diamond":
        draw.polygon(
            [(px, py - r), (px + r, py), (px, py + r), (px - r, py)],
            fill=fill,
            outline=_OUTLINE_COLOR,
            width=2,
        )
    else:  # square
        draw.rectangle([px - r, py - r, px + r, py + r], fill=fill, outline=_OUTLINE_COLOR, width=2)

    if marker.label:
        draw.text(
            (px + r + 3, py - r),
            marker.label,
            font=font,
            fill=_LABEL_COLOR,
            stroke_width=2,
            stroke_fill=_OUTLINE_COLOR,
        )


# ---------------------------------------------------------------------------
# Base layer
# ---------------------------------------------------------------------------

# Repo-root/assets/maps/<slug>.png. A module attribute (not a hardcoded path
# inline) so tests can monkeypatch it to point at a temp directory.
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
MAPS_DIR = ASSETS_DIR / "maps"

_BACKGROUND_COLOR = (86, 106, 74)  # muted terrain green
_GRIDLINE_COLOR = (255, 255, 255, 40)
_LABEL_TEXT_COLOR = (255, 255, 255, 170)
_WATERMARK_COLOR = (255, 255, 255, 55)
_GRID_SPACING_M = 1000.0  # 1km gridlines


def _generate_placeholder(info: TerrainInfo, size: tuple[int, int]) -> Image.Image:
    """A clean, deliberately-designed stand-in base layer for terrains with no real imagery yet."""
    width_px, height_px = size
    img = Image.new("RGBA", (width_px, height_px), (*_BACKGROUND_COLOR, 255))
    draw = ImageDraw.Draw(img, "RGBA")
    label_font = ImageFont.load_default()

    spacing_x = (_GRID_SPACING_M / info.width_m) * width_px
    spacing_y = (_GRID_SPACING_M / info.height_m) * height_px

    col = 0
    x = 0.0
    while x <= width_px:
        draw.line([(x, 0), (x, height_px)], fill=_GRIDLINE_COLOR, width=1)
        if x > 0:
            draw.text((x + 2, 2), str(col), font=label_font, fill=_LABEL_TEXT_COLOR)
        x += spacing_x
        col += 1

    row = 0
    y = height_px
    while y >= 0:
        draw.line([(0, y), (width_px, y)], fill=_GRIDLINE_COLOR, width=1)
        if y < height_px:
            draw.text((2, max(y - 12, 0)), str(row), font=label_font, fill=_LABEL_TEXT_COLOR)
        y -= spacing_y
        row += 1

    watermark_font = ImageFont.load_default(size=max(width_px // 12, 12))
    text = info.display_name.upper()
    bbox = draw.textbbox((0, 0), text, font=watermark_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((width_px - tw) / 2, (height_px - th) / 2),
        text,
        font=watermark_font,
        fill=_WATERMARK_COLOR,
    )

    return img.convert("RGB")


def _load_or_generate_base(info: TerrainInfo, size: tuple[int, int]) -> Image.Image:
    path = MAPS_DIR / f"{info.slug}.png"
    if path.is_file():
        with Image.open(path) as src:
            return src.convert("RGB").resize(size, Image.LANCZOS)
    return _generate_placeholder(info, size)


# ---------------------------------------------------------------------------
# Public rendering API
# ---------------------------------------------------------------------------


def render_map(
    terrain: str | TerrainInfo,
    markers: list[Marker],
    size: int | tuple[int, int] = 1280,
) -> Image.Image:
    """Render a terrain base layer with markers overlaid. Returns a PIL Image.

    Markers outside the terrain's world extent are clamped to the nearest
    edge pixel rather than skipped, so a mis-placed or edge-of-map marker is
    still visible (pinned at the border) instead of silently vanishing.
    """
    info = resolve_terrain(terrain)
    size_tuple = _size_tuple(size)
    width_px, height_px = size_tuple

    base = _load_or_generate_base(info, size_tuple).convert("RGBA")
    draw = ImageDraw.Draw(base, "RGBA")
    marker_font = ImageFont.load_default()

    for marker in markers:
        px, py = world_to_pixel(marker.x, marker.z, info, size_tuple)
        px = _clamp(px, 0.0, float(width_px))
        py = _clamp(py, 0.0, float(height_px))
        _draw_marker(draw, px, py, marker, marker_font)

    return base.convert("RGB")


def render_to_png_bytes(
    terrain: str | TerrainInfo,
    markers: list[Marker],
    size: int | tuple[int, int] = 1280,
) -> bytes:
    """Render and encode as PNG bytes, ready for `discord.File(io.BytesIO(...), ...)`."""
    img = render_map(terrain, markers, size)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
