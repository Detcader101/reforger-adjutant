# assets/

Static, non-code assets for the bot.

## assets/maps/

Real terrain base-layer images go here, one file per terrain slug:

```
assets/maps/everon.png
assets/maps/arland.png
assets/maps/kolguyev.png
```

The slug must match a key in `TERRAINS` in `adjutant/services/mapping.py`.
`mapping.render_map()` checks `assets/maps/<slug>.png` first; if the file
exists it's loaded and resized to the requested render size. If it's
missing, a generated placeholder base layer is used instead (flat muted
green, 1km gridlines with edge labels, terrain name watermark) so `/map`
works and looks intentional before real imagery is sourced.

**Format:** any Pillow-readable image (PNG preferred). Orientation must
match the coordinate convention in `mapping.py`: north (+Z) up, east (+X)
right — i.e. the image's top edge is the terrain's north edge. If a
sourced map image is oriented differently, re-export/rotate it to match
before dropping it in here rather than special-casing orientation in code.

**Confirmed extents** (from the reforger-research pass, not placeholders):
Everon 12800×12800m, Arland 4096×4096m (not 5942 — that figure circulating
elsewhere is a screenshot pixel size, not a world extent), Kolguyev
12800×12800m. These live in `TERRAINS` in `mapping.py`; if a future pass
finds a discrepancy, get the corrected extent alongside the source imagery
for that terrain — mismatched extents will make markers land in the wrong
place on an otherwise-correct image, so they should be verified together,
not in separate passes.

**Still pending:** the imagery itself — no real terrain PNGs exist yet, so
every terrain currently renders the generated placeholder base layer. Real
assets will come later via EnfusionMapMaker-style extraction.

The `MAPS_DIR` path the loader checks is `adjutant/services/mapping.py`'s
`MAPS_DIR` constant, resolved relative to the repo root — don't move this
directory without updating that.
