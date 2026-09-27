"""Drawing a vessel the way a camera sees one, instead of stamping shapes onto a canvas.

The previous drawings were made with `ImageDraw.line` and `ImageDraw.ellipse` filled with flat colour,
and that is why they read as clip art: a flat fill has no volume, a straight line has no anatomy, and
evenly coloured dots have nothing to do with how blood or contrast appears on an image. Reviewing a
rendered frame against a real angiogram produced the same three complaints every time — the vessel is
a straight tube, it is flat, and the "flow" is decoration.

Three ideas replace all of that, and each one is a physical fact rather than a style choice:

* **A vessel is a cylinder.** What a camera records across its width is the *path length* through it:
  `sqrt(1 - u^2)` in normalised offset `u`, which is also the Beer-Lambert factor for a contrast column
  and the silhouette term for a tube of blood. Bright in the middle, vanishing at both edges. Painting
  the cross-section in bands of that profile is what makes a tube look round instead of flat, and it is
  the same function that makes a real angiogram's vessels look round.
* **Blood and contrast are a density that moves.** Not particles, not a coloured smear: a concentration
  field `rho(s, t)` along the centreline, carried downstream and spreading as it goes, moving faster
  during systole than during diastole. Brightness is `path_length x rho`, so the bolus appears as a
  column with a sharp front and a soft tail — which is what an injection actually looks like.
* **Anatomy branches.** An intracranial aneurysm sits at a bifurcation, and a drawing with no
  bifurcation cannot look like one. The trunk splits, the daughters are narrower by Murray's law, and
  every centreline is a smooth curve with a little tortuosity rather than a straight segment.

Everything here is deterministic: no randomness at draw time, so the same parameters always give the
same frame and "make the third loop smaller" is a number to change rather than a re-roll.

Pure Pillow on purpose — this ships inside a desktop app whose dependency list is short, and every
effect below is achievable with strokes, masks and one blur.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

# Where the light is, as a unit vector pointing from the surface toward the lamp. Upper left, because
# that is where every diagram in every textbook puts it and a vessel lit from below reads as a mistake.
LIGHT = (-0.5547, -0.8321)

# How many concentric strokes it takes to fake a continuous cross-section profile. More bands is a
# smoother tube and a slower frame; 14 is past the point where a viewer can see the steps at any size
# this app renders, and it is paid once per still and per radius band per frame.
BANDS = 14

# Levels the flow density is quantised to when it is painted. The column is a gradient along its
# length, and drawing it as 10 runs of constant brightness costs 10 strokes instead of 400 dots.
DENSITY_STEPS = 10


def blend(a: Sequence[int], b: Sequence[int], k: float) -> tuple[int, int, int]:
    """`k` of the way from `a` to `b`, clamped. The one colour operation this module needs."""
    k = max(0.0, min(1.0, float(k)))
    return (int(round(a[0] + (b[0] - a[0]) * k)),
            int(round(a[1] + (b[1] - a[1]) * k)),
            int(round(a[2] + (b[2] - a[2]) * k)))


def path_length(f: float) -> float:
    """`sqrt(1 - u^2)` for a normalised offset `f` from the axis of a cylinder.

    The whole reason a real vessel on a real image is brightest down its middle and fades to nothing
    at its edges. `f` is |offset| / radius, so 0 is the axis and 1 is the wall.
    """
    f = max(0.0, min(1.0, abs(float(f))))
    return math.sqrt(max(0.0, 1.0 - f * f))


# --------------------------------------------------------------------- curves
def catmull(points: Sequence[tuple[float, float]], n: int = 64) -> list[tuple[float, float]]:
    """A smooth curve through every point (Catmull-Rom), sampled `n` times.

    Through, not near: the caller's control points are places the vessel really goes, and a curve that
    only approaches them is how a centreline ends up somewhere nobody asked for.
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return pts * max(1, n)
    if len(pts) == 2:
        return [(pts[0][0] + (pts[1][0] - pts[0][0]) * i / (n - 1),
                 pts[0][1] + (pts[1][1] - pts[0][1]) * i / (n - 1)) for i in range(n)]
    ext = [pts[0]] + pts + [pts[-1]]
    out: list[tuple[float, float]] = []
    segs = len(pts) - 1
    for i in range(n):
        u = i / (n - 1) * segs
        k = min(int(u), segs - 1)
        t = u - k
        p0, p1, p2, p3 = ext[k], ext[k + 1], ext[k + 2], ext[k + 3]
        t2, t3 = t * t, t * t * t
        out.append((
            0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                   + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                   + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
            0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                   + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                   + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)))
    return out


def tortuosity(pts: Sequence[tuple[float, float]], amp: float, waves: float = 1.0,
               phase: float = 0.0) -> list[tuple[float, float]]:
    """Bend a polyline sideways, sinusoidally, so it stops being a rule.

    Real arteries curve. A parameter rather than a random number: the same input has to give the same
    frame, and the reviewer's note "less wiggle on the left" has to be a number.
    """
    if amp <= 0 or len(pts) < 2:
        return list(pts)
    out = []
    for i, (x, y) in enumerate(pts):
        u = i / (len(pts) - 1)
        j = min(max(i - 1, 0), len(pts) - 2)
        dx = pts[j + 1][0] - pts[j][0]
        dy = pts[j + 1][1] - pts[j][1]
        d = math.hypot(dx, dy) or 1.0
        off = amp * math.sin(2 * math.pi * (waves * u + phase)) * math.sin(math.pi * u)
        out.append((x - dy / d * off, y + dx / d * off))
    return out


def offset(pts: Sequence[tuple[float, float]], dx: float, dy: float) -> list[tuple[float, float]]:
    return [(x + dx, y + dy) for x, y in pts]


def along(pts: Sequence[tuple[float, float]], u: float) -> tuple[float, float]:
    """The point `u` of the way along a polyline, by arc length."""
    u = max(0.0, min(1.0, float(u)))
    if len(pts) == 1:
        return pts[0]
    lens = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(lens) or 1.0
    want = u * total
    run = 0.0
    for i, seg in enumerate(lens):
        if run + seg >= want:
            k = (want - run) / seg if seg else 0.0
            return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k,
                    pts[i][1] + (pts[i + 1][1] - pts[i][1]) * k)
        run += seg
    return pts[-1]


def sub(pts: Sequence[tuple[float, float]], u0: float, u1: float,
        step: float = 0.012) -> list[tuple[float, float]]:
    """The part of a polyline between two fractions of its length, resampled evenly."""
    u0, u1 = max(0.0, min(u0, u1)), min(1.0, max(u0, u1))
    if u1 - u0 < 1e-6:
        return [along(pts, u0)]
    n = max(2, int((u1 - u0) / step) + 1)
    return [along(pts, u0 + (u1 - u0) * i / (n - 1)) for i in range(n)]


# --------------------------------------------------------------------- the field behind the drawing
def _lcg(seed: int):
    x = (seed or 1) & 0x7FFFFFFF
    while True:
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        yield x


def grain(size: tuple[int, int], seed: int = 7, cell: int = 6) -> Any:
    """A soft noise image, from a fixed seed. Two jobs: it stops a flat background from looking like a
    PowerPoint slide, and it is what a low-dose fluoroscopic image is actually full of.

    Deterministic (the LCG walks a fixed sequence) so a frame can be reproduced and compared.
    """
    from PIL import Image
    w, h = size
    cw, ch = max(2, w // cell), max(2, h // cell)
    rnd = _lcg(seed)
    tiny = Image.new("L", (cw, ch))
    tiny.putdata([(next(rnd) % 256) for _ in range(cw * ch)])
    return tiny.resize((w, h), Image.BICUBIC)


def backdrop(size: tuple[int, int], top: Sequence[int], bottom: Sequence[int], *,
             vignette: float = 0.5, grain_amp: float = 5.0, seed: int = 7) -> Any:
    """What is behind the vessels: a vertical fall-off, a corner vignette and a little noise.

    Cheap, and it is most of the difference between "a diagram" and "a picture". A flat fill reads as a
    shape on a slide however good the shape is.
    """
    from PIL import Image, ImageChops
    w, h = size
    strip = Image.new("RGB", (1, h))
    strip.putdata([blend(top, bottom, i / max(1, h - 1)) for i in range(h)])
    img = strip.resize((w, h), Image.NEAREST)
    if vignette > 0:
        rnd = _lcg(seed + 11)
        small = Image.new("L", (9, 9))
        small.putdata([255 - int(vignette * 255 * ((1 - (i % 9) / 8) ** 2 + (1 - (i // 9) / 8) ** 2) / 2)
                       for i in range(81)])
        next(rnd)
        img = Image.composite(img, Image.new("RGB", size, (0, 0, 0)),
                              small.resize((w, h), Image.BICUBIC).point(lambda v: v))
        img = Image.composite(img, Image.new("RGB", size, (0, 0, 0)),
                              small.resize((w, h), Image.BICUBIC))
    if grain_amp > 0:
        g = grain(size, seed=seed, cell=max(3, w // 90))
        img = ImageChops.add(img, Image.merge("RGB", (g, g, g)).point(
            lambda v: int(v * grain_amp / 255)))
    return img


# --------------------------------------------------------------------- tubes
def taper(n: int, w0: float, w1: float) -> list[float]:
    """`n` widths running linearly from `w0` to `w1`.

    A vessel that is the same thickness from the aorta to the cortex is a stroke, not an artery. Taper
    is one of the two things (with the bifurcation) that make a drawing read as vasculature, and it is
    free: the ribbon already takes a width per point.
    """
    n = max(2, int(n))
    return [w0 + (w1 - w0) * i / (n - 1) for i in range(n)]


def width_at(width: "float | Sequence[float]", i: int, n: int) -> float:
    """The width for point `i` of `n`, whether the caller gave a number or a per-point list."""
    if isinstance(width, (int, float)):
        return float(width)
    if not width:
        return 0.0
    return float(width[min(i, len(width) - 1)])


def ribbon(pts: Sequence[tuple[float, float]], half_widths: Sequence[float]) -> list[tuple[float, float]]:
    """The outline of a stroke of varying thickness: one side out, the other side back.

    Pillow can only draw a line of *one* width, which is why the first version of this drawing had
    vessels that never changed calibre. A polygon can taper, and it also gets rid of the flat cap at
    each end — the offset outline meets itself at the tips.
    """
    n = len(pts)
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for i, (x, y) in enumerate(pts):
        j0, j1 = max(0, i - 1), min(n - 1, i + 1)
        dx, dy = pts[j1][0] - pts[j0][0], pts[j1][1] - pts[j0][1]
        d = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / d, dx / d
        hw = max(0.5, float(half_widths[min(i, len(half_widths) - 1)]))
        left.append((x + nx * hw, y + ny * hw))
        right.append((x - nx * hw, y - ny * hw))
    return left + right[::-1]


def _poly(pts) -> list[tuple[int, int]]:
    return [(int(round(x)), int(round(y))) for x, y in pts]


def paint_tube(img, pts: Sequence[tuple[float, float]], width: "float | Sequence[float]", *,
               edge: Sequence[int], core: Sequence[int], wall: Sequence[int],
               bands: int = BANDS) -> None:
    """A vessel with volume: a dark wall around a lumen painted by cross-section path length.

    Two layers, and each answers a different reviewer note. The wall is what makes "the catheter is
    *inside* the vessel" visible at all. The banded lumen is `path_length` sampled inward, so the middle
    is brightest and both edges fall to the wall colour — which is the same physics that makes a real
    vessel on a real angiogram look round, and the reason a flat fill never does.
    """
    from PIL import ImageDraw
    n = len(pts)
    if n < 2:
        return
    d = ImageDraw.Draw(img)
    hw = [width_at(width, i, n) / 2.0 for i in range(n)]
    d.polygon(_poly(ribbon(pts, [h * 1.24 for h in hw])), fill=tuple(wall))
    for j in range(bands, 0, -1):
        f = j / bands
        c = blend(core, edge, 1.0 - path_length(f))
        d.polygon(_poly(ribbon(pts, [h * 0.96 * f for h in hw])), fill=c)


def paint_specular(img, pts: Sequence[tuple[float, float]], width: "float | Sequence[float]", *,
                   color: Sequence[int], k: float = 0.4, offset_k: float = 0.46) -> None:
    """Only the highlight streak — no wall, no lumen.

    It has to be its own function, and the reason is a mistake worth recording: the first version
    layered the highlight by calling `paint_tube` a second time with a bright colour, which painted an
    opaque vessel over the flow that had just been drawn inside it. Every frame came out identical and
    the animation looked frozen while the numbers said the bolus was moving.
    """
    from PIL import Image, ImageDraw, ImageFilter
    n = len(pts)
    if n < 2:
        return
    hw = [width_at(width, i, n) / 2.0 * offset_k for i in range(n)]
    sh = ribbon(offset(pts, LIGHT[0] * (hw[0] if hw else 1), LIGHT[1] * (hw[0] if hw else 1)),
                [max(0.6, h * 0.42) for h in hw])
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).polygon(_poly(sh), fill=tuple(color) + (int(round(255 * max(0.0, min(1.0, k)))),))
    layer = layer.filter(ImageFilter.GaussianBlur(max(1.0, (hw[len(hw) // 2] if hw else 2) * 0.55)))
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))


def paint_stream(img, pts: Sequence[tuple[float, float]], width: "float | Sequence[float]",
                 dens, *, dim: Sequence[int], bright: Sequence[int],
                 bands: int = BANDS, steps: int = DENSITY_STEPS, blur: float = 0.0) -> None:
    """Blood or contrast moving inside a lumen, as a density — not as dots.

    `dens` is the concentration. It may be a sequence aligned with `pts`, or a function of
    `(s, f)` — position along the path and normalised radius — which is how a caller says "the lamina
    half way out to the wall is moving at 75% of the axial speed". That second form is what makes flow
    look like flow; see `laminar`.

    The colour at radius band `f` and position `s` is that band's own cross-section colour pulled toward
    `bright` by the density there — `path_length x rho`, written the way a renderer can afford: the
    column is brightest down the middle *and* brightest where the bolus is, which is what an injection
    actually looks like on a run.

    Two details are here because a reviewer found them missing. The whole layer is drawn on a `Patch`
    and blurred before it lands, because painting a gradient in discrete steps leaves the steps visible
    and a bolus with a ruler-straight front is not a bolus. And it is masked to the lumen afterwards, so
    blurring it does not let it bleed through the vessel wall.
    """
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    n = len(pts)
    if n < 2:
        return
    pts = list(pts)
    if not callable(dens):
        n = min(n, len(dens))
        if n < 2:
            return
        pts, dens = pts[:n], [max(0.0, min(1.0, float(v))) for v in dens[:n]]
    hw = [width_at(width, i, n) / 2.0 * 0.48 for i in range(n)]
    pad = max(hw) * 1.6 + 4.0
    p = Patch(img, (min(x for x, _ in pts) - pad, min(y for _, y in pts) - pad,
                    max(x for x, _ in pts) + pad, max(y for _, y in pts) + pad), scale=1)
    d = ImageDraw.Draw(p.layer)
    lpts = p.points(pts)
    lhw = [p.length(h) for h in hw]
    # Widest and dimmest first, narrowing and brightening inward: the cross-section is painted
    # outside-in so each stroke lands on top of a slightly wider, slightly darker one.
    for j in range(bands, 0, -1):
        f = j / bands
        if lhw[n // 2] * f < 1.0:
            continue
        band = blend(dim, bright, path_length(f))
        dv = (dens if not callable(dens)
              else [max(0.0, min(1.0, float(dens(i / max(1, n - 1), f)))) for i in range(n)])
        # Faintest concentration first, densest last — **ascending**, because these runs are nested and
        # the bright middle of the bolus has to end up on top of its own dim fringe. Painting them the
        # other way round leaves a column that is uniformly dim, which reads as "nothing is moving".
        for k in range(1, steps + 1):
            thr = k / steps
            lo = None
            for i in range(n + 1):
                on = i < n and dv[i] >= thr - 1e-9
                if on and lo is None:
                    lo = i
                elif not on and lo is not None:
                    if i - lo >= 2:
                        a, b = max(0, lo - 1), min(n, i + 1)
                        seg, hseg = lpts[a:b], [h * f for h in lhw[a:b]]
                        if len(seg) >= 2 and max(hseg) >= 0.5:
                            d.polygon(_poly(ribbon(seg, hseg)), fill=blend(dim, band, thr) + (255,))
                    lo = None
    # A round leading edge, so the front of the bolus is a front rather than a cut-off.
    for i in (0, n - 1):
        v = dv[i] if not callable(dens) else max(0.0, min(1.0, float(dens(i / max(1, n - 1), 0.5))))
        if v > 0.08:
            x, y = lpts[i]
            r = max(1.0, lhw[i] * (0.55 + 0.45 * v))
            d.ellipse([int(x - r), int(y - r), int(x + r), int(y + r)],
                      fill=blend(dim, bright, v * 0.9) + (255,))
    if blur > 0:
        p.layer = p.layer.filter(ImageFilter.GaussianBlur(p.length(blur)))
    keep = Image.new("L", p.layer.size, 0)
    ImageDraw.Draw(keep).polygon(_poly(ribbon(lpts, [h * 1.02 for h in lhw])), fill=255)
    p.layer.putalpha(ImageChops.multiply(p.layer.getchannel("A"), keep))
    p.close()


# --------------------------------------------------------------------- the aneurysm
def dome_polygon(center: tuple[float, float], rx: float, ry: float, *,
                 neck: float = 0.42, wobble: float = 0.055, n: int = 160) -> list[tuple[float, float]]:
    """An aneurysm as an irregular dome on a neck, instead of a perfect ellipse hanging off a pipe.

    Two things were wrong with the ellipse. A berry aneurysm is not round — it is a lobulated dome
    whose wall has grown unevenly — and it does not float: it opens into the artery through a neck, and
    the neck's width relative to the dome is what decides whether a coil will stay in it. `wobble` is
    deterministic (three fixed harmonics), so the shape is reproducible and adjustable.
    """
    cx, cy = center
    out: list[tuple[float, float]] = []
    for i in range(n + 1):
        ang = math.pi + math.pi * (i / n)
        w = 1.0 + wobble * (math.sin(3.0 * ang + 0.7) + 0.6 * math.sin(5.0 * ang + 2.1))
        # The lower part of the sweep narrows into the neck instead of closing the circle.
        k = max(0.0, math.cos(ang - math.pi * 1.5))
        narrow = neck + (1.0 - neck) * k
        out.append((cx + math.cos(ang) * rx * w * narrow,
                    cy - abs(math.sin(ang)) * ry * w))
    return out


# --------------------------------------------------------------------- the tree
def branches(trunk: Sequence[tuple[float, float]], neck: float, width: float,
             *, size: tuple[int, int], spread: float = 0.34, tortuosity_amp: float = 0.035,
             perfs: int = 2, seed: int = 3) -> list[dict]:
    """The artery, as a tree: the trunk, a bifurcation at the neck, and a couple of small branches.

    Why this exists: an intracranial aneurysm is a disease of a *bifurcation* — the wall gives way at
    the apex where flow divides — and a drawing with one straight tube and an ellipse stuck on top
    cannot look like one no matter how well it is shaded. Radii follow Murray's law
    (`r_parent^3 = r_d1^3 + r_d2^3`), which is why real arteries taper the way they do at a split.

    Returns segments in painting order (widest first), each `{pts, width, depth}`.
    """
    w, h = size
    pts = list(trunk)
    apex = pts[min(len(pts) - 1, max(0, neck))]
    # Daughters leave the apex and run off the right edge, above and below the trunk, so the frame reads
    # as "flow arrives, divides, and carries on" rather than "a pipe with a bump".
    rnd = _lcg(seed)
    out: list[dict] = [{"pts": pts, "width": taper(len(pts), width * 1.10, width * 0.88), "depth": 0}]
    # Murray's law with an even split: each daughter carries half the flow, so r = R / 2^(1/3). It is
    # also why the sum of the daughters' areas exceeds the parent's — the reason an artery looks *wider*
    # at a bifurcation — and it costs one line.
    dw = width / (2.0 ** (1.0 / 3.0))
    for sign, ph in ((-1.0, 0.0), (1.0, 0.5)):
        end = (w * 1.02, apex[1] + sign * h * spread * (0.72 + 0.5 * (next(rnd) % 100) / 100.0))
        c1 = (apex[0] + (end[0] - apex[0]) * 0.30, apex[1] + sign * h * spread * 0.18)
        c2 = (apex[0] + (end[0] - apex[0]) * 0.68, apex[1] + sign * h * spread * 1.05)
        d = catmull([apex, c1, c2, end], 96)
        d = tortuosity(d, tortuosity_amp * h, waves=0.85, phase=ph)
        out.append({"pts": d, "width": taper(len(d), dw, dw * 0.62), "depth": 1})
        # Small branches off each daughter: the perforators are what make a vessel tree look like a tree,
        # and they are also what a reader silently misses when a drawing has only two tubes.
        for k in range(max(0, perfs)):
            start = along(d, 0.42 + 0.26 * k)
            tip = (start[0] + w * 0.20 * (1.0 - 0.35 * k), start[1] + sign * h * 0.16 * (1.0 + 0.4 * k))
            p = catmull([start, (start[0] + w * 0.07, start[1] + sign * h * 0.05), tip], 44)
            out.append({"pts": p, "width": taper(len(p), width * 0.30, width * 0.14), "depth": 2})
    return out


def tree_extent(segs: Iterable[dict], size: tuple[int, int]) -> tuple[float, float]:
    """Leftmost and topmost of a set of segments — for placing a heading inside the drawing."""
    xs = [x for s in segs for x, _ in s["pts"]]
    ys = [y for s in segs for _, y in s["pts"]]
    return (min(xs) if xs else 0.0, min(ys) if ys else 0.0)


def sphere_profile(f: float) -> float:
    """`sqrt(1 - u^2)` again, but this is the silhouette of a *sphere* seen from the front.

    Kept as a named alias rather than inlined because the two uses are different ideas that happen to
    share a formula: a tube's cross-section and a dome's curvature. A reader who finds one call site
    should not have to work out which of the two it means.
    """
    return path_length(f)


# --------------------------------------------------------------------- supersampled patches
class Patch:
    """A scratch canvas over one region of an image, drawn supersampled and pasted back once.

    This exists because of a trade the renderer has to make. Drawing everything at 2x and shrinking the
    finished frame is the only way to get clean edges — but at 1080x1920 that is a quarter of a
    gigapixel of work per frame, and a 40-second shot is a thousand frames. So the big static parts are
    supersampled once and kept, and the parts that move are drawn on a scratch canvas covering only
    themselves, at 2x, and shrunk on the way in. A dome is a few hundred pixels across; its supersampled
    copy costs what it should.

    Coordinates inside a patch are the patch's own. `at()` converts a frame coordinate.
    """

    def __init__(self, img, box: tuple[float, float, float, float], *, scale: int = 2,
                 alpha: bool = True) -> None:
        from PIL import Image
        w, h = img.size
        x0 = max(0, int(math.floor(box[0])))
        y0 = max(0, int(math.floor(box[1])))
        x1 = min(w, int(math.ceil(box[2])))
        y1 = min(h, int(math.ceil(box[3])))
        self.scale = max(1, int(scale))
        self.box = (x0, y0, x1, y1)
        self.origin = (x0, y0)
        self.size = ((x1 - x0) * self.scale, (y1 - y0) * self.scale)
        self.layer = Image.new("RGBA" if alpha else "RGB", self.size, (0, 0, 0, 0))
        self._img = img
        self._alpha = alpha

    def at(self, x: float, y: float) -> tuple[float, float]:
        """A frame coordinate, in this patch's own (supersampled) coordinates."""
        return ((x - self.origin[0]) * self.scale, (y - self.origin[1]) * self.scale)

    def points(self, pts) -> list[tuple[float, float]]:
        return [self.at(x, y) for x, y in pts]

    def length(self, v: float) -> float:
        return v * self.scale

    def close(self) -> None:
        """Shrink the patch and composite it into the image it was cut from."""
        from PIL import Image
        if self.layer.size != (self.box[2] - self.box[0], self.box[3] - self.box[1]):
            self.layer = self.layer.resize((self.box[2] - self.box[0], self.box[3] - self.box[1]),
                                           Image.LANCZOS)
        if self._alpha:
            region = self._img.crop(self.box)
            self._img.paste(Image.alpha_composite(region.convert("RGBA"), self.layer).convert("RGB"),
                            self.box)
        else:
            self._img.paste(self.layer, self.box)


def paint_dome(img, center: tuple[float, float], rx: float, ry: float, *,
               edge: Sequence[int], core: Sequence[int], highlight: Sequence[int],
               outline: Sequence[int], neck: float = 0.42, wobble: float = 0.055,
               bands: int = 26, rim: float = 0.34, shadow: float = 0.38, scale: int = 2) -> None:
    """The sac, shaded as a rounded volume instead of filled flat.

    A flat ellipse with a stroke around it is a sticker, which is what a reviewer called it. A real
    berry aneurysm is a lobulated dome, and it is darker where it turns away from the light and where it
    meets the neck. So: shrinking rings offset toward the lamp (which puts the highlight off-centre, the
    way a sphere's is), softened so the rings do not read as rings, a blurred shadow at the neck, and
    one small specular.

    Drawn through a `Patch`, so the many bands it takes to look smooth cost a canvas the size of the
    dome rather than of the frame.
    """
    from PIL import Image, ImageDraw, ImageFilter
    if rx <= 0 or ry <= 0:
        return
    cx, cy = center
    pad = max(rx, ry) * 0.6
    p = Patch(img, (cx - rx - pad, cy - ry - pad, cx + rx + pad, cy + ry + pad), scale=scale)
    lcx, lcy = p.at(cx, cy)
    srx, sry = p.length(rx), p.length(ry)

    rings = Image.new("RGBA", p.layer.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    for j in range(bands, 0, -1):
        f = j / bands
        px = lcx + LIGHT[0] * srx * 0.17 * (1.0 - f)
        py = lcy - sry * 0.30 + LIGHT[1] * sry * 0.13 * (1.0 - f)
        rd.polygon(_poly(dome_polygon((px, py), srx * f * 0.97, sry * f * 0.97,
                                      neck=neck, wobble=wobble)),
                   fill=tuple(blend(edge, core, (1.0 - rim) + rim * sphere_profile(f))) + (255,))
    rings = rings.filter(ImageFilter.GaussianBlur(max(1.0, srx * 0.045)))
    silhouette = Image.new("L", p.layer.size, 0)
    ImageDraw.Draw(silhouette).polygon(
        _poly(dome_polygon((lcx, lcy), srx, sry, neck=neck, wobble=wobble)), fill=255)
    p.layer.paste(rings, (0, 0), silhouette)

    if shadow > 0:
        lay = Image.new("RGBA", p.layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(lay).ellipse(
            [int(lcx - srx * 1.0), int(lcy + sry * 0.18), int(lcx + srx * 1.0), int(lcy + sry * 1.05)],
            fill=(0, 0, 0, int(round(255 * shadow))))
        lay = lay.filter(ImageFilter.GaussianBlur(max(1.0, srx * 0.20)))
        p.layer = Image.alpha_composite(
            p.layer, Image.composite(lay, Image.new("RGBA", p.layer.size, (0, 0, 0, 0)), silhouette))
    # The outline is drawn over the *upper* sweep only. A closed stroke around the whole dome puts a
    # straight line across its base, and a reviewer read that as a chord cutting the sac off from the
    # artery it is growing out of. Leaving the base open is what lets the two read as one wall.
    outline_pts = _poly(dome_polygon((lcx, lcy), srx, sry, neck=neck, wobble=wobble))
    skip = max(1, int(len(outline_pts) * 0.085))
    arc = outline_pts[skip:len(outline_pts) - skip + 1]
    if len(arc) > 2:
        ImageDraw.Draw(p.layer).line(arc, fill=tuple(outline) + (255,),
                                     width=max(2, int(round(srx * 0.082))), joint="curve")
    if highlight is not None:
        hr = max(2, int(srx * 0.17))
        hx = lcx + LIGHT[0] * srx * 0.46
        hy = lcy - sry * 0.40 + LIGHT[1] * sry * 0.24
        lay = Image.new("RGBA", p.layer.size, (0, 0, 0, 0))
        ImageDraw.Draw(lay).ellipse([int(hx - hr), int(hy - hr * 0.78),
                                     int(hx + hr), int(hy + hr * 0.78)],
                                    fill=tuple(highlight) + (140,))
        lay = lay.filter(ImageFilter.GaussianBlur(max(1.0, hr * 0.6)))
        p.layer = Image.alpha_composite(
            p.layer, Image.composite(lay, Image.new("RGBA", p.layer.size, (0, 0, 0, 0)), silhouette))
    p.close()


# --------------------------------------------------------------------- flow
def pulse(t: float, hz: float = 1.15, amp: float = 0.38) -> float:
    """A cardiac waveform: `1 + amp x p(t)`, in [1 - amp, 1 + amp].

    Two humps, not one, because that is what a pulse is: the systolic peak, then the dicrotic notch and
    the smaller second wave as the aortic valve closes. Blood in an artery is faster during systole
    than during diastole, which is why a real injection has a rhythm instead of a constant speed — the
    single most recognisable thing about flow on a run.
    """
    ph = (t * hz) % 1.0
    sys = math.exp(-((ph - 0.16) ** 2) / (2 * 0.055 ** 2))
    dic = 0.55 * math.exp(-((ph - 0.36) ** 2) / (2 * 0.070 ** 2))
    return 1.0 + amp * (sys + dic - 0.55)


def laminar(f: float) -> float:
    """How fast the lamina at normalised radius `f` travels, relative to the one on the axis.

    Blood in an artery does not move as a plug. Poiseuille flow is a parabola: fastest down the middle,
    stationary against the wall, and the axial speed is twice the mean. So a blob of contrast that
    started as a straight bar across the lumen ends up as a parabola, and it is the *shear* between
    laminae that makes moving blood look like moving blood.

    This is the correction that turned a shot a reviewer called "five identical stills" into a moving
    one, and it is the physical fact behind the fix rather than a trick: nothing was added to the
    picture except the velocity profile real blood has.
    """
    f = max(0.0, min(1.0, abs(float(f))))
    return 1.0 - f * f


def _sigmoid(x: float) -> float:
    """A logistic that does not overflow for large |x|."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


_PHASE_CACHE: dict[tuple[float, float, float, int], list[float]] = {}


def phase_of(t: float, *, speed: float = 1.0, hz: float = 1.15, amp: float = 0.38,
             n: int = 256) -> float:
    """How far the flow has travelled by time `t` — the integral of a pulsatile velocity.

    Deliberately not `speed x t`. Blood does not move at a constant rate: it surges in systole and eases
    in diastole, so where a slug *is* is the integral of that waveform and not a straight line. Advancing
    a pattern linearly while also claiming a pulse would be two stories contradicting each other, and the
    rhythm is exactly what makes markers read as driven by a heart.

    Cached by (speed, hz, amp) because it is a few hundred additions and it is asked for once per frame
    per branch.
    """
    key = (round(speed, 4), round(hz, 4), round(amp, 4), n)
    table = _PHASE_CACHE.get(key)
    if table is None:
        table = [0.0] * (n + 1)
        acc = 0.0
        for i in range(n):
            acc += pulse(i / n, hz=hz, amp=amp) / n
            table[i + 1] = acc
        _PHASE_CACHE[key] = table
    t = max(0.0, min(1.0, float(t)))
    k = t * n
    i = int(k)
    frac = k - i
    span = table[min(n, i + 1)] - table[min(n, i)]
    return (table[min(n, i)] + span * frac) * speed


# Where the slugs sit when `phase` is 0, and how wide each one is relative to the others. Deliberately
# uneven, and this is a correction with a measurement behind it: with three slugs evenly spaced at 1/3
# apart, the pattern repeats every third of the vessel — so a shift of almost exactly one third (which is
# what a plausible flow speed produces over part of a shot) lands the picture back on itself, and two
# frames half a shot apart come out looking like the same frame. A reviewer read that as "nothing is
# moving". Uneven spacing makes the pattern's period the whole vessel, so any real shift changes it.
SLUGS = (0.03, 0.24, 0.46, 0.63, 0.86)
SLUG_SIZES = (1.05, 0.70, 1.45, 0.85, 1.15)


def flow_train_at(s: float, phase: float, *, width: float = 0.11, diff: float = 0.009,
                  floor: float = 0.30, tail: float = 2.6) -> float:
    """Blood already on its way: columns of denser blood travelling down the vessel.

    `phase` is the distance travelled (see `phase_of`), not time — the caller owns the timing, so this
    stays a pure function of position and phase.

    Not the same thing as `bolus`, and the difference matters. An injected bolus is one countable event
    with a beginning and an end; blood that is simply flowing never stops arriving, so the pattern wraps:
    a column leaving the far end is replaced by one entering the near end.

    The shape is a **column, not a bump**, and that is the point. A Gaussian blob of density whose peak
    slides downstream is a *brightness wave*: it brightens and dims a region in place, and a reviewer
    watching eight consecutive frames of exactly that described it as "breathing, not flowing". What the
    eye actually tracks is an **edge** — the steep front of a bolus, arriving at a place and then being
    past it. So each column has a front as sharp as `diff` allows, a flat body, and a longer, softer
    trailing tail (`tail`), which is also the asymmetry a real injection has.

    The columns differ in length and are unevenly spaced (`SLUGS`, `SLUG_SIZES` — fixed values, never
    random). Both matter: identical blobs at regular intervals repeat too soon and give the eye nothing
    distinguishable to follow.
    """
    best = 0.0
    for pos, size in zip(SLUGS, SLUG_SIZES):
        behind = (((pos + phase) % 1.0) - s) % 1.0     # 0 at the front, growing upstream behind it
        head = _sigmoid(behind / max(1e-4, diff))
        w = width * size
        foot = _sigmoid((w - behind) / max(1e-4, diff * tail))
        best = max(best, head * foot)
    return max(0.0, min(1.0, floor + (1.0 - floor) * best))


def flow_train(n: int, phase: float, **kw) -> list[float]:
    """`flow_train_at` sampled `n` times along the whole centreline."""
    return [flow_train_at(i / max(1, n - 1), phase, **kw) for i in range(n)]


def bolus_at(s: float, lead: float, *, width: float = 0.45, diff: float = 0.018,
             floor: float = 0.0) -> float:
    """Contrast concentration at arc position `s` (0..1), given the leading edge's position.

    A column of contrast enters at the near end, travels downstream, and spreads at both of its edges.
    That one model produces every feature a real injection has — a steep leading edge, a longer trailing
    tail, a body still arriving behind the front — and it is the difference between "an injection" and
    "a dot moving along a line".

    `lead` is a position, not a time, and the caller gets it from `phase_of`. Two sign conventions are
    easy to get backwards and both were, once, in the first draft: the column occupies the interval
    *behind* the leading edge (`lead - width .. lead`), and the leading edge starts outside the vessel so
    that frame 0 has no contrast in it yet.
    """
    spread = max(1e-4, diff)
    head = _sigmoid((lead - s) / spread)
    tail = _sigmoid((s - (lead - width)) / spread)
    return max(0.0, min(1.0, floor + (1.0 - floor) * head * tail))


def bolus(n: int, lead: float, **kw) -> list[float]:
    """`bolus_at` sampled `n` times along the whole centreline."""
    return [bolus_at(i / max(1, n - 1), lead, **kw) for i in range(n)]




def wave(t: float, *, speed: float = 0.42, floor: float = 0.22, hz: float = 1.15,
         amp: float = 0.38) -> float:
    """A density that only breathes — for a vessel drawn as already carrying blood.

    No front, no tail: just the concentration rising and falling at heart rate. It is what makes a
    "this artery still supplies the brain" shot move without pretending an injection is happening.
    """
    k = pulse(t, hz=hz, amp=amp) * speed
    return max(0.0, min(1.0, floor + (1.0 - floor) * min(1.0, k)))
