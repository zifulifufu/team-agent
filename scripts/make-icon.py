#!/usr/bin/env python3
"""Draw the app icon — the same mark the interface draws, at icon sizes.

Why a generator instead of a checked-in picture: the mark is one shape that has to look the same in
the sidebar, on the home page, in the About panel and in the Dock. A hand-made PNG is a fourth copy
of it that nothing keeps in step, and the moment it drifts the app introduces itself two ways again —
which is exactly what happened, twice: the sidebar drew three nodes in a tile while the middle panel
drew a filled square with a sparkle, and the Dock showed Electron's own icon.

The geometry below is the one in `desktop/src/components/BrandMark.tsx` (a 20-unit view box, tile
inset 1.4 with radius 5, nodes at (10, 6.9), (6.3, 13), (13.7, 13) with radius 2.1). If either
changes, change both — they are two drawings of one shape, not two designs.

Run it after changing the mark:  .venv/bin/python scripts/make-icon.py
It writes `desktop/build/icon.png` (512, for the window and non-macOS) and `desktop/build/icon.icns`
(macOS, built with `iconutil`, so it needs no extra dependency to *use*).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

# The mark, in its own units (see the docstring).
VIEW = 20.0
TILE_INSET = 1.4
TILE_RADIUS = 5.0
NODES = ((10.0, 6.9), (6.3, 13.0), (13.7, 13.0))
NODE_RADIUS = 2.1

# The colours of the default theme (`--primary` / `--on-primary`). An icon is a fixed picture, so it
# takes the one the app ships with rather than following a theme that does not exist outside it.
BACKDROP = (0, 185, 107, 255)
NODE = (255, 255, 255, 255)

MASTER = 4096                      # drawn once at this size, then sampled down for every variant
MARGIN = 0.075                     # how much of the canvas the artwork keeps clear, as macOS icons do
SIZES = (16, 32, 64, 128, 256, 512, 1024)


def draw(size: int) -> Image.Image:
    """The mark at `size` pixels square: a filled tile with the three nodes on it.

    Drawn oversized and sampled down rather than drawn at size, because a 16 px icon with hard edges
    is the one place this shape looks broken — and circles are exactly what goes jagged first.
    """
    scale = size / MASTER
    box = MASTER * (1 - 2 * MARGIN)
    origin = MASTER * MARGIN
    unit = box / (VIEW - 2 * TILE_INSET)          # one mark unit, in master pixels
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    master = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    pen = ImageDraw.Draw(master)
    pen.rounded_rectangle([origin, origin, origin + box, origin + box],
                          radius=TILE_RADIUS * unit, fill=BACKDROP)
    for x, y in NODES:
        cx = origin + (x - TILE_INSET) * unit
        cy = origin + (y - TILE_INSET) * unit
        r = NODE_RADIUS * unit
        pen.ellipse([cx - r, cy - r, cx + r, cy + r], fill=NODE)
    if scale < 1:
        master = master.resize((size, size), Image.LANCZOS)
    image.paste(master, (0, 0), master)
    return image


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "desktop" / "build"
    out.mkdir(parents=True, exist_ok=True)
    master = draw(MASTER)
    master.resize((512, 512), Image.LANCZOS).save(out / "icon.png")

    iconset = Path(tempfile.mkdtemp(prefix="ta-iconset-")) / "icon.iconset"
    iconset.mkdir()
    # The names `iconutil` insists on: each size, and its @2x twin.
    for size in (16, 32, 128, 256, 512):
        master.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        master.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    made = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "icon.icns")],
                          capture_output=True, text=True)
    if made.returncode != 0:
        print("iconutil failed:", made.stderr.strip(), file=sys.stderr)
        return 1
    print(f"wrote {out / 'icon.png'} and {out / 'icon.icns'}")
    for size in SIZES:
        print(f"  {size}px  {master.resize((size, size), Image.LANCZOS).size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
