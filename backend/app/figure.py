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
from functools import lru_cache
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
@lru_cache(maxsize=64)
def font(size: int):
    """The first font this machine really has, at `size`. Falls back to Pillow's own bitmap font so a
    machine with none of the candidates still produces a frame rather than an exception.

    Cached because `ImageFont.truetype` re-reads and re-parses the file every time — a few
    milliseconds each, which nobody notices until an animation asks for it 480 times (four sizes per
    frame) and the whole encode turns out to be waiting on font parsing.
    """
    from PIL import ImageFont
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


# Characters that may not *start* a line. Chinese punctuation carries no width of its own to speak
# of, so a wrap that ignores this leaves a full stop alone on the last line — which a reviewer spotted
# in a rendered film. Pulling it back onto the previous line costs a few pixels of overflow and buys
# text that looks typeset rather than wrapped.
NO_START = "。，、;;::!!??）》」』】”’…—·%℃"


def wrap(text: str, f, limit: int, draw) -> list[str]:
    """Break a line to fit `limit` pixels, character by character, without starting a line on
    punctuation.

    Characters rather than words because Chinese has no spaces: a word-based wrap puts a whole
    sentence on one line and it runs off the frame.
    """
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        cur = ""
        for ch in para:
            if cur and draw.textlength(cur + ch, font=f) <= limit:
                cur += ch
                continue
            # ⚠️ `ch in NO_START` has to be tested **here** — on the character being pushed onto a
            # new line — and not on `not cur`. `cur` is empty only at the *start* of a line, so the
            # old branch could never fire in the case the comment above cites: a line filled to the
            # limit whose next character is a full stop went down the `else` branch and the film kept
            # the lone `。` on a line of its own (measured 2026-09-27, against the same reviewer
            # feedback that comment describes). The fix is the same either way: a few pixels of
            # overflow beats punctuation stranded on its own.
            if ch in NO_START and (cur or lines):
                if cur:
                    cur += ch
                else:
                    lines[-1] += ch     # nothing on this line yet: pull it back to the previous one
                continue
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
            body_fit: str = "full", ruler: bool = False, marks: list | None = None,
            canvas: tuple[int, int, int] | None = None) -> Path:
    """Put a picture, or a drawing, on a titled canvas with a caption and a credit line, and save it.

    Everything about the layout exists for one reason: a picture dropped into a film with no heading
    and no source is a picture nobody can check. The heading says what we are looking at, the caption
    says what to notice, and the credit line says where it came from and under what terms — the answer
    to the question every published film eventually gets asked.

    `draw` is called with `(image, draw)` for a schematic; supplying it means `body_path` is unused.
    `canvas` overrides the background (`INK`) — `visual` passes the film's own canvas so that a framed
    picture and a drawn one do not sit on two slightly different darks.
    """
    plate(size, body_path, heading=heading, caption=caption, credit=credit, draw=draw,
          body_fit=body_fit, ruler=ruler, marks=marks, canvas=canvas).save(out)
    return out


def plate(size: tuple[int, int], body_path: Path | None = None, *,
          heading: str = "", caption: str = "", credit: str = "", draw=None,
          body_fit: str = "full", ruler: bool = False, marks: list | None = None,
          canvas: tuple[int, int, int] | None = None):
    """The same titled canvas, returned as an image instead of written to a file.

    It exists for the animations: every frame of one is this canvas with a different body drawn into
    it, so the heading, the caption and the credit must be produced by exactly the code that produces
    them for a still — otherwise a film's frames and the still a reviewer approved would be two
    slightly different layouts, and the difference would show up as the caption jumping when the shot
    changes from a drawing to an animation.
    """
    from PIL import Image, ImageDraw
    w, h = size
    head_h, foot_h = _band_heights(size, head=bool(heading), foot=(caption or credit))
    # The canvas the frame sits on, and — for a drawing — the field the drawing itself is made on.
    # Both default to what they have always been, so an existing caller is byte-for-byte unchanged;
    # an explicit `canvas` moves both, which is what keeps a film to **one** dark instead of three.
    explicit = tuple(canvas) if canvas else None
    base = explicit or INK
    body_base = explicit or DRAWING_BG
    canvas = Image.new("RGB", size, base)
    d = ImageDraw.Draw(canvas)

    avail_h = h - head_h - foot_h
    if draw is not None:
        body = Image.new("RGB", size, body_base)
        draw(body, ImageDraw.Draw(body))
        canvas.paste(body, (0, 0))
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
        # `base`, not `INK`: the footer is part of the canvas, and filling it with the old constant
        # while the rest of the frame moved is how a film ends up with two darks four units apart —
        # invisible to the eye and visible to the measurement, which is worse.
        d.rectangle([0, y, w, h], fill=base)
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
    return canvas


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
    pad, gap = max(12, int(h * 0.007)), max(34, int(h * 0.028))
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


# The vocabulary. Each name is something that can be **drawn at a time** — and a still is simply that
# drawing at its last moment, which is why there is one list and not two (see `schematic`).
ANIMATIONS: tuple[str, ...] = (
    "blood_flow", "aneurysm_grow", "coil_fill", "catheter_advance", "contrast_fill",
)
SCHEMATICS = ANIMATIONS
# Names that meant a still before these could move. Kept working rather than renamed away: a skill,
# a saved storyboard or a group's habit naming one of them should not break.
SCHEMATIC_ALIASES: dict[str, str] = {
    "aneurysm": "aneurysm_grow",
    "aneurysm_coiling": "coil_fill",
}
# Which parts each drawing contains before `show` adds any.
ANIMATION_PARTS: dict[str, tuple[str, ...]] = {
    "blood_flow": ("sac", "flow"),
    "aneurysm_grow": ("sac",),
    "coil_fill": ("sac", "catheter", "coil"),
    "catheter_advance": ("sac", "catheter", "flow"),
    "contrast_fill": ("sac",),
}
SCHEMATIC_PARTS: dict[str, tuple[str, ...]] = {
    **ANIMATION_PARTS,
    **{old: ANIMATION_PARTS[new] for old, new in SCHEMATIC_ALIASES.items()},
}
PARTS = ("sac", "catheter", "coil", "flow")
CAMERAS = ("none", "push", "pull")
# The camera each drawing uses unless told otherwise: a push where attention should narrow, nothing
# where the motion itself is the point.
ANIMATION_CAMERA: dict[str, str] = {
    "blood_flow": "none",
    "aneurysm_grow": "push",
    "coil_fill": "push",
    "catheter_advance": "push",
    "contrast_fill": "none",
}
STILL_AT = 0.62                 # the frame kept for review: past half way, everything visible
CONTRAST = (236, 240, 248)      # what a vessel looks like when it is full of water-soluble contrast
GHOST = (150, 158, 172)         # the dashed line saying where a wall used to be
# Where each object travelling in the blood sits, and how big it is. Fixed rather than random: the
# same input has to give the same frames, or a note like "move the third one" means nothing.
_JITTER = (0.0, 0.37, 0.71, 0.19, 0.53, 0.88, 0.09, 0.62, 0.44, 0.28, 0.80, 0.05, 0.66, 0.33, 0.95, 0.14)


def canonical(kind: str) -> str:
    """The animation name behind a name. Raises with the whole vocabulary if there is none."""
    k = str(kind or "").strip().lower()
    k = SCHEMATIC_ALIASES.get(k, k)
    if k not in ANIMATIONS:
        raise FigureError(i18n.pick_now(
            f"\"{kind or '(none)'}\" is not a drawing this app can make. Available: "
            f"{', '.join(ANIMATIONS)}"
            + (f" (or their older names: {', '.join(SCHEMATIC_ALIASES)})." if SCHEMATIC_ALIASES else "."),
            f"「{kind or '(没给)'}」不是本程序会画的东西。可用:{', '.join(ANIMATIONS)}"
            + (f"(旧名字 {', '.join(SCHEMATIC_ALIASES)} 也仍然可用)。" if SCHEMATIC_ALIASES else "。")))
    return k


def check_camera(name: str) -> str:
    """The camera move, validated in one place — the tool layer checks a storyboard before anything
    is drawn, and the drawer checks again because it can also be called directly."""
    cam = str(name or "none").strip().lower()
    if cam not in CAMERAS:
        raise FigureError(i18n.pick_now(
            f"Camera \"{cam}\" is not one of: {', '.join(CAMERAS)}.",
            f"镜头运动「{cam}」不在可用之列:{', '.join(CAMERAS)}。"))
    return cam


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


# ------------------------------------------------------- drawing something at a time
# Everything below is a function of `t` in 0..1 rather than a fixed picture, because a still and a
# moving frame have to be the same drawing: a film whose frames were one shape and whose stills were
# another is a film nobody can review. `schematic` is these, sampled at t = 1.
def _ease(t: float) -> float:
    """Smoothstep — the reason a computed motion does not look mechanical."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def sample(pts: list[tuple[float, float]], u: float) -> tuple[float, float]:
    """The point `u` of the way along a polyline (0..1). This is how anything travels here."""
    u = max(0.0, min(1.0, u))
    x = u * (len(pts) - 1)
    i = min(int(x), len(pts) - 2)
    f = x - i
    (x0, y0), (x1, y1) = pts[i], pts[i + 1]
    return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)


def _sub(pts: list[tuple[float, float]], u0: float, u1: float) -> list[tuple[float, float]]:
    """The stretch of a polyline between two fractions — the part already travelled."""
    if u1 <= u0:
        return [pts[0], pts[0]]
    n = len(pts) - 1
    lo, hi = int(u0 * n), min(n, int(u1 * n) + 1)
    return [sample(pts, u0), *pts[lo + 1:hi], sample(pts, u1)]


def blend(a: tuple[int, int, int], b: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    k = max(0.0, min(1.0, k))
    return (int(round(a[0] + (b[0] - a[0]) * k)),
            int(round(a[1] + (b[1] - a[1]) * k)),
            int(round(a[2] + (b[2] - a[2]) * k)))


def _dot(d, p: tuple[float, float], r: float, color) -> None:
    d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def _dashed_ellipse(d, center: tuple[float, float], rx: float, ry: float, color, width: int,
                    dashes: int = 18) -> None:
    """A dashed outline: a claim about where something *was*, which a solid line would not make."""
    step = 360.0 / dashes
    for i in range(0, dashes, 2):
        a0 = i * step
        d.arc([center[0] - rx, center[1] - ry, center[0] + rx, center[1] + ry],
              start=a0, end=a0 + step, fill=color, width=width)


def _camera(kind: str, t: float, focus: tuple[float, float]):
    """A slow push or pull about one point — always *in addition to* something moving by itself. A
    camera move over a still frame is the slide show this is here to replace."""
    if kind == "push":
        k = 1.0 + 0.10 * _ease(t)
    elif kind == "pull":
        k = 1.10 - 0.10 * _ease(t)
    else:
        return lambda p: p
    return lambda p: (focus[0] + (p[0] - focus[0]) * k, focus[1] + (p[1] - focus[1]) * k)


def _sac_at(g: dict, scale: float) -> tuple[tuple[float, float], float]:
    """The sac at a fraction of its full size, with its neck staying on the vessel wall.

    Recomputing the centre from the radius is what makes growth read as ballooning *out of* the wall
    rather than as a circle getting bigger somewhere near it.
    """
    rr = g["_rr"] * scale
    return (g["_sac"][0], g["_mid"] - g["_vw"] * 0.5 - rr * 0.45), rr


def _coil_loops(d, center: tuple[float, float], r: float, turns: float,
                progress: float) -> tuple[float, float] | None:
    """The coil at `progress` 0..1; returns the leading end so the feeding wire can find it.

    Loops rather than a spiral (a spiral of decreasing radius reads as a bullseye — a reviewer said
    so), and the loop going in is an *arc*: that is what makes the fill look like it is happening
    rather than like the last frame was faded in.
    """
    n = max(3, int(round(turns)))
    width = max(3, int(r / 10))
    lead: tuple[float, float] | None = None
    for i in range(n):
        k = i / max(1, n - 1)
        rr = r * (0.95 - 0.52 * k)
        ox = center[0] + (r * 0.14 if i % 2 else -r * 0.06)
        oy = center[1] + (r * 0.34 * k - r * 0.14)
        box = [ox - rr, oy - rr * 0.78, ox + rr, oy + rr * 0.78]
        if progress >= (i + 1) / n:
            d.ellipse(box, outline=COIL, width=width)
            lead = (ox + rr, oy)
        elif progress > i / n:
            a1 = -180 + 360 * ((progress - i / n) * n)
            d.arc(box, start=-180, end=a1, fill=COIL, width=width)
            ang = math.radians(a1)
            return (ox + rr * math.cos(ang), oy + rr * 0.78 * math.sin(ang))
        else:
            return lead
    return lead


def _sac_opaque(plate, center: tuple[float, float], rr: float, k: float) -> None:
    """The sac as it opacifies: the same ellipse, with its interior going from dark to contrast."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(plate)
    x, y = center
    d.ellipse([x - rr, y - rr, x + rr, y + rr * 0.95], fill=SAC,
              outline=VESSEL_DARK, width=max(3, int(rr / 9)))
    inner = (max(0, x - rr * 0.72), max(0, y - rr * 0.74))
    d.ellipse([inner[0], inner[1], inner[0] + rr * 1.44, inner[1] + rr * 1.3],
              fill=blend((146, 46, 48), CONTRAST, k))


def drawer(kind: str, size: tuple[int, int], params: dict | None = None,
           labels: list[dict] | None = None):
    """`t` in, a `draw(plate, draw)` out — for `plate(..., draw=...)` or for a whole animation.

    The geometry is worked out once here rather than inside the per-frame closure: at 24fps a
    twenty-second shot asks for this 480 times, and recomputing the vessel path each time would give
    the same answer for a whole second of encoding.

    A returned function is cheap to call repeatedly but shares `labels`, which is why the label list
    is copied at the call site (`animate.render`) rather than mutated here.
    """
    kind = canonical(kind)
    p = dict(params or {})
    labs = [x for x in (labels if labels is not None else p.get("labels") or []) if isinstance(x, dict)]
    g = schematic_geometry(size, p)
    parts = set(ANIMATION_PARTS[kind]) | {str(x).strip().lower() for x in (p.get("show") or [])}
    turns = max(2.0, min(float(p.get("coil_turns") or 3.2), 6.0))
    particles = max(2, min(int(p.get("particles") or 9), 16))
    camera = check_camera(p.get("camera") or ANIMATION_CAMERA[kind])
    w, h, mid, vw = g["_w"], g["_h"], g["_mid"], g["_vw"]
    path, neck, tip = g["_path"], g["_neck"], g["_tip"]

    def at(t: float):
        t = max(0.0, min(1.0, float(t)))
        cam = _camera(camera, t, g["_sac"])
        e = _ease(t)

        def draw(plate, d):
            scale = 1.0
            if kind == "aneurysm_grow":
                # A blister that becomes the sac, with a settle at the end so it lands rather than
                # simply stopping.
                scale = 0.34 + 0.66 * e
                if t > 0.55:
                    scale *= 1.0 + 0.03 * math.sin(2 * math.pi * 3.0 * t)
            elif kind == "blood_flow":
                scale = 1.0 + 0.025 * math.sin(2 * math.pi * 1.6 * t)    # a pulse travelling through

            # ---- the drawing order is the anatomy's, not the code's
            if "sac" in parts:
                center, rr = _sac_at(g, scale)
                if kind == "aneurysm_grow" and scale < 0.985:
                    _dashed_ellipse(d, cam(center), g["_rr"], g["_rr"] * 0.95, GHOST,
                                    max(2, int(vw / 9)))
                if kind == "contrast_fill":
                    _sac_opaque(plate, cam(center), rr, _ease(max(0.0, min(1.0, (t - 0.45) / 0.35))))
                else:
                    sac(plate, cam(center), rr, rr * 0.95)
            tube(plate, [cam(q) for q in path], vw)

            if kind == "contrast_fill":
                # A bolus running up the lumen: the bright column behind the leading dot. A finding
                # being made, not a decoration.
                u = _ease(min(1.0, t / 0.75))
                if u > 0:
                    tube(plate, [cam(q) for q in _sub(path, max(0.0, u - 0.30), u)],
                         max(6, int(vw * 0.62)), CONTRAST)
                    _dot(d, cam(sample(path, u)), vw * 0.42, CONTRAST)

            if "coil" in parts:
                center, rr = _sac_at(g, 1.0)
                lead = _coil_loops(d, cam(center), rr * 0.92, turns, _ease(min(1.0, t / 0.85)))
                if lead is not None and t < 0.97:
                    # The wire between the catheter tip and the leading loop: the coil is being
                    # pushed in, not appearing.
                    d.line([cam(tip), lead], fill=COIL, width=max(2, int(vw / 8)))

            if "catheter" in parts:
                body = path[:neck + 1]
                ctrl = (body[-1][0] + (tip[0] - body[-1][0]) * 0.35, body[-1][1] + vw * 0.6)
                wide = max(5, int(vw * 0.34))
                if kind == "catheter_advance":
                    q = _ease(min(1.0, t / 0.9))
                    if q < 0.72:
                        seg = _sub(body, 0.0, q / 0.72)
                        catheter(plate, [cam(x) for x in seg], wide)
                        _dot(d, cam(seg[-1]), vw * 0.30, (226, 240, 255))
                    else:
                        lead_c = _bezier(body[-1], ctrl, tip, 26)
                        k = (q - 0.72) / 0.28
                        catheter(plate, [cam(x) for x in body + lead_c[:max(2, int(len(lead_c) * k))]],
                                 wide)
                else:
                    # **Inside the lumen, then up through the neck, tip left in the sac.** The first
                    # version sent the catheter in from outside the vessel and through its wall, which
                    # is not how the device works, and stopped its tip at the neck — a reviewer caught
                    # both on sight, which is the whole argument for looking at a frame first.
                    lead_c = _bezier(body[-1], ctrl, tip, 26)
                    catheter(plate, [cam(x) for x in body + lead_c], wide)

            if "flow" in parts:
                # Three things travel together, and all three are needed: a band of lighter blood
                # (what makes the motion visible at all on a phone), a bright streak behind the
                # leading edge, and the dot itself. The first version drew only the dots and a
                # reviewer's frame-difference check came back at 0.3% of the picture — which is
                # motion nobody would see. The two arrows stay put: they say which way is downstream
                # in a single frame, which a moving dot cannot.
                speed = 1.5 if kind == "blood_flow" else 0.7
                for i in range(particles):
                    # Evenly spaced dots are the trap here: at these speeds the pattern comes back to
                    # almost exactly where it started within a fraction of a second, so the stream
                    # looks like it barely moves. Fixed but irregular offsets and sizes (the same ones
                    # every run, so the film is still reproducible) make it read as blood.
                    u = (_JITTER[i % len(_JITTER)] + t * speed) % 1.0
                    tail = (0.05 if kind == "blood_flow" else 0.035) * (0.7 + 0.6 * _JITTER[(i * 5) % len(_JITTER)])
                    wide = vw * (0.20 + 0.12 * _JITTER[(i * 3) % len(_JITTER)])
                    band = [cam(q) for q in _sub(path, max(0.0, u - tail * 1.9), u)]
                    d.line(band, fill=blend(VESSEL, FLOW, 0.45), width=max(4, int(vw * 0.46)),
                           joint="curve")
                    d.line([cam(sample(path, max(0.0, u - tail))), cam(sample(path, u))],
                           fill=FLOW, width=max(3, int(vw * 0.24)))
                    _dot(d, cam(sample(path, u)), wide, FLOW)
                flow(plate, cam((w * 0.06, mid + vw * 0.52)), cam((w * 0.30, mid + vw * 0.36)),
                     max(4, vw // 5))
                flow(plate, cam((w * 0.72, mid + vw * 0.42)), cam((w * 0.94, mid + vw * 0.52)),
                     max(4, vw // 5))

            # Labels are *not* scaled with the camera — they have to stay readable — and since each
            # one points at a part, it can appear when that part does (`from`) and leave when it is
            # done (`to`). That is how a drawing gets narrated without the narration racing it.
            f = font(int(h * 0.0165))
            # The sac is a keep-out area for every label. A plate may not touch it: a label that sits
            # on the aneurysm hides the thing it is naming, and one that touches the neck hides where
            # the coil is going in. (A reviewer caught exactly that once the coil shot was rendered at
            # full size — at 270 pixels wide the collision was invisible.) Seeded here because this is
            # the only layer that knows where the anatomy is; `_tag` knows about plates.
            sc, scr = _sac_at(g, scale)
            taken: list = [(int(sc[0] - scr * 1.08), int(sc[1] - scr * 1.08),
                            int(sc[0] + scr * 1.08), int(sc[1] + scr * 1.08))]
            for lab in labs:
                if not (float(lab.get("from") or 0.0) <= t <= float(lab.get("to") or 1.0)):
                    continue
                anchor = str(lab.get("at_part") or "").strip()
                point = None
                if anchor == "sac":
                    sp = _sac_at(g, scale)[0]
                    point = (sp[0] / w, sp[1] / h)
                elif anchor in g and not anchor.startswith("_"):
                    point = g[anchor]
                if point is None:
                    point = lab.get("at") or [0.5, 0.5]
                _tag(d, str(lab.get("text") or ""), _px(point, size, "label"), f, size, taken)

        return draw

    return at


def schematic(kind: str, size: tuple[int, int], params: dict | None = None):
    """The same drawing, at its end — which is what a still is.

    One vocabulary instead of two: "the coil is in place" and "the coil being wound in" are the same
    picture at two times, so asking which one the caller wanted told us nothing. `aneurysm` and
    `aneurysm_coiling` still work as older names for the same two drawings.

    Draw order is the anatomy's, not the code's: the bulge first, the vessel over its base so the two
    read as one continuous wall, then whatever instrument is inside.
    """
    p = dict(params or {})
    p["camera"] = "none"        # a still is never pushed in: the frame is the frame
    return drawer(kind, size, p)(1.0)


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
