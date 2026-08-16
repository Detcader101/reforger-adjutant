"""Behaviour spec for the Pillow map rendering service (adjutant/services/mapping.py).

Pure coordinate transforms + grid references + rendering. No Discord, no DB.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from adjutant.services import mapping

# ---------------------------------------------------------------------------
# Terrain registry
# ---------------------------------------------------------------------------


def test_terrain_registry_exposes_everon_arland_and_kolguyev():
    assert "everon" in mapping.TERRAINS
    assert "arland" in mapping.TERRAINS
    assert "kolguyev" in mapping.TERRAINS
    everon = mapping.TERRAINS["everon"]
    arland = mapping.TERRAINS["arland"]
    kolguyev = mapping.TERRAINS["kolguyev"]
    assert everon.display_name == "Everon"
    assert arland.display_name == "Arland"
    assert kolguyev.display_name == "Kolguyev"
    assert everon.width_m > arland.width_m


def test_terrain_extents_match_confirmed_research_values():
    """Confirmed (not placeholder) world extents from the reforger-research pass."""
    everon = mapping.TERRAINS["everon"]
    arland = mapping.TERRAINS["arland"]
    kolguyev = mapping.TERRAINS["kolguyev"]
    assert (everon.width_m, everon.height_m) == (12800.0, 12800.0)
    assert (arland.width_m, arland.height_m) == (4096.0, 4096.0)
    assert (kolguyev.width_m, kolguyev.height_m) == (12800.0, 12800.0)
    # 5942 was a circulating figure for Arland but is a screenshot pixel size,
    # not a world extent in metres — must not leak back in.
    assert arland.width_m != 5942.0


def test_resolve_terrain_accepts_slug_or_terrain_info():
    by_slug = mapping.resolve_terrain("arland")
    by_info = mapping.resolve_terrain(by_slug)
    assert by_slug is by_info


def test_resolve_terrain_rejects_unknown_slug_with_clear_error():
    with pytest.raises(mapping.UnknownTerrainError):
        mapping.resolve_terrain("chernarus")


# ---------------------------------------------------------------------------
# Coordinate transform
# ---------------------------------------------------------------------------


def test_world_to_pixel_and_back_round_trips_for_in_bounds_coordinates():
    terrain = mapping.TERRAINS["arland"]
    size = 1000
    x, z = 1234.5, 2876.25
    px, py = mapping.world_to_pixel(x, z, terrain, size)
    rx, rz = mapping.pixel_to_world(px, py, terrain, size)
    assert rx == pytest.approx(x, abs=1e-6)
    assert rz == pytest.approx(z, abs=1e-6)


def test_world_origin_maps_to_bottom_left_pixel_for_corner_terrain():
    terrain = mapping.TERRAINS["arland"]
    size = 1000
    px, py = mapping.world_to_pixel(0.0, 0.0, terrain, size)
    assert px == pytest.approx(0.0)
    assert py == pytest.approx(size)  # z=0 (south edge) is the bottom of the image


def test_far_corner_maps_to_top_right_pixel_for_corner_terrain():
    terrain = mapping.TERRAINS["arland"]
    size = 1000
    px, py = mapping.world_to_pixel(terrain.width_m, terrain.height_m, terrain, size)
    assert px == pytest.approx(size)
    assert py == pytest.approx(0.0)  # max z (north edge) is the top of the image


def test_increasing_z_moves_the_point_up_the_image():
    """North (increasing Z) must move toward smaller pixel Y (up), not down."""
    terrain = mapping.TERRAINS["arland"]
    size = 800
    _, py_low_z = mapping.world_to_pixel(2000.0, 500.0, terrain, size)
    _, py_high_z = mapping.world_to_pixel(2000.0, 3500.0, terrain, size)
    assert py_high_z < py_low_z


def test_increasing_x_moves_the_point_right_across_the_image():
    terrain = mapping.TERRAINS["arland"]
    size = 800
    px_low_x, _ = mapping.world_to_pixel(500.0, 2000.0, terrain, size)
    px_high_x, _ = mapping.world_to_pixel(3500.0, 2000.0, terrain, size)
    assert px_high_x > px_low_x


def test_world_to_pixel_accepts_non_square_image_size_tuple():
    terrain = mapping.TERRAINS["arland"]
    px, py = mapping.world_to_pixel(terrain.width_m, 0.0, terrain, (640, 480))
    assert px == pytest.approx(640.0)
    assert py == pytest.approx(480.0)


# ---------------------------------------------------------------------------
# Grid references
# ---------------------------------------------------------------------------


def test_parse_grid_reads_four_digit_form_at_1km_precision():
    x, z = mapping.parse_grid("0208")
    assert x == pytest.approx(2000.0)
    assert z == pytest.approx(8000.0)


def test_parse_grid_reads_ten_digit_form_at_1m_precision():
    x, z = mapping.parse_grid("0231008730")
    assert x == pytest.approx(2310.0)
    assert z == pytest.approx(8730.0)


def test_parse_grid_reads_six_digit_form_at_100m_precision():
    x, z = mapping.parse_grid("023087")
    assert x == pytest.approx(2300.0)
    assert z == pytest.approx(8700.0)


def test_parse_grid_accepts_space_separated_six_digit_form():
    x, z = mapping.parse_grid("023 087")
    assert x == pytest.approx(2300.0)
    assert z == pytest.approx(8700.0)


def test_parse_grid_reads_eight_digit_form_at_10m_precision():
    x, z = mapping.parse_grid("02310873")
    assert x == pytest.approx(2310.0)
    assert z == pytest.approx(8730.0)


def test_parse_grid_eight_digit_form_is_finer_than_six_digit_form():
    """The extra two digits must resolve within the 100m cell, not just repeat it."""
    coarse_x, coarse_z = mapping.parse_grid("023087")
    fine_x, fine_z = mapping.parse_grid("02350875")
    assert fine_x != coarse_x
    assert fine_z != coarse_z
    assert coarse_x <= fine_x < coarse_x + 100
    assert coarse_z <= fine_z < coarse_z + 100


def test_parse_grid_rejects_malformed_reference():
    with pytest.raises(mapping.InvalidGridReferenceError):
        mapping.parse_grid("not a grid")
    with pytest.raises(mapping.InvalidGridReferenceError):
        mapping.parse_grid("12345")  # odd length, not a valid 6/8 digit form


def test_format_grid_produces_six_digit_form_by_default():
    assert mapping.format_grid(2300.0, 8700.0) == "023 087"


def test_format_grid_produces_eight_digit_form_on_request():
    assert mapping.format_grid(2310.0, 8730.0, digits=8) == "0231 0873"


def test_format_grid_produces_four_digit_form_on_request():
    assert mapping.format_grid(2000.0, 8000.0, digits=4) == "02 08"


def test_format_grid_produces_ten_digit_form_on_request():
    assert mapping.format_grid(2310.0, 8730.0, digits=10) == "02310 08730"


def test_format_grid_rejects_unsupported_digit_count():
    with pytest.raises(ValueError):
        mapping.format_grid(2300.0, 8700.0, digits=5)


def test_format_grid_round_trips_through_parse_grid_at_matching_precision():
    original = "045 612"
    x, z = mapping.parse_grid(original)
    assert mapping.format_grid(x, z) == original


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_map_returns_image_of_requested_square_size():
    img = mapping.render_map("arland", [], size=256)
    assert img.size == (256, 256)


def test_render_map_placeholder_base_is_used_when_no_terrain_image_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping, "MAPS_DIR", tmp_path)  # empty dir, no <slug>.png
    img = mapping.render_map("arland", [], size=200)
    assert img.size == (200, 200)
    assert img.mode == "RGB"


def test_render_map_loads_and_resizes_real_terrain_image_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping, "MAPS_DIR", tmp_path)
    real = Image.new("RGB", (64, 64), (10, 20, 30))
    real.save(tmp_path / "arland.png")
    img = mapping.render_map("arland", [], size=300)
    assert img.size == (300, 300)
    # corner pixel should be close to the source image's flat colour, proving the
    # real asset was loaded rather than the generated placeholder being drawn.
    r, g, b = img.getpixel((0, 0))[:3]
    assert (r, g, b) == pytest.approx((10, 20, 30), abs=5)


def test_render_map_draws_a_marker_that_is_visually_distinct_from_the_background():
    terrain = mapping.TERRAINS["arland"]
    size = 400
    background = mapping.render_map("arland", [], size=size)
    marker = mapping.Marker(
        kind="friendly", label="1-1", x=terrain.width_m / 2, z=terrain.height_m / 2
    )
    with_marker = mapping.render_map("arland", [marker], size=size)
    px, py = mapping.world_to_pixel(marker.x, marker.z, terrain, size)
    sample_at_marker = with_marker.getpixel((int(px), int(py)))
    sample_from_background = background.getpixel((int(px), int(py)))
    assert sample_at_marker != sample_from_background


def test_marker_kinds_lists_the_four_recognised_kinds_for_command_choices():
    assert set(mapping.MARKER_KINDS) == {"objective", "friendly", "enemy", "note"}


def test_render_map_draws_each_marker_kind_without_error():
    markers = [
        mapping.Marker(kind="objective", label="Obj A", x=1000, z=1000),
        mapping.Marker(kind="friendly", label="1st Sqd", x=2000, z=2000),
        mapping.Marker(kind="enemy", label="Contact", x=3000, z=3000),
        mapping.Marker(kind="note", label="Watch this", x=4000, z=4000),
        mapping.Marker(kind="unknown-kind", label="Fallback", x=5000, z=5000),
    ]
    img = mapping.render_map("everon", markers, size=512)
    assert img.size == (512, 512)


def test_render_map_clamps_out_of_bounds_markers_into_the_frame_instead_of_raising():
    terrain = mapping.TERRAINS["arland"]
    size = 400
    far_outside = mapping.Marker(kind="enemy", label="Bogey", x=-99999, z=99999)
    img = mapping.render_map("arland", [far_outside], size=size)
    assert img.size == (size, size)
    # clamped marker should be drawn right at the top-left corner region.
    corner = img.getpixel((2, 2))
    background = mapping.render_map("arland", [], size=size).getpixel((2, 2))
    assert corner != background
    _ = terrain  # terrain kept for clarity of intent, not otherwise asserted on


def test_render_to_png_bytes_returns_a_decodable_png():
    data = mapping.render_to_png_bytes("everon", [], size=128)
    assert isinstance(data, bytes)
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG"
    assert img.size == (128, 128)
