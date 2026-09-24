"""Pictures that teach: a frame made from real material, and schematics drawn here.

Two things live in this module, and they are the two legitimate ways to show something medical
without asking a generative model to imagine it:

  * **A frame built on a real picture.** A real angiogram, plate or photograph, put on a canvas with
    a heading, a caption and a line naming where the picture came from and who owns it. The pictures
    people already have are correct, already reviewed, and already captioned — the only thing missing
    is the presentation that makes one usable inside a film: no software chrome or clinical shorthand
    in shot, a heading that says what we are looking at, an arrow where to look, and an honest
    credit line.
  * **A schematic drawn from primitives.** For explaining a procedure to a general audience, a clean
    drawing that reads at a glance beats a photograph: a vessel, a bulge on its wall, a catheter, a
    coil, an arrow showing flow. Drawn here — deterministically, from the same parameters every time —
    it cannot invent anatomy it was not told about, and it cannot produce the "that is not what an
    aneurysm looks like" failure that a text-to-video model produces by design. Every schematic
    carries a line saying it is a drawing, because a drawing that pretends to be a picture is the
    other way to mislead.

Coordinates in the public functions are **normalised** (0..1 of the canvas) so a caller that cannot
measure a picture — which is every model — can still point at the right place by reading the caption
or the figure's own description.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from . import i18n

# Tried in order; the first that Pillow can open wins. PingFang is absent on this machine and
# Hiragino Sans GB is present, which is exactly why the list is walked rather than assumed.
FONTS = ("/System/Library/Fonts/PingFang.ttc",
         "/System/Library/Fonts/Hiragino Sans GB.ttc",
         "/System/Library/Fonts/STHeiti Medium.ttc",
         "/System/Library/Fonts/Supplemental/Songti.ttc",
         "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
         "/System/Library/Fonts/Helvetica.ttc")

COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MARKS = ("arrow", "circle", "label", "inset")

INK = (12, 16, 22)            # the canvas behind a picture
BAND = (22, 32, 46)           # the heading band
ACCENT = (46, 124, 246)       # one accent, used for rules and marks
PAPER = (240, 244, 250)
MUTED = (150, 165, 185)
DRAWING_BG = (11, 20, 34)     # schematics sit on a deeper blue

# What a drawing is made of. Deliberately a small vocabulary: each shape is something a person can
# check at a glance, and "add a new shape" is a smaller job than "make the model draw better".
VESSEL = (196, 62, 58)
VESSEL_DARK = (126, 32, 34)
SAC = (214, 84, 78)
COIL = (236, 240, 248)
CATHETER = (108, 176, 255)
FLOW = (120, 208, 255)


class FigureError(Exception):
    """Something the caller can act on: a picture that is not there, a mark that makes no sense."""


# --------------------------------------------------------------------- typography
def font(size: int):
    """The first font this machine really has, at `size`. Falls back to Pillow's own bitmap font so a
    machine with none of the candidates still produces a frame rather than an exception."""
    from PIL import ImageFont
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def wrap(text: str, f, limit: int, draw) -> list[str]:
    """Break a line to fit `limit` pixels, character by character.

    Characters rather than words because Chinese has no spaces: a word-based wrap puts a whole
    sentence on one line and it runs off the frame.
    """
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=f) <= limit:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        lines.append(cur)
    return [ln for ln in lines if ln.strip()] or [""]


def shadow(draw, xy, text, f, fill, anchor="mm") -> None:
    """Text with a drop shadow. A plain white line over a bright angiogram is unreadable, and this is
    the text a viewer will look at most."""
    x, y = xy
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1)):
        draw.text((x + dx, y + dy), text, font=f, fill=(0, 0, 0, 180), anchor=anchor)
    draw.text(xy, text, font=f, fill=fill, anchor=anchor)


def rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) if COLOR_RE.match(str(value or "")) \
        else fallback


# --------------------------------------------------------------------- the canvas
def _band_heights(size: tuple[int, int], *, head: bool, foot: str) -> tuple[int, int]:
    w, h = size
    return (int(h * 0.085) if head else 0), (int(h * 0.075) + int(h * 0.028) * (2 if foot else 0))


def compose(body_path: Path | None, out: Path, size: tuple[int, int], *,
            heading: str = "", caption: str = "", credit: str = "", draw=None,
            body_fit: str = "full", ruler: bool = False, marks: list | None = None) -> Path:
    """Put a picture, or a drawing, on a titled canvas with a caption and a credit line.

    Everything about the layout exists for one reason: a picture dropped into a film with no heading
    and no source is a picture nobody can check. The heading says what we are looking at, the caption
    says what to notice, and the credit line says where it came from and under what terms — the answer
    to the question every published film eventually gets asked.

    `draw` is called with `(image, draw)` for a schematic; supplying it means `body_path` is unused.
    """
    from PIL import Image, ImageDraw
    w, h = size
    head_h, foot_h = _band_heights(size, head=bool(heading), foot=(caption or credit))
    canvas = Image.new("RGB", size, INK)
    d = ImageDraw.Draw(canvas)

    avail_h = h - head_h - foot_h
    if draw is not None:
        plate = Image.new("RGB", size, DRAWING_BG)
        draw(plate, ImageDraw.Draw(plate))
        canvas.paste(plate, (0, 0))
        d = ImageDraw.Draw(canvas)
    elif body_path is not None:
        img = Image.open(body_path).convert("RGB")
        box_w, box_h = w - 80, avail_h - 40
        if body_fit == "full":                      # never crop: a cropped angiogram loses the part
            fitted = img.copy()                     # somebody was going to point at
            fitted.thumbnail((box_w, box_h), Image.LANCZOS)
        else:
            scale = max(box_w / img.width, box_h / img.height)
            fitted = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                                Image.LANCZOS)
            left = (fitted.width - box_w) // 2
            top = (fitted.height - box_h) // 2
            fitted = fitted.crop((left, top, left + box_w, top + box_h))
        if ruler:
            fitted = _with_ruler(fitted)
        canvas.paste(fitted, ((w - fitted.width) // 2, head_h + (avail_h - fitted.height) // 2))

    if heading:
        d.rectangle([0, 0, w, head_h], fill=BAND)
        d.rectangle([0, head_h - 4, w, head_h], fill=ACCENT)
        f = font(int(h * 0.026))
        d.text((w // 2, head_h // 2), wrap(heading, f, w - 80, d)[0], font=f, fill=(255, 255, 255),
               anchor="mm")

    if caption or credit:
        y = h - foot_h
        d.rectangle([0, y - 4, w, y], fill=ACCENT)
        d.rectangle([0, y, w, h], fill=INK)
        ty = y + 18
        f_cap = font(int(h * 0.0215))
        for line in wrap(caption, f_cap, w - 80, d)[:2]:
            d.text((40, ty), line, font=f_cap, fill=PAPER)
            ty += int(h * 0.028)
        if credit:
            d.text((40, h - int(h * 0.032)), wrap(credit, font(int(h * 0.016)), w - 80, d)[0],
                   font=font(int(h * 0.016)), fill=MUTED)
    # Marks last, on the finished canvas: they point at what is actually on screen — including at a
    # magnified inset, which is drawn from the canvas it sits on.
    if marks:
        apply_marks(canvas, marks, size)
    canvas.save(out)
    return out


def _with_ruler(img):
    """A scale bar, drawn only when the caller knows the width it represents. A ruler that is guessed
    is worse than no ruler: it is a number somebody might quote."""
    from PIL import ImageDraw
    w, h = img.size
    d = ImageDraw.Draw(img)
    x0, x1 = int(w * 0.06), int(w * 0.16)
    y = h - int(h * 0.06)
    d.rectangle([x0 - 6, y - int(h * 0.03), x1 + 6, y + int(h * 0.022)], fill=(0, 0, 0))
    d.line([x0, y, x1, y], fill=(255, 255, 255), width=max(2, h // 300))
    return img


# --------------------------------------------------------------------- marks on a real picture
def _px(at: Any, size: tuple[int, int], what: str) -> tuple[int, int]:
    """A normalised point as pixels, checked. Refusing a point outside the frame is deliberate: a
    mark that lands off-canvas is a caller error worth hearing about, not a silent no-op."""
    if not isinstance(at, (list, tuple)) or len(at) != 2:
        raise FigureError(i18n.pick_now(
            f"{what}: \"at\" has to be two numbers between 0 and 1 (x, y) — the point in the frame "
            "you mean, measured from the top-left.",
            f"{what}:「at」要写成 0 到 1 之间的两个数(x, y)—— 你指的是画面上哪个位置,从左上角量。"))
    try:
        x, y = float(at[0]), float(at[1])
    except (TypeError, ValueError):
        raise FigureError(i18n.pick_now(f"{what}: \"at\" is not a pair of numbers.",
                                        f"{what}:「at」不是两个数字。")) from None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise FigureError(i18n.pick_now(
            f"{what}: \"at\" is outside the frame ({x}, {y}). Give a fraction of the picture, not "
            "pixels.",
            f"{what}:「at」落在画面之外({x}, {y})。请给画面的比例,不要给像素。"))
    w, h = size
    return int(x * w), int(y * h)


def apply_marks(canvas, marks: list, size: tuple[int, int], taken: list | None = None) -> None:
    """Draw arrows, circles, labels and magnified insets. This is what turns a correct picture into a
    teaching picture: without a mark, the viewer does not know which of the forty things on an
    angiogram they are supposed to be looking at."""
    from PIL import Image, ImageDraw
    w, h = size
    d = ImageDraw.Draw(canvas)
    f = font(int(h * 0.0165))
    taken = taken if taken is not None else []
    for i, m in enumerate(marks or [], 1):
        if not isinstance(m, dict):
            raise FigureError(i18n.pick_now(f"Mark {i} is not an object.", f"第 {i} 个标注不是一条记录。"))
        kind = str(m.get("kind") or "").strip().lower()
        if kind not in MARKS:
            raise FigureError(i18n.pick_now(
                f"Mark {i}: \"{kind}\" is not a kind of mark. Use one of: {', '.join(MARKS)}.",
                f"第 {i} 个标注:「{kind}」不是标注类型。可用:{', '.join(MARKS)}。"))
        label = str(m.get("text") or "").strip()
        if kind == "arrow":
            a = _px(m.get("from"), size, i18n.pick_now(f"Mark {i} (arrow)", f"第 {i} 个标注(箭头)"))
            b = _px(m.get("at"), size, i18n.pick_now(f"Mark {i} (arrow)", f"第 {i} 个标注(箭头)"))
            d.line([a, b], fill=ACCENT, width=max(3, h // 220))
            _head(d, a, b, max(9, h // 62), ACCENT)
            if label:
                _tag(d, label, a, f, size, taken)
        elif kind == "circle":
            c = _px(m.get("at"), size, i18n.pick_now(f"Mark {i} (circle)", f"第 {i} 个标注(圈注)"))
            r = int(float(m.get("radius") or 0.12) * min(w, h))
            d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=ACCENT,
                      width=max(3, h // 240))
            if label:
                _tag(d, label, (c[0] + r, c[1] - r), f, size, taken)
        elif kind == "label":
            at = _px(m.get("at"), size, i18n.pick_now(f"Mark {i} (label)", f"第 {i} 个标注(标注)"))
            _tag(d, label or "?", at, f, size, taken)
        else:                                        # inset
            c = _px(m.get("at"), size, i18n.pick_now(f"Mark {i} (inset)", f"第 {i} 个标注(放大)"))
            zoom = max(1.2, min(float(m.get("zoom") or 2.5), 8.0))
            sw, sh = int(w / zoom * 0.42), int(h / zoom * 0.42)
            src = canvas.crop((max(0, c[0] - sw // 2), max(0, c[1] - sh // 2),
                               min(w, c[0] + sw // 2), min(h, c[1] + sh // 2)))
            iw, ih = int(w * 0.34), int(w * 0.34 * (sh / max(1, sw)))
            src = src.resize((iw, ih), Image.LANCZOS)
            corner = str(m.get("corner") or "tr")
            x = (w - iw - 28) if corner in ("tr", "br") else 28
            y = (28 + int(h * 0.09)) if corner in ("tr", "tl") else (h - ih - 28 - int(h * 0.11))
            d.rectangle([c[0] - sw // 2, c[1] - sh // 2, c[0] + sw // 2, c[1] + sh // 2],
                        outline=ACCENT, width=max(2, h // 400))
            canvas.paste(src, (x, y))
            d.rectangle([x, y, x + iw, y + ih], outline=ACCENT, width=max(4, h // 300))
            d.line([(x + iw // 2, y + (ih if corner in ("tr", "tl") else 0)),
                    (c[0], c[1] - sh // 2 if corner in ("tr", "tl") else c[1] + sh // 2)],
                   fill=ACCENT, width=max(2, h // 500))
            if label:
                d.text((x + iw // 2, y + ih + 16), wrap(label, f, iw, d)[0], font=f, fill=PAPER,
                       anchor="ma")


def _head(d, a, b, size: int, color) -> None:
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    for s in (-0.42, 0.42):
        d.line([b, (b[0] - size * math.cos(ang + s), b[1] - size * math.sin(ang + s))],
               fill=color, width=max(3, size // 3))


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0 and dy > 0 else 0


# Tried in order: under the point, then beside it, then above. Sideways before above, because a plate
# directly above a point in the upper half of the frame has nowhere to go and ends up on top of it.
# …and then the same eight again further out, for the case that actually happens: three labels on
# anchors within a few percent of each other (the sac, the coil in it, the catheter tip in that).
_SPOTS = ((0, 1), (1, 1), (-1, 1), (0, -1), (1, -1), (-1, -1), (1, 0), (-1, 0),
          (1, 2), (-1, 2), (0, 2), (1.7, 0.4), (-1.7, 0.4), (1.7, -0.4), (-1.7, -0.4), (0, -2))


def _tag(d, text: str, at: tuple[int, int], f, size: tuple[int, int],
         taken: list | None = None) -> None:
    """A label with a plate behind it, a line joining it to the point it names, and a place to sit.

    Two review findings are built into this. The line is not decoration: a plate floating near a
    structure leaves the reader to guess which thing it means. And the plate has to be *placed* — the
    first version always put it above the point, so three labels whose anchors were within a few
    percent of each other landed on top of one another and on the aneurysm itself, hiding the very
    thing they named.
    """
    w, h = size
    pad, gap = 12, 34
    lines = wrap(text, f, int(w * 0.42), d)[:2]
    tw = int(max(d.textlength(ln, font=f) for ln in lines)) + pad * 2
    th = int(f.size * 1.5) * len(lines) + pad
    taken = taken if taken is not None else []
    best: tuple[tuple[int, int, int, int], int, int] | None = None
    for dx, dy in _SPOTS:
        cx = at[0] + (dx * (tw // 2 + gap) if dx else 0)
        cy = at[1] + (dy * (th // 2 + gap) if dy else 0)
        x = min(max(8, cx - tw // 2), w - tw - 8)
        y = min(max(8, cy - th // 2), h - th - 8)
        rect = (x, y, x + tw, y + th)
        if _overlap(rect, (at[0] - 6, at[1] - 6, at[0] + 6, at[1] + 6)):
            continue                                   # never sit on the point it names
        clash = sum(_overlap(rect, r) for r in taken)
        if best is None or clash < best[2]:
            best = (rect, dy, clash)
        if clash == 0:
            break
    if best is None:                                   # nowhere free: put it under the point anyway
        x = min(max(8, at[0] - tw // 2), w - tw - 8)
        y = min(max(8, at[1] + gap), h - th - 8)
        best = ((x, y, x + tw, y + th), 1, 0)
    (x, y, x2, y2), dy, _clash = best
    taken.append((x, y, x2, y2))
    edge = (x + tw // 2, y if dy > 0 else y2)
    d.line([edge, at], fill=ACCENT, width=max(2, h // 520))
    d.ellipse([at[0] - 5, at[1] - 5, at[0] + 5, at[1] + 5], fill=ACCENT)
    d.rounded_rectangle([x, y, x2, y2], radius=10, fill=(10, 14, 20))
    d.rectangle([x, y, x2, y2], outline=ACCENT, width=2)
    for i, ln in enumerate(lines):
        d.text((x + tw // 2, y + pad // 2 + int(f.size * 0.75) + i * int(f.size * 1.5)), ln, font=f,
               fill=PAPER, anchor="mm")


# --------------------------------------------------------------------- schematics
def _bezier(p0, p1, p2, n: int = 48) -> list[tuple[float, float]]:
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
            for t in (i / (n - 1) for i in range(n))]


def tube(plate, pts, width: int, color=VESSEL) -> None:
    """A vessel: a dark wall, a brighter lumen inside it, and a highlight along one side.

    Three parts rather than one thick stroke, because "the catheter is inside the vessel" is only
    visible if inside and outside look different — a reviewer pointed out that a flat red band makes
    the reader take the device's position on trust.
    """
    from PIL import ImageDraw
    d = ImageDraw.Draw(plate)
    rounded = [(int(x), int(y)) for x, y in pts]
    d.line(rounded, fill=VESSEL_DARK, width=int(width * 1.2), joint="curve")
    d.line(rounded, fill=color, width=int(width * 0.86), joint="curve")
    r = width // 2
    for (x, y) in (rounded[0], rounded[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=VESSEL_DARK)
        d.ellipse([x - int(r * 0.86), y - int(r * 0.86), x + int(r * 0.86), y + int(r * 0.86)],
                  fill=color)
    # One highlight along the upper-left: enough to suggest a wall, cheap to draw.
    off = max(2, width // 4)
    d.line([(x - off // 2, y - off) for x, y in rounded], fill=(240, 168, 164),
           width=max(2, width // 7), joint="curve")


def sac(plate, center: tuple[float, float], rx: float, ry: float) -> None:
    """The aneurysm: a bulge on the wall. Drawn as an ellipse joined to the vessel, which is the shape
    that reads as 'a weak spot that ballooned' — nothing more is claimed."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(plate)
    x, y = center
    d.ellipse([x - rx, y - ry, x + rx, y + ry], fill=SAC, outline=VESSEL_DARK, width=max(3, int(ry / 9)))
    # A darker interior: a sac full of contrast is dark, and it is also the only background on which
    # a coil's loops are visible at all.
    inner = (max(0, x - rx * 0.72), max(0, y - ry * 0.74))
    d.ellipse([inner[0], inner[1], inner[0] + rx * 1.44, inner[1] + ry * 1.3], fill=(146, 46, 48))


def coil(plate, center: tuple[float, float], r: float, loops: float = 3.2) -> None:
    """A coil mass: a bundle of overlapping loops, drawn from the outside in.

    Not a spiral. The first version drew a true spiral of decreasing radius and a reviewer read it as
    a bullseye — which is what a spiral *is* on a flat background. A coil in a sac is a loose ball of
    wire, and a handful of offset loops is both closer to that and obviously not a target.
    """
    from PIL import ImageDraw
    d = ImageDraw.Draw(plate)
    n = max(3, int(round(loops)))
    width = max(3, int(r / 10))
    for i in range(n):
        t = i / max(1, n - 1)
        rr = r * (0.95 - 0.52 * t)
        ox = center[0] + (r * 0.14 if i % 2 else -r * 0.06)
        oy = center[1] + (r * 0.34 * t - r * 0.14)
        d.ellipse([ox - rr, oy - rr * 0.78, ox + rr, oy + rr * 0.78],
                  outline=COIL, width=width)


def catheter(plate, pts, width: int) -> None:
    from PIL import ImageDraw
    d = ImageDraw.Draw(plate)
    rounded = [(int(x), int(y)) for x, y in pts]
    d.line(rounded, fill=(38, 74, 122), width=int(width * 1.3), joint="curve")
    d.line(rounded, fill=CATHETER, width=width, joint="curve")
    tip = rounded[-1]
    d.ellipse([tip[0] - width, tip[1] - width, tip[0] + width, tip[1] + width], fill=(226, 240, 255))


def flow(plate, a: tuple[float, float], b: tuple[float, float], width: int, color=FLOW) -> None:
    from PIL import ImageDraw
    d = ImageDraw.Draw(plate)
    d.line([(int(a[0]), int(a[1])), (int(b[0]), int(b[1]))], fill=color, width=width)
    _head(d, a, b, int(width * 5.5), color)


SCHEMATICS = ("aneurysm", "aneurysm_coiling", "blood_flow")
# What each template shows unless `show` says otherwise. Kept beside the templates rather than inside
# them, so "what does this drawing contain" is answerable without reading the drawing code.
SCHEMATIC_PARTS: dict[str, tuple[str, ...]] = {
    "aneurysm": ("sac",),
    "aneurysm_coiling": ("sac", "catheter", "coil"),
    "blood_flow": ("sac", "flow"),
}


def schematic_geometry(size: tuple[int, int], params: dict | None = None) -> dict:
    """Where everything in a schematic is, both normalised (for callers) and in pixels (for drawing).

    This exists because of a mistake worth not repeating: a label's position was given as a fraction
    of the frame, and the fraction was **guessed** — the label for the aneurysm ended up pointing at
    empty space, and a reviewer said so. A drawing made here knows exactly where its own parts are, so
    nobody should ever have to guess: a caller names the part (`at_part`) and the anchor is looked up.
    The normalised points are also handed back in the tool's answer, which is what makes a plain
    `at` useful when a caller does want to place something by hand.
    """
    p = dict(params or {})
    w, h = size
    mid = h * 0.46
    vw = max(14, int(w * 0.055))
    rr = w * max(0.05, min(float(p.get("sac_r") or 0.115), 0.22))
    sac_x = w * max(0.2, min(float(p.get("sac_at_x") or 0.60), 0.8))
    sac_y = mid - vw * 0.5 - rr * 0.45
    path = _bezier((w * 0.02, mid + vw * 0.5), (w * 0.5, mid - vw * 0.28), (w * 0.98, mid + vw * 0.42))
    neck = min(range(len(path)), key=lambda i: abs(path[i][0] - sac_x))
    tip = (sac_x + rr * 0.04, sac_y + rr * 0.34)      # inside the sac, visible over the coil
    return {
        # normalised, for callers
        "sac": (sac_x / w, sac_y / h),
        "coil": (sac_x / w, (sac_y - rr * 0.1) / h),
        "catheter_tip": (tip[0] / w, tip[1] / h),
        "vessel": (0.5, mid / h),
        "flow_in": (0.18, (mid + vw * 0.44) / h),
        "flow_out": (0.83, (mid + vw * 0.47) / h),
        # in pixels, for drawing
        "_w": w, "_h": h, "_mid": mid, "_vw": vw, "_rr": rr, "_sac": (sac_x, sac_y),
        "_path": path, "_neck": neck, "_tip": tip,
    }


def schematic(kind: str, size: tuple[int, int], params: dict | None = None):
    """A function that draws `kind` into a plate, for `compose(..., draw=...)`.

    The vocabulary is small on purpose. Every shape here is something a viewer can check against the
    caption in one glance, and every shape is drawn from the same parameters every time — which is the
    property a generated picture cannot offer, and the reason a film built this way can be reviewed
    and re-cut without the pictures changing under it.

    Draw order is the anatomy's, not the code's: the bulge first, the vessel over its base so the two
    read as one continuous wall, then whatever instrument is inside.
    """
    kind = (kind or "").strip().lower()
    if kind not in SCHEMATICS:
        raise FigureError(i18n.pick_now(
            f"\"{kind}\" is not a schematic this app can draw. Available: {', '.join(SCHEMATICS)}.",
            f"「{kind}」不是本程序会画的示意图。可用:{', '.join(SCHEMATICS)}。"))
    p = dict(params or {})
    parts = set(SCHEMATIC_PARTS[kind])
    parts |= {str(x).strip().lower() for x in (p.get("show") or [])}
    turns = max(1.0, min(float(p.get("coil_turns") or 3.2), 6.0))
    labels = [x for x in (p.get("labels") or []) if isinstance(x, dict)]
    g = schematic_geometry(size, p)
    w, h, mid, vw, rr = g["_w"], g["_h"], g["_mid"], g["_vw"], g["_rr"]
    sac_x, sac_y = g["_sac"]

    def draw(plate, d):
        if "sac" in parts:
            sac(plate, (sac_x, sac_y), rr, rr * 0.95)
        tube(plate, g["_path"], vw)
        if "coil" in parts:
            coil(plate, (sac_x, sac_y), rr * 0.92, turns)
        if "catheter" in parts:
            # **Inside the lumen, then up through the neck, tip left inside the coil.** The first
            # version of this drawing sent the catheter in from outside the vessel and through its
            # wall, which is not how the device works at all, and stopped its tip at the neck — a
            # reviewer caught both on sight, which is the entire argument for looking at a still
            # before rendering it. Drawing it last also makes the tip sit *in* the coil, which is
            # what shows that the coil is being fed in rather than floating there.
            body = g["_path"][:g["_neck"] + 1]
            tip = g["_tip"]
            lead = _bezier(body[-1],
                           (body[-1][0] + (tip[0] - body[-1][0]) * 0.35, body[-1][1] + vw * 0.6),
                           tip, 26)
            catheter(plate, body + lead, max(5, int(vw * 0.34)))
        if "flow" in parts:
            flow(plate, (w * 0.06, mid + vw * 0.52), (w * 0.30, mid + vw * 0.36), max(4, vw // 5))
            flow(plate, (w * 0.72, mid + vw * 0.42), (w * 0.94, mid + vw * 0.52), max(4, vw // 5))
        f = font(int(h * 0.0165))
        taken: list = []
        for lab in labels:
            anchor = str(lab.get("at_part") or "").strip()
            point = g.get(anchor) if anchor in g and not anchor.startswith("_") else None
            if point is None:
                point = lab.get("at") or [0.5, 0.5]
            _tag(d, str(lab.get("text") or ""), _px(point, size, "label"), f, size, taken)

    return draw


def drawing_note() -> str:
    """The line every schematic frame carries. A drawing that does not say it is a drawing is the
    other way to mislead — and this one is easy to forget, so it is generated, not typed."""
    return i18n.pick_now("(Schematic drawing, not a real image.)",
                         "(示意图,非真实影像。)")


def workspace_output(workspace: Path, subdir: str, name: str, ext: str = ".png") -> Path:
    """A fresh path inside the workspace to draw a frame into.

    The same three guarantees `assemble` and `media` give for their own output, for the same reasons:
    the directory is checked rather than assumed (a member can create `figures` as a symlink and
    `mkdir(exist_ok=True)` would write straight through it), the result is re-checked for containment,
    and a name that is already taken gets a number instead of being overwritten — which also means a
    planted symlink can never be the file that gets opened, since an existing path is never opened.
    """
    from .coderun import inside
    root = Path(workspace).resolve()
    out_dir = Path(workspace) / subdir
    if out_dir.is_symlink():
        raise FigureError(i18n.pick_now(
            f"\"{out_dir}\" is a symlink, so nothing was drawn. Frames are kept in a real folder inside "
            "the workspace.",
            f"「{out_dir}」是符号链接,所以没有画。教学图要放在工作目录里的真实目录中。"))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not inside(root, out_dir):
        raise FigureError(i18n.pick_now(
            f"\"{out_dir}\" resolves outside the workspace, so nothing was drawn.",
            f"「{out_dir}」解析后在工作目录之外,没有画。"))
    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(name or "figure")).strip("-")[:40] or "figure"
    candidate = out_dir / f"{stem}{ext}"
    n = 2
    while candidate.exists():
        candidate = out_dir / f"{stem}-{n}{ext}"
        n += 1
    return candidate
