"""What a picture has to be before it may go into a film, and what happens to it when it is not.

This module exists because of one measured failure. A 300-second science film was assembled from
30 pictures and the verdict was 「图不专业」. Measuring them explained why, and none of the reasons
had anything to do with drawing skill:

  * **12 of 30 were not the film's shape at all.** The worst was 2210x584 — an aspect of 3.784
    against the frame's 0.5625, off by a factor of nearly seven. A sideways web picture or a chat
    screenshot cannot be put into a vertical frame; it can only be cropped, stretched or padded.
  * **8 different background colours.** `#081828` on one, `#F8F8F8` on the next, `#D8D8D8` after it.
  * **A 14x spread in how many colours a picture uses** (121 to 1706) — flat line art next to a
    rendered three-dimensional illustration, in one film, one after another.

Meanwhile the 15 pictures this app drew itself were **identical to each other**: same background,
colour spread within 7/255, edge density within one. So the drawing code was never the problem.
The problem was what got admitted, and that nothing owned the answer.

Three things follow, and they are the three parts of this module.

**A spec.** One place that says what the frame is, what colour sits behind a picture that does not
fill it, how far a picture may be from the frame's shape before it stops being a picture of that
frame, and how small is too small. It lives here rather than in `layouts` (which is how a *film*
looks: captions, pace, cards) or in `figure` (which is how a *drawing* is made), because neither
answers "may this go in the frame, and how" — and that question is the one that was unanswered.

**The house style, measured off the material — `profile()` and `redraw()`.** What a group hands over
*is* the brief for how the film looks, and it is the only brief that cannot be argued with. So the
style is not chosen here: `profile()` reads a set of pictures and reports what they have in common —
the background they sit on, the colours they are drawn in, how heavy the lines are, and how much of
the set actually agrees. `redraw()` then draws a picture again in those terms: its own background is
dropped, its structure is kept, and its colours are replaced by the film's ramp. That is what "unify
the material" means, and it is deliberately **not** applied to photographs.

**A gate — `admit()`.** Given a picture and a frame, it returns the one way that picture may be
placed, and why: `cover` when it is already the frame's shape, `redraw` when it is a drawing that can
be drawn again in the film's palette, `plate` when it is a real picture — framed whole on the film's
own background with a heading and a source line, never cropped, never stretched, and never re-drawn,
because a re-drawn angiogram is an invented angiogram. That split is not a style preference; it is
the difference between restyling a diagram and fabricating evidence.

Why a gate rather than a note in the prompt: `layouts` already documented the same lesson — *"a
layout that is quietly not loaded assembles with the default look while the caller believes it got
the clinic one"*. A rule that only exists in prose is a rule a model can forget on shot 9 of 30. So
the default path enforces it, and naming a `fit` explicitly still overrides — the gate decides what
happens when nobody decided.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import figure
from . import i18n

__all__ = ["SPEC", "ASPECT_TOLERANCE", "MIN_SHORT_EDGE", "SOFT_SHORT_EDGE", "FITS", "ORDER",
           "PROFILE_BUDGET", "SAME_COLOUR", "FLAT_COVERAGE", "FILM_CANVAS",
           "PALETTE_FLOOR", "PALETTE_GRID", "PALETTE_MAX",
           "LIGHT_FIELD", "FIELD_SHARE", "NEUTRAL_CHROMA", "TONE_LO_GAP", "TONE_MIN_RANGE",
           "FIELD_RING", "FIELD_INNER", "FLIP_DROP",
           "audit", "admit", "plate_write", "tone_of", "tone_write", "on_a_page", "profile",
           "redraw",
           "save_profile", "rules", "report", "PLATE_LABEL"]

# --------------------------------------------------------------------- the spec
# How far a picture's shape may be from the frame's before it stops counting as "made for this
# frame". Expressed as a share of the frame's own aspect, so it means the same thing at 9:16 and at
# 16:9. 4% is the measured slack of ordinary re-exports: 1080x1920 (0.56250) against 1080x1856
# (0.58190) is 3.5% and is the same shot; 1920x1080 (1.7778) against 1080x1920 (0.56250) is 216%
# and is not. Anything between those two is a decision, not a rounding error, and `admit` refuses to
# guess: see `admit()` for what it does instead.
ASPECT_TOLERANCE = 0.04

# A picture is shown inside the frame with the plate's own margins, so it is upscaled to about the
# frame's short edge. Under this many pixels that upscaling is visible on a phone; under the soft
# floor it is visible on a monitor. Both are warnings rather than refusals — a small picture that
# says something nobody else has is still the right picture to use, said out loud.
MIN_SHORT_EDGE = 540
SOFT_SHORT_EDGE = 1000

# How a picture may be put into the frame. `cover`/`contain`/`blur` are `assemble`'s and keep their
# meanings. `redraw` and `plate` are this module's, and they are two answers to the same problem —
# "this picture was not made for this film":
#
#   * `redraw` — **the picture is drawn again in the film's own style.** Its structure is kept (the
#     pixels that are not background, quantised to the film's palette) and its own background is
#     replaced by the film's. This is what "unify the material" means for a flat picture: a chart, a
#     diagram, a screenshot of one. Nothing about the shape of the data changes; only the look does.
#   * `plate` — **the picture keeps its pixels and is framed.** Used for anything photographic, and
#     for anything this app did not draw, because re-drawing a real angiogram would mean inventing
#     the anatomy it is evidence of — the one thing `figure`'s own docstring rules out. Framed on
#     the film's background with a heading and a source line, it reads as a reference being shown.
#
# A picture already shaped like the frame fills it (`cover`) and is never redrawn: a 9:16 picture
# made for a 9:16 film is already in the film's world.
FITS = ("redraw", "plate", "cover", "contain", "blur")

# The order the gate considers, when the caller named none.
ORDER = ("cover", "redraw", "plate")

# A plate with no heading still needs *something* in the band, because the band is what makes the
# picture read as framed rather than as a picture that failed to fill the screen. This is the honest
# minimum: it claims nothing about what is in the picture.
PLATE_LABEL = ("Reference image", "参考图")

# The one colour behind everything the film places: a framed photograph, a re-drawn chart, and the
# app's own schematics and animations alike. It is `DRAWING_BG` — the field the drawing code already
# uses — and **not** `INK`, the plate's own darker value, because picking `INK` meant a re-draw
# repainted 73% of every schematic the app had already drawn correctly. Two darks four units apart
# is a difference nobody can see and a fact the measurement still reports, which is exactly the kind
# of thing that turns into "why does it say two backgrounds".
FILM_CANVAS = tuple(figure.DRAWING_BG)



def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % tuple(int(v) for v in rgb)


# --------------------------------------------------------------------- measuring a picture
def audit(path: "str | Path", *, sample: int = 256) -> dict:
    """Measure one picture: the numbers the verdict is made of, and nothing about how it looks.

    A model that cannot see can still act on these, and a person can check them. `sample` bounds the
    work — a 2210x584 screenshot and a 4000x6000 photograph are reduced to the same thumbnail before
    anything is counted, so the cost of measuring a folder is the cost of reading it.
    """
    from PIL import Image

    p = Path(path)
    with Image.open(p) as im:
        im = im.convert("RGB")
        width, height = im.size
        background, edge = _border_canvas(im)
        small = im.copy()
        small.thumbnail((sample, sample), Image.LANCZOS)
    pixels = list(small.getdata())
    counts: dict[tuple[int, int, int], int] = {}
    for px in pixels:
        counts[px] = counts.get(px, 0) + 1
    top = sorted(counts.values(), reverse=True)
    coverage = sum(top[:8]) / max(1, len(pixels))
    n = max(1, len(pixels))
    field_lum = _lum(background)
    return {
        "path": str(p),
        "width": width,
        "height": height,
        "aspect": (width / height) if height else 0.0,
        "short_edge": min(width, height),
        # How many distinct colours survive the thumbnail. Not a quality score on its own — a
        # schematic is *supposed* to be flat — but a film that mixes 121-colour line art with
        # 1706-colour rendering is a film with two visual languages, and this is what shows it.
        "colours": len(counts),
        # The share of the picture carried by its eight commonest colours, which is what separates a
        # drawing from a photograph: flat fills concentrate, continuous tone does not. This is the
        # number `flat` is decided by, and the thresholds were measured rather than chosen (see
        # FLAT_COVERAGE).
        "coverage": round(coverage, 4),
        "background": _hex(background),
        # How far the four corners are from each other: a picture whose own background is uniform
        # reads 0, and a picture with a shadow or a gradient behind it reads high and will show a
        # visible edge wherever it is placed. Measured on the real corner pixels, for the same reason
        # the background is.
        "corner_delta": max(max(c) - min(c) for c in
                            zip(edge[0], edge[1], edge[-2], edge[-1])),
        # Whether this is a **drawing** or a **photograph**, which is the question that decides
        # re-drawing against framing — not a taste judgement, one count off the same thumbnail.
        "flat": coverage >= FLAT_COVERAGE,
        # ---- the tone: what this picture treats as black, and whether it is carrying a page.
        # `field_lum` is the luminance of its own canvas. Above `LIGHT_FIELD` with a big enough
        # `field_share` it is a page rather than a photograph's own dark, and a page cannot stay:
        # nine stills of a real film had one, which is what "not the same tone" turns out to mean
        # when it is counted.
        "field_lum": round(field_lum, 1),
        "field_share": round(sum(1 for c in pixels
                                 if max(abs(a - b) for a, b in zip(c, background)) <= SAME_COLOUR) / n, 4),
        # How far the average pixel is from neutral. A projection or a print is near 0; anything with
        # colour in it is not, and the difference decides whether flipping the tone is lossless.
        "chroma": round(sum(max(c) - min(c) for c in pixels) / n, 1),
        # A page is flat at the edges and something else in the middle; a photograph is the same kind
        # of thing all over. These two shares are how `FIELD_RING`/`FIELD_INNER` tell them apart, and
        # they are the numbers that stand between "remove the paper" and "delete the subject".
        **_field_shape(small, background),
    }


def _field_shape(im, field: tuple) -> dict:
    """How much of a picture's **border** is its field, and how much of its **middle** is.

    `im` is already the bounded thumbnail, so this costs nothing extra. The bands are 1/12 of the
    picture on each side, which is wide enough to survive a one-pixel crop line and narrow enough to
    be about the border rather than about the picture.
    """
    w, h = im.size
    bw, bh = max(1, w // 12), max(1, h // 12)
    bands = [im.crop((0, 0, w, bh)), im.crop((0, h - bh, w, h)),
             im.crop((0, 0, bw, h)), im.crop((w - bw, 0, w, h))]
    inner = im.crop((w // 6, h // 6, w * 5 // 6, h * 5 // 6))

    def share(zone) -> float:
        px = list(zone.getdata())
        if not px:
            return 0.0
        return sum(1 for c in px if max(abs(a - b) for a, b in zip(c, field)) <= SAME_COLOUR) / len(px)

    return {"field_ring": round(min(share(b) for b in bands), 4), "field_inner": round(share(inner), 4)}


def _against(info: dict, size: tuple[int, int]) -> tuple[float, float]:
    """The picture's aspect against the frame's, and their relative distance."""
    fw, fh = size
    frame = (fw / fh) if fh else 0.0
    if not frame or not info.get("aspect"):
        return frame, 1.0
    return frame, abs(info["aspect"] / frame - 1.0)


# --------------------------------------------------------------------- the gate
def _on_film_canvas(info: dict) -> bool:
    """Whether a picture already sits on the film's own background.

    This is the test that stops the engine damaging work that is already right, and it exists because
    it did exactly that: measured, a re-draw of a schematic this app had drawn itself came out 78.7%
    identical — the other 21% being its heading band and its darker field, both judged "background"
    and both repainted. A picture already on the film's canvas is already in the film's world; there
    is nothing to unify, and "improving" it is the expensive kind of quiet.
    """
    return max(abs(a - b) for a, b in zip(_unhex(info["background"]), FILM_CANVAS)) <= SAME_COLOUR


def admit(path: "str | Path", size: tuple[int, int], *, fit: str = "",
          credit: str = "") -> dict:
    """The one way this picture may be placed in a frame of `size`, and the reason why.

    Returns `{"fit", "reason", "warnings", "info", "repaint"}`. `fit` is a member of `FITS`;
    `repaint` is False when the picture may be placed but must **not** be drawn again.

    The rules, in order:

      1. **A `fit` the caller named is honoured**, whatever it is — this is the same principle
         `layouts` follows: naming a thing is a decision, and second-guessing a decision is the
         silent substitution this project treats as the expensive mistake. Warnings still come back
         so the cost is visible.
      2. **A picture already on the film's own background is left alone.** If it is also the frame's
         shape it fills the frame untouched; otherwise it is placed whole on the film's canvas and
         nothing is repainted. This is not a shortcut, it is the point: the app's own schematics and
         animations are drawn on that canvas already, and repainting 21% of a picture the user
         approved is damage, not unification.
      3. **A drawing from somewhere else is drawn again** in the film's palette (`redraw`) — a chart
         off a white page, a diagram out of a slide. Shape is not the test: *unification* is, and a
         set of drawings each carrying their own background is the "two films stapled together"
         problem even when every one of them happens to be 9:16.
      4. **A real picture already shaped like the frame fills it** (`cover`).
      5. **Any other real picture is framed** (`plate`) — not cropped, not stretched, and **not
         re-drawn**: a re-drawn angiogram is an invented angiogram, and the whole point of using a
         real one is that somebody real produced it.

    A missing `credit` is a warning and not a refusal: a film with an uncredited picture is a film
    nobody may publish, and that belongs in the film's notes where a person will see it, not in an
    exception that stops the render.
    """
    info = audit(path)
    frame, delta = _against(info, size)
    want = (fit or "").strip().lower()
    warnings: list[str] = []
    fits_frame = delta <= ASPECT_TOLERANCE
    at_home = _on_film_canvas(info)
    tone = tone_of(info)
    verdict = {"repaint": True, "tone": tone}

    if tone:
        warnings.append(i18n.pick_now(
            f"its own field is {'light' if info['field_lum'] >= LIGHT_FIELD else 'dark'} "
            f"({info['background']}, {info['field_share']:.0%} of it) so it is brought into the film's "
            f"tone before it is placed — "
            + ("its grey scale is reversed, the way a viewer shows a projection"
               if tone == "flip" else
               "the page goes and the content stays, nothing else about it is changed"),
            f"它自己的底是{'亮' if info['field_lum'] >= LIGHT_FIELD else '暗'}的"
            f"({info['background']},占 {info['field_share']:.0%}),所以放置前先归到本片色调——"
            + ("反相处理,和阅片显示一致" if tone == "flip" else "去掉那张纸底,内容保持不变")))

    if info["short_edge"] and info["short_edge"] < MIN_SHORT_EDGE:
        warnings.append(i18n.pick_now(
            f"it is only {info['width']}x{info['height']}; at this frame it is scaled up "
            f"{max(1.0, size[0] / max(1, info['width'])):.1f}x and will look soft",
            f"它只有 {info['width']}x{info['height']};放进这个画幅要放大 "
            f"{max(1.0, size[0] / max(1, info['width'])):.1f} 倍,会发软"))
    elif info["short_edge"] and info["short_edge"] < SOFT_SHORT_EDGE:
        warnings.append(i18n.pick_now(
            f"{info['short_edge']}px on the short edge — under {SOFT_SHORT_EDGE}px it is visibly "
            "upscaled on a monitor",
            f"短边 {info['short_edge']} 像素——低于 {SOFT_SHORT_EDGE} 像素时在显示器上能看出放大"))
    if info["corner_delta"] > 40:
        warnings.append(i18n.pick_now(
            f"its own background is uneven ({info['corner_delta']}/255 across the corners), so it "
            "keeps a visible edge wherever it is placed",
            f"它自己的底色不均匀(四角差 {info['corner_delta']}/255),放在哪里都会看出一道边"))
    if not (credit or "").strip():
        warnings.append(i18n.pick_now(
            "it has no credit line, so the film cannot say where this picture came from",
            "它没有来源说明,成片没法回答这张图从哪来"))

    if want in FITS:
        if want in ("cover", "blur") and not fits_frame:
            warnings.append(i18n.pick_now(
                f"asked for `{want}` on a {info['width']}x{info['height']} picture in a "
                f"{size[0]}x{size[1]} frame: that crops or stretches "
                f"{delta * 100:.0f}% of its shape away",
                f"在 {size[0]}x{size[1]} 画幅里对一张 {info['width']}x{info['height']} 的图用 "
                f"「{want}」:会裁掉或拉伸掉它 {delta * 100:.0f}% 的形状"))
        if want == "redraw" and not info["flat"]:
            warnings.append(i18n.pick_now(
                f"asked to re-draw a photograph ({info['colours']} colours): re-drawing a real "
                "picture makes a picture that claims to be real and is not — `plate` keeps it whole",
                f"要求重画一张照片({info['colours']} 种颜色):重画真实图片会造出「看着像真的、"
                "其实不是」的东西——用 `plate` 原样保留"))
        return {"fit": want, "reason": i18n.pick_now(
            "the caller asked for it", "调用方指定的"), "warnings": warnings, "info": info,
            **verdict}

    if at_home:
        if fits_frame:
            return {"fit": "cover", "reason": i18n.pick_now(
                f"it is already on the film's background ({info['background']}) and already the "
                f"frame's shape — it fills the frame and nothing is touched",
                f"它本来就在片子的底色上({info['background']}),而且本来就是画幅形状——直接填满,"
                "什么都不动"), "warnings": warnings, "info": info, "repaint": False, "tone": ""}
        return {"fit": "redraw", "reason": i18n.pick_now(
            f"it is already on the film's background ({info['background']}), so it is placed whole "
            "and **nothing is repainted**",
            f"它本来就在片子的底色上({info['background']}),所以整张放进去,**什么都不重画**"),
            "warnings": warnings, "info": info, "repaint": False, "tone": ""}

    if fits_frame and not info["flat"]:
        return {"fit": "cover", "reason": i18n.pick_now(
            f"it is a real picture ({info['coverage']:.0%} in its eight commonest colours) whose "
            f"shape ({info['aspect']:.4f}) is the frame's ({frame:.4f})",
            f"它是一张真实图片({info['coverage']:.0%} 集中在最常见的八种颜色里),"
            f"形状({info['aspect']:.4f})就是画幅的形状({frame:.4f})"),
            "warnings": warnings, "info": info, **verdict}

    if info["flat"]:
        return {"fit": "redraw", "reason": i18n.pick_now(
            f"it is a drawing ({info['coverage']:.0%} of it is in its eight commonest colours) on a "
            f"background of its own ({info['background']}), so it is drawn again in the film's "
            "palette — same structure, one palette",
            f"它是一张画({info['coverage']:.0%} 集中在它自己最常见的八种颜色里),但带着自己的底色"
            f"({info['background']}),所以用片子的调色板重画一遍——结构不变,调色板统一"),
            "warnings": warnings, "info": info, **verdict}

    return {"fit": "plate", "reason": i18n.pick_now(
        f"it is a real picture ({info['coverage']:.0%} in its eight commonest colours) whose shape "
        f"({info['aspect']:.4f}) is {delta * 100:.0f}% away from the frame's ({frame:.4f}), so it is "
        "framed whole rather than cropped, stretched or re-drawn",
        f"它是一张真实图片({info['coverage']:.0%} 集中在最常见的八种颜色里),形状"
        f"({info['aspect']:.4f})与画幅({frame:.4f})差 {delta * 100:.0f}%,所以整张装进画框——"
        "不裁、不拉伸、也不重画"), "warnings": warnings, "info": info, **verdict}


# --------------------------------------------------------------------- the tone
def on_a_page(info: dict) -> dict:
    """The evidence behind the `page` call, for a person to check: how much of the **border** is the
    field against how much of the **middle** is.

    A page is flat at the edges and something else in the middle (border high, middle low); a bright
    photograph is the same kind of thing all over (both middling). It is reported rather than gated —
    see `FIELD_RING` for the measurement that ruled the test out as a gate.
    """
    return {"border": info.get("field_ring", 0.0), "middle": info.get("field_inner", 0.0),
            "share": info.get("field_share", 0.0)}


def tone_of(info: dict) -> str:
    """What has to happen to this picture's **tone** before it can sit in this film, and nothing else.

    Returns `""` (already the film's tone), `"flip"` (a neutral picture on a light field — the grey
    scale is reversed) or `"page"` (a page or a lightbox is taken out from under the content).

    This is deliberately separate from `admit()`'s verdict: `admit()` decides **how the picture is
    placed** (fills the frame, drawn again, framed whole), and this decides **whether its light and
    dark belong to the film**. Those are two different questions, and the film this was written
    against got the first one right and the second one wrong — nine of its thirty stills were white
    pages, and every one of them had been placed correctly.
    """
    if _on_film_canvas(info):
        return ""
    if info.get("field_lum", 0) < LIGHT_FIELD or info.get("field_share", 0) < FIELD_SHARE:
        return ""
    return "flip" if info.get("chroma", 999) < NEUTRAL_CHROMA else "page"


def _ends_lut(content, *, film_lum: float, paper_lum: float, mask=None):
    """A per-channel table that moves a picture's **black** onto the film's own black.

    Only the black end, and only when it is lifted. A picture with a true black already has the
    film's black, and stretching it would be a grade nobody asked for — so this returns nothing for
    it and the picture passes through. The **white** end is deliberately not moved: a photograph's
    white is the photograph's, and pulling 255 down to the film's paper would make it dimmer without
    making the film any more coherent. (Measured on the two photographs this rule left bright: their
    interiors are 51% bright, which is what the picture *is*, not a page hanging in the frame.)

    Hue is kept by measuring one luminance scale and applying it to all three channels, so a graded
    angiogram is still the same angiogram seen in the film's light.
    """
    ends = _percentiles(content, mask)
    if ends is None:
        return None, None
    lo, hi = ends
    if lo < film_lum + TONE_LO_GAP:
        return None, None                      # it has a black of its own: leave it alone
    if hi - lo < TONE_MIN_RANGE:
        return None, None                      # a bright surface, not a picture with lifted shadows
    span = max(1.0, hi - lo)
    scale = min(1.6, max(1.0, paper_lum - film_lum) / span)      # bounded: never a violent stretch
    lut = [max(0, min(255, round(film_lum + (v - lo) * scale))) for v in range(256)]
    return lut, (lo, hi)


def _percentiles(im, mask=None, *, lo_pct: float = 0.01, hi_pct: float = 0.99):
    """The picture's black and white ends, measured on the content rather than the whole rectangle.

    Restricted to `mask` when there is one, because a matted page is not the picture and its white
    would otherwise be read as the picture's white — which is how a page becomes "the picture is too
    bright" after it has already been removed.
    """
    from PIL import Image

    gray = im.convert("L")
    if mask is not None:
        small = Image.new("L", gray.size)
        small.paste(gray, (0, 0), mask)
        vals = sorted(v for v in small.getdata() if v)
    else:
        small = gray.copy()
        small.thumbnail((200, 200), Image.LANCZOS)
        vals = sorted(small.getdata())
    if not vals:
        return None
    n = len(vals)
    return vals[int(n * lo_pct)], vals[min(n - 1, int(n * hi_pct))]


def tone_write(src: "str | Path", out: "str | Path", size: tuple[int, int], *,
               margin: float = 0.04) -> dict:
    """Put a real picture into the film's tone, whole and uncropped, and say what was done.

    Returns `{"out", "applied", "info", "ends"}`. `applied` is a list of short tags — `"flip"`,
    `"page removed"`, `"ends matched"` — so a film's notes can state the change instead of implying
    that nothing happened to somebody's picture.

    Five rules, and each exists because of something measured:

      1. **A picture already on the film's canvas is not touched at all.** (The same rule `admit()`
         follows, and for the same reason: it is already right.)
      2. **A neutral picture on a light field is flipped**, value only, in HSV. This is the display
         convention rather than a taste: DICOM stores such a projection as `MONOCHROME1` — "minimum
         sample value displayed as white" — and viewers reverse it so anatomy reads bright on a dark
         field. It is lossless, and it is the only operation here that touches the content's own
         pixels at all. Only when there is something dark to flip (`FLIP_DROP`): a greyscale
         photograph of a brightly lit wall is not a projection, and turning it black would be a
         change nobody asked for.
      3. **A page is taken out from under the content.** A colour diagram or screenshot on a white
         page keeps its pixels byte for byte and loses the paper. This is matting, not re-drawing:
         the page carries no information, which is exactly why it may go. A projection that was just
         flipped gets its field removed too, because the field is dark by then but not the film's own
         dark — and two darks four units apart is the fault this whole module is here to remove.
      4. **The two ends are matched** to the film's black and its paper, and only when the picture's
         black is visibly lifted — so a picture that already has a true black is left alone.
      5. **A surface is left completely alone** (`TONE_MIN_RANGE`): a picture whose whole range is
         narrow has nothing to flip, nothing to keep and nothing to grade, and all three of the wrong
         things this module did first were done to one. It is placed as it is, brightness intact.
      6. **Nothing is ever cropped**, and what goes is only ever a colour the picture's own border is
         made of, matched within 24/255 — a page, a lightbox surround, a scan margin.
    """
    from PIL import Image, ImageChops, ImageOps

    p = Path(src)
    with Image.open(p) as im:
        img = ImageOps.exif_transpose(im).convert("RGB")
    info = audit(p)
    applied: list[str] = []
    mask = None

    if _on_film_canvas(info):
        # Rule 1. `admit()` does not send such a picture here, and this is the belt to that braces:
        # the cost of getting it wrong is repainting work that was already correct.
        canvas = Image.new("RGB", size, FILM_CANVAS)
        canvas.save(Path(out))
        return {"out": Path(out), "applied": [], "info": info, "ends": None, "untouched": True}

    field_rgb = _unhex(info["background"])
    field_lum = _lum(field_rgb)

    # ---- is this a picture or a surface?
    # One measurement answers both dangerous questions at once, which is why it is taken once. A
    # picture whose whole range is narrow has nothing to flip and nothing to keep: flipping a
    # greyscale photograph of a brightly lit wall turns a white wall black, and removing the "field"
    # of a surface that *is* one flat colour removes the picture. Both are the same fact — its range
    # is 20 levels wide, not 200 — so both are refused by the same test.
    ends_in = _percentiles(img)
    say = (ends_in is not None) and (ends_in[1] - ends_in[0]) >= TONE_MIN_RANGE

    if say and info["chroma"] < NEUTRAL_CHROMA and field_lum >= LIGHT_FIELD \
            and ends_in[0] <= field_lum - FLIP_DROP:
        img = _flip_value(img)                              # rule 2
        applied.append("flip")
        # The field flips with everything else, so the mask below is built from the *new* field
        # colour. Without this the flipped projection's field is 5,5,5 — dark, and 29 levels away from
        # the film's own 11,20,34, which is a second dark in one film: invisible to the eye and exactly
        # the thing this module exists to remove. The share is unchanged by a flip, because flipping
        # maps one colour to one colour.
        field_rgb = tuple(255 - v for v in field_rgb)

    if say and info["field_share"] >= FIELD_SHARE:
        diff = ImageChops.difference(
            img, Image.new("RGB", img.size, field_rgb)).convert("L")
        mask = diff.point(lambda v: 0 if v <= SAME_COLOUR else 255)      # rule 3
        applied.append("page removed")

    fw, fh = size
    if abs((img.width / img.height) / (fw / fh) - 1) <= ASPECT_TOLERANCE:
        # Already this frame's shape: it **fills** the frame. `thumbnail` only ever shrinks, so a
        # 900x1600 picture in a 1080x1920 frame used to be placed inside it with two strips of canvas
        # down the sides — which is not "cover", it is a small picture on a big frame.
        img = img.resize((fw, fh), Image.LANCZOS)
        if mask is not None:
            mask = mask.resize((fw, fh), Image.LANCZOS)
    else:
        box_w, box_h = int(fw * (1 - 2 * margin)), int(fh * (1 - 2 * margin))
        img.thumbnail((box_w, box_h), Image.LANCZOS)
        if mask is not None:
            mask.thumbnail((box_w, box_h), Image.LANCZOS)

    lut, ends = _ends_lut(img, film_lum=_lum(FILM_CANVAS), paper_lum=_lum(figure.PAPER), mask=mask)
    if lut is not None:
        img = Image.merge("RGB", tuple(ch.point(lut) for ch in img.split()))   # rule 4
        applied.append("ends matched")

    canvas = Image.new("RGB", size, FILM_CANVAS)
    canvas.paste(img, ((fw - img.width) // 2, (fh - img.height) // 2), mask)
    canvas.save(Path(out))
    return {"out": Path(out), "applied": applied, "info": info, "ends": ends, "untouched": False}


def _flip_value(im):
    """Reverse the grey scale, keeping hue and saturation untouched.

    `MONOCHROME1` in DICOM means exactly this picture: "the minimum sample value is intended to be
    displayed as white". Every viewer reverses it so the anatomy reads bright on a dark field. Doing
    it in HSV rather than on RGB matters for the coloured cases this is also used on: inverting RGB
    turns a red artery cyan, and an artery that changes colour between two shots is worse than a dark
    one.
    """
    from PIL import Image, ImageOps

    h, s, v = im.convert("HSV").split()
    return Image.merge("HSV", (h, s, ImageOps.invert(v))).convert("RGB")


# --------------------------------------------------------------------- framing a picture
def plate_write(src: "str | Path", out: "str | Path", size: tuple[int, int], *,
                heading: str = "", caption: str = "", credit: str = "") -> Path:
    """Put the picture on the spec's canvas with its heading, caption and source, and save it.

    Delegates to `figure.compose`, which is the same code that renders an approved still and every
    frame of an animation — so a shot does not change its layout when it changes from a drawing to a
    picture, which is the failure `figure.plate` was written to prevent.

    **The picture is toned first** (`tone_write`), and that is the fix for 「不是同等色调」: a plate used
    to paste a white page onto the film's dark canvas, so 17-30% of the frame stayed paper and a film
    with nine such pictures read as two films stapled together. The toned still is already the frame's
    size and already on the film's canvas, so the heading band, the caption and the credit line all
    land on one continuous field.
    """
    label = heading.strip() or i18n.pick_now(*PLATE_LABEL)
    src = Path(src)
    toned = Path(out).with_name(Path(out).stem + "-toned.png")
    tone_write(src, toned, size, margin=0.0)
    return figure.compose(toned, Path(out), size, heading=label, caption=caption.strip(),
                          credit=credit.strip(), body_fit="full", canvas=FILM_CANVAS)


# --------------------------------------------------------------------- what the models are told
def rules() -> str:
    """The spec, in the words a member can act on. Read by `tools.py`'s film guidance.

    Kept here rather than written out in the prompt so there is one text and one set of numbers: a
    prompt that quotes a tolerance the code does not use is worse than no prompt.
    """
    return i18n.pick_now(
        "**Shape before content.** Two numbers decide whether a picture can go into a film, and "
        "both are measured rather than judged. (a) *Shape*: a picture whose aspect is within "
        f"{ASPECT_TOLERANCE * 100:.0f}% of the frame's fills the frame; anything else is **framed** "
        "— put on the film's own background with a heading, a caption and its source line, never "
        "cropped and never stretched. So a sideways web picture or a chat screenshot is not "
        "forbidden, it is *plated*, and it then reads as a reference you are showing rather than as "
        "a picture that failed to fill the screen. Nothing is lost by this: the film already knows "
        "the picture's shape, so do not crop it by hand and do not ask for `cover` to \"make it "
        "fit\". (b) *Size*: the short edge should be at least "
        f"{SOFT_SHORT_EDGE}px and must be at least {MIN_SHORT_EDGE}px, or it is visibly upscaled. "
        "Give every picture a `credit` naming where it came from; a film that cannot answer that "
        "question is one nobody may publish.",
        "**先看形状,再看内容。** 两个数字决定一张图能不能进片子,而且都是量出来的,不是判断出来的。"
        f"(a) **形状**:长宽比与画幅相差 {ASPECT_TOLERANCE * 100:.0f}% 以内的,填满画幅;其余一律"
        "**装进画框**——放在片子自己的底色上,配标题、说明和来源行,绝不裁切、绝不拉伸。所以横版网图"
        "或聊天截图并不是被禁止,而是会被「装框」,读起来就成了一张「给你看的参考资料」,而不是一张"
        "没填满屏幕的图。这对你没有损失:程序已经知道它的形状,不要手工裁剪,也不要为了「让它填满」"
        f"而写 `cover`。(b) **尺寸**:短边应在 {SOFT_SHORT_EDGE} 像素以上,至少不得低于 "
        f"{MIN_SHORT_EDGE} 像素,否则放大痕迹肉眼可见。每张图都要给 `credit`,写清它从哪来——"
        "回答不了这个问题的成片,没有人可以发布。")


# --------------------------------------------------------------------- the house style
#
# The material a group hands over is the brief for how the film should look, and it is the only
# brief that cannot be argued with. So the style is **measured off the material**, not chosen here:
# `profile()` reads a set of pictures and reports what they have in common — the background they sit
# on, the colours they are drawn in, how heavy the lines are, and how much of the set actually agrees
# with the majority. `redraw()` then draws any flat picture again in those terms.
#
# Everything is measured with integer arithmetic and Pillow only, for two reasons: this machine has
# no generative model that can be trusted with anatomy, and a style that comes out of a random seed
# is a style nobody can hold still between two runs of the same film.

# How many source pixels one picture contributes to the pooled measurement. The set is measured as a
# set, so a 4000x6000 photograph must not outvote twenty diagrams.
PROFILE_BUDGET = 2_000

# Two colours count as the same colour when every channel is within this. 24/255 is below the
# threshold at which a person reliably separates two flat fills, so it groups a set drawn by the same
# hand and separates a set drawn by two.
SAME_COLOUR = 24

# A picture counts as **drawable again** rather than photographic when this much of it is one of its
# eight commonest colours. Measured on the group's own 22 pictures, and the two groups do not touch:
#
#   app-drawn figures and animations   0.73 – 0.80
#   web diagrams and a real angiogram  0.40 – 0.53
#   photographs and 3D renders         0.03 – 0.06
#
# 0.65 sits in the empty gap, and the pictures in the middle band are routed to `plate` on purpose:
# a re-draw is only free of risk when the picture is unambiguously a drawing, and one of the 0.42s
# is somebody's angiogram.
FLAT_COVERAGE = 0.65

# How the material's palette is read, and every one of these three numbers was chosen off the real
# material after a first attempt got it wrong. See `_palette`.
#
# A colour has to cover this much of *one* picture before it counts as a colour of the drawing at
# all: it removes anti-aliasing blends and JPEG speckle without touching a thin, deliberate stroke.
PALETTE_FLOOR = 0.002
# Colours are bucketed on a coarse grid before voting, so 0x0B1422 and 0x0C1016 are one colour and
# do not each take a slot.
PALETTE_GRID = 12
# How many colours the film may be drawn in. Measured: the material's own figures use eleven — three
# reds, a near-white, two blues, a cyan, two mauves and the heading band — and a smaller limit is how
# the blue and the cyan were lost the first time.
PALETTE_MAX = 14

# --------------------------------------------------------------------- the tone
# 「同等色调」 is not "the same average brightness" — it is **the same black and the same white**. A
# picture can have the film's average and still be a white page hung in a dark film, and that is
# measurable: of the 30 stills in the film this was written against, nine had a field at 189-255
# luminance covering 42-67% of the frame, so a third of the film was white paper. These four numbers
# are what separate "this picture is on a page" from "this picture fills its own frame".
#
# A field at or above this luminance is a page, a lightbox or a paper print — not a photograph's own
# darkness. 150 leaves room for a bright-but-real photograph (a wall, a drape) to stay well under it.
LIGHT_FIELD = 150
# And it has to cover this much of the picture before the picture is "on" something. Below it the
# light is part of the picture and removing it would remove the picture.
FIELD_SHARE = 0.25
# Below this chroma the picture is greyscale, and greyscale can be flipped **losslessly** (value
# inverted in HSV). This matters for medicine specifically: a projection stored light-on-dark is
# exactly what DICOM's `MONOCHROME1` means, and every viewer flips it so anatomy reads bright on a
# dark field. Flipping is the display convention and loses nothing; inverting *colour* would not be,
# which is why the test is neutral-by-measurement and not a guess.
NEUTRAL_CHROMA = 12
# A black end this far above the film's own black is lifted — the picture has no true black — and is
# pulled down so its shadows sit where the film's shadows sit.
TONE_LO_GAP = 40
# Flipping needs something to flip: the picture's own dark end has to be this far below its field
# before reversing it says anything. A projection is anatomy at 20 on a light box at 250 (230 below);
# a greyscale photograph of a brightly lit wall is 225 against a field of 238 (13 below), and flipping
# *that* turns a white wall black — which is what a fixture written for the case caught.
FLIP_DROP = 80
# A picture whose **whole range** is this narrow is not a picture with lifted shadows, it is a
# **surface**: a brightly lit wall, a drape, a white page. Three different wrong things were tried
# before this one test was found to answer all of them, and each was caught by a fixture written for
# the case rather than by reasoning:
#
#   * pulling its black down to the film's black stretched 16 levels of brightness across 224 and
#     turned a genuinely bright photograph black;
#   * flipping it (a bright wall is greyscale, so the neutral test alone said "flip") turned a white
#     wall black;
#   * removing its "field" removed the picture, because on a surface the field **is** the picture.
#
# One measurement refuses all three: a picture spans 200 levels, a surface spans 20. Below this range
# nothing is done to it at all — no flip, no matte, no grade — and it is placed as it is.
TONE_MIN_RANGE = 60
# The share above is what actually separates a **page under a picture** from **a picture that is
# bright**, and a second test was tried and rejected — recorded here because the idea is the obvious
# one and it does not work:
#
#   "a page is flat at the edges and something else in the middle" → require the border to be ≥60% the
#   field and the middle ≤35% of it. Measured on the group's own 11 light-field pictures, the border
#   statistic came out 0.37-0.84 — the **lowest** of four bands is not a good statistic on a 256px
#   thumbnail, because a wide screenshot's content runs into one band. The rule rejected nine real
#   pages while catching nothing the share test had missed: the two genuinely bright photographs that
#   had to be protected were already at 12% and 14% field share, well under `FIELD_SHARE`.
#
# So the share is the gate and these two numbers are diagnostics, reported in the film's notes so a
# person can check the call rather than trust it.
FIELD_RING = 0.60
FIELD_INNER = 0.35


def _lum(rgb: tuple) -> float:
    """A colour's own brightness, on the one weighting that answers "does this read as light?"."""
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


# The spec, in one place, written **after** the numbers it quotes: it references the tone constants
# above, so defining it next to `FILM_CANVAS` would have been a NameError at import.
SPEC: dict[str, Any] = {
    "aspect_tolerance": ASPECT_TOLERANCE,
    "min_short_edge": MIN_SHORT_EDGE,
    "soft_short_edge": SOFT_SHORT_EDGE,
    "fits": FITS,
    # Taken from `figure` rather than repeated: a second copy of "the background" is exactly how
    # eight backgrounds happened.
    "background": "#%02X%02X%02X" % FILM_CANVAS,
    "band": "#%02X%02X%02X" % figure.BAND,
    "accent": "#%02X%02X%02X" % figure.ACCENT,
    # The tone, so a film's notes can state it rather than describe it.
    "film_black": round(_lum(FILM_CANVAS), 1),
    "light_field": LIGHT_FIELD,
    "field_share": FIELD_SHARE,
    "neutral_chroma": NEUTRAL_CHROMA,
}


def _border_canvas(im) -> tuple:
    """The colour of a picture's own canvas, read off its **real** border pixels.

    Read at full resolution rather than off a thumbnail, and this is not a detail: a thumbnail
    averages eleven source pixels into one, so the first row of a 96px reduction of a 1080x1920
    frame is a blend of the film's background with whatever the picture put there. Measured that way,
    every still in a set *looks* like it has its own background — which is precisely the number this
    module has to report honestly, so it is read where it is true.
    """
    w, h = im.size
    step = max(1, min(w, h) // 96)
    edge = []
    for x in range(0, w, step):
        edge.append(im.getpixel((x, 0)))
        edge.append(im.getpixel((x, h - 1)))
    for y in range(0, h, step):
        edge.append(im.getpixel((0, y)))
        edge.append(im.getpixel((w - 1, y)))
    counts: dict[tuple, int] = {}
    for px in edge:
        counts[px] = counts.get(px, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0], edge


def _profile_pixels(im, budget: int = PROFILE_BUDGET) -> tuple[list, tuple]:
    """One picture's contribution: its pixels, and the colour of its own canvas.

    The canvas is read off the **border**, not off the whole picture: a diagram's most common colour
    is its background and so is a photograph's, but the border is where a background is unambiguous —
    the middle of a picture is where the subject is.
    """
    from PIL import Image

    canvas, _ = _border_canvas(im)
    small = im.copy()
    small.thumbnail((96, 96), Image.LANCZOS)
    w, h = small.size
    step = max(1, int((w * h / max(1, budget)) ** 0.5))
    pixels = [small.getpixel((x, y)) for y in range(0, h, step) for x in range(0, w, step)]
    return pixels, canvas


def _palette(pictures: "list[Path]", canvas: tuple, *, limit: int = PALETTE_MAX) -> list[tuple]:
    """The colours a set of **drawings** actually uses, ranked by how many of them use each one.

    ⚠️ Two things about this are the whole lesson, and the first version of this module got both
    wrong, which is why the film came back with the coil and the catheter painted over:

    **Only the drawings vote.** Photographs and screenshots are never re-drawn — they are framed — so
    they have no business deciding the palette of the things that are. The first version pooled all
    30 pictures, of which 53% were photographs and chat screenshots, and the greys from those landed
    in the palette while the material's own blue and cyan did not.

    **Ranked by pictures, not by pixels.** This is the part that a frequency-weighted clustering
    cannot do. In a teaching schematic the parts that carry the meaning — the coil, the catheter, the
    flow arrows — are *thin*, and by pixel share they lose to a background and a vessel wall every
    time: measured on the real material, the coil was 0.8% of its picture and the catheter's blue was
    0.5%. k-means on pixel counts duly absorbed both into whatever was darkest nearby, and the
    catheter came out grey. A colour used by eleven of fifteen pictures is what this set *is*, however
    few pixels it covers, so the vote is per picture: a colour that appears in a picture at all (above
    the noise floor) is evidence, and one that covers a fifth of a single photograph is not.

    The representative kept for each bucket is the commonest exact colour inside it, so the palette
    holds the material's real values rather than an average of them — which is what makes re-drawing
    a picture this app drew itself nearly a no-op.
    """
    from PIL import Image

    votes: dict[tuple, list] = {}
    for p in pictures:
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                total = im.width * im.height
                counts: dict[tuple, int] = {}
                for px in im.getdata():
                    counts[px] = counts.get(px, 0) + 1
        except Exception:                       # an unreadable picture is already reported by profile
            continue
        seen: set = set()
        for colour, n in counts.items():
            if n / total < PALETTE_FLOOR:
                continue
            if max(abs(a - b) for a, b in zip(colour, canvas)) <= SAME_COLOUR:
                continue                        # the background is not a colour of the drawing
            key = tuple(v * PALETTE_GRID // 256 for v in colour)
            row = votes.setdefault(key, [0, 0, {}])
            row[1] += n
            row[2][colour] = row[2].get(colour, 0) + n
            if key not in seen:                 # once per picture, however many shades land in it
                seen.add(key)
                row[0] += 1
    ranked = sorted(votes.values(), key=lambda r: (-r[0], -r[1]))
    out = [max(r[2].items(), key=lambda kv: kv[1])[0] for r in ranked[:limit]]
    return sorted(out, key=lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])


def profile(paths: "list[Path] | tuple[Path, ...]", *, k: int = PALETTE_MAX) -> dict:
    """What a set of pictures has in common — the film's style, measured rather than chosen.

    Reads every picture that can be read, pools them evenly, and reports:

      * `canvas` — the background the material sits on, and `canvas_rgb` for it. If the set does not
        agree on this, `split` names the group that disagrees, because "these eleven are white on
        white and these four are dark" is the sentence that explains a film that looks like two
        films stapled together.
      * `ramp` — the content colours, darkest first. A picture is re-drawn by mapping its luminance
        onto this ramp, which keeps its structure and gives it the set's palette.
      * `line_weight` — 0..1, from edge density. A set of thin line drawings and a set of heavy ones
        are two different visual languages even in the same colours.
      * `flat_share` — how much of the set is drawable again rather than photographic. This is what
        decides `redraw` against `plate` per picture.
      * `unreadable`, `agreement` — the honest parts. A style inferred from three of thirty pictures
        is not the style of the material, and the caller is told so instead of guessing.
    """
    from PIL import Image

    canvases: list[tuple] = []
    edges: list[float] = []
    flats: list[bool] = []
    unreadable: list[str] = []
    per_picture: list[dict] = []

    for p in paths:
        p = Path(p)
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                pixels, canvas = _profile_pixels(im)
        except Exception as e:
            unreadable.append(f"{p.name}: {type(e).__name__}")
            continue
        canvases.append(canvas)
        gray = im.convert("L")
        gray.thumbnail((128, 128), Image.LANCZOS)
        gw, gh = gray.size
        data = list(gray.getdata())
        diff = 0
        for y in range(gh):
            row = y * gw
            for x in range(1, gw):
                diff += abs(data[row + x] - data[row + x - 1])
        edges.append(diff / max(1, gw * (gh - 1)) / 255.0)
        counts: dict[tuple, int] = {}
        for px in pixels:
            counts[px] = counts.get(px, 0) + 1
        flat = sum(sorted(counts.values(), reverse=True)[:8]) / max(1, len(pixels)) >= FLAT_COVERAGE
        flats.append(flat)
        per_picture.append({"path": str(p), "canvas": _hex(canvas), "colours": len(counts),
                            "flat": flat})

    canvas_counts: dict[tuple, int] = {}
    for c in canvases:
        canvas_counts[c] = canvas_counts.get(c, 0) + 1
    canvas = max(canvas_counts.items(), key=lambda kv: kv[1])[0] if canvas_counts else FILM_CANVAS
    disagreeing = [row for row in per_picture
                   if max(abs(a - b) for a, b in zip(_unhex(row["canvas"]), canvas)) > SAME_COLOUR]

    # ⚠️ The palette comes off **the drawings only**, and this was the module's worst bug. It used to
    # be pooled over every picture — including the 53% of them that are photographs and chat
    # screenshots, which are never re-drawn at all — and the greys from those took the slots that
    # belonged to the material's own blues. A set whose drawings are all dark-on-dark would also have
    # had its palette read off a white-page chart someone dropped in beside them.
    drawable = [Path(row["path"]) for row in per_picture if row["flat"]]
    ramp = _palette(drawable or [Path(row["path"]) for row in per_picture], canvas, limit=k)

    # ⚠️ The film's own canvas is **not** the material's. The material's canvas decides what has to
    # happen to the material (how many pictures disagree with each other, and whether ink has to
    # trade places with paper); the film's canvas is the one its cards, captions and credit lines
    # are already drawn on. Using the material's canvas here instead would put two backgrounds in
    # one film — the exact fault this module exists to remove — and would make a set of white-page
    # diagrams produce a white film whose dark title cards no longer belong to it.
    #
    # It is `DRAWING_BG`, not `INK`, and that choice is what stops the engine damaging work that was
    # already right: `DRAWING_BG` is the field this app's own schematics and animations are drawn on,
    # so for those pictures a re-draw is a no-op instead of a 4/255 repaint of 73% of their pixels.
    # `INK` is the plate's; keeping them apart meant two darks in one film.
    film_canvas = FILM_CANVAS
    return {
        "count": len(per_picture) + len(unreadable),
        "read": len(per_picture),
        "unreadable": unreadable,
        "canvas": _hex(canvas),
        "canvas_rgb": tuple(canvas),
        "film_canvas": _hex(film_canvas),
        "film_canvas_rgb": film_canvas,
        "ramp": [_hex(c) for c in ramp],
        "ramp_rgb": [tuple(c) for c in ramp],
        "line_weight": round(sum(edges) / max(1, len(edges)), 4),
        "flat_share": round(sum(1 for f in flats if f) / max(1, len(flats)), 3),
        "agreement": round(1 - len(disagreeing) / max(1, len(per_picture)), 3),
        "split": disagreeing[:8],
        "pictures": per_picture,
    }


def _unhex(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _ramp_palette(prof: dict):
    """The film's colours as a Pillow palette image, for `quantize(palette=...)`.

    Nearest-colour mapping rather than a luminance lookup table, and the difference is not academic:
    mapping by luminance alone collapses a red vessel and a blue catheter that happen to be equally
    bright into the *same* colour, which is how a medical schematic stops explaining anything. Pillow
    does the nearest-colour match in C, so it stays fast.
    """
    from PIL import Image

    ramp = [tuple(c) for c in (prof.get("ramp_rgb") or [])] or [tuple(figure.PAPER)]
    pal = Image.new("P", (1, 1))
    flat = [v for rgb in ramp for v in rgb]
    pal.putpalette(flat + [0] * (768 - len(flat)))
    return pal


def _tone_for(prof: dict, own_canvas: tuple, img):
    """Swap ink and paper, but only when *this* picture actually has ink and paper — and never for
    the whole set.

    Two conditions, both required, and the second one was added after a test caught what the first
    alone does. A picture has to be (a) sitting on a canvas lighter than the film's, and (b) actually
    drawn dark-on-light — its content darker than its own canvas. Without (b), a solid colour that
    fills the frame is "light" and would be inverted into a dark frame, which is not a style decision
    but a way to turn a yellow title frame black.

    The first version tested only (a), once for the whole set, and it was wrong in a way only a
    reading of the output caught: the app's own schematics are already drawn on a dark canvas, and
    inverting them turned their white labels black, which the film's dark background then swallowed —
    the labels were still in the file and no longer on the screen.

    The inversion is done in HSV with the hue and saturation untouched and only the *value* inverted,
    so a light diagram becomes a dark one without its reds turning cyan. Inverting RGB would do
    exactly that, and a schematic whose artery changed colour between two shots is worse than a dark
    one.
    """
    from PIL import Image, ImageChops, ImageOps

    film = tuple(prof.get("film_canvas_rgb") or FILM_CANVAS)
    film_lum = 0.299 * film[0] + 0.587 * film[1] + 0.114 * film[2]
    own_lum = 0.299 * own_canvas[0] + 0.587 * own_canvas[1] + 0.114 * own_canvas[2]
    if own_lum <= film_lum + 40:
        return None                     # already dark: leave it alone

    small = img.copy()
    small.thumbnail((96, 96), Image.LANCZOS)
    mask = ImageChops.difference(
        small, Image.new("RGB", small.size, tuple(own_canvas))).convert("L").point(
            lambda v: 255 if v > 40 else 0)
    kept = [p for p, m in zip(small.convert("L").get_flattened_data(), mask.get_flattened_data())
            if m]
    if not kept or sum(kept) / len(kept) >= own_lum - 10:
        return None                     # nothing darker than the canvas: no ink to swap

    def apply(im):
        h, s, v = im.convert("HSV").split()
        return Image.merge("HSV", (h, s, ImageOps.invert(v))).convert("RGB")

    return apply


def redraw(src: "str | Path", out: "str | Path", size: tuple[int, int], prof: dict, *,
           margin: "float | None" = None, repaint: bool = True) -> Path:
    """Draw a flat picture again in the film's style: same structure, the film's colours.

    The order of the four steps is the whole implementation, and **three of them were wrong first**:

      1. **Its own background goes.** The pixels near the picture's own canvas colour are masked out,
         so a chart rescued from a white page does not arrive as a white rectangle on a dark film.
      2. **Everything is resized before anything is quantised.** Quantising first and resizing after
         lets the resampler interpolate the palette back into thousands of colours — measured: 1743
         distinct colours in a picture that was supposed to have six.
      3. **The colours are replaced first, and ink and paper trade places after** — in that order, and
         the order is not cosmetic. Swapping the tone first makes the picture *dark and desaturated*,
         and the nearest colour in a palette built from the original becomes whichever hue happens to
         be darkest, so a red artery arrives blue. Quantise first (hue-faithful), then invert the
         value, and the red stays red at a lower key.
      4. **The inversion itself is done in HSV**, with hue and saturation untouched, and only for the
         pictures that need it — see `_tone_for`.

    It is deliberately **not** for photographs. Quantising a photograph produces a posterised picture
    that claims to be a real one, and on an angiogram or an intraoperative photograph that is the
    failure this project treats as expensive. `admit()` routes those to `plate` instead.
    """
    from PIL import Image, ImageChops, ImageOps

    p = Path(src)
    with Image.open(p) as im:
        img = ImageOps.exif_transpose(im).convert("RGB")
    _, own_canvas = _profile_pixels(img, budget=400)
    # The mask is built from the difference against the picture's own background, so a background
    # that is nearly-but-not-quite flat (a JPEG, a scan) is removed with it.
    diff = ImageChops.difference(img, Image.new("RGB", img.size, tuple(own_canvas))).convert("L")

    fw, fh = size
    # A margin exists so that a picture which does *not* fill the frame looks placed rather than
    # broken. A picture that already is the frame's shape has nothing to apologise for, and giving it
    # one would shrink it for no reason — so the margin is decided per picture rather than fixed.
    if margin is None:
        margin = 0.0 if abs((img.width / img.height) / (fw / fh) - 1) <= ASPECT_TOLERANCE else 0.04
    box = (int(fw * (1 - 2 * margin)), int(fh * (1 - 2 * margin)))
    img.thumbnail(box, Image.LANCZOS)
    diff.thumbnail(box, Image.LANCZOS)          # same source size, so the same scale factor

    if repaint:
        content = img.quantize(palette=_ramp_palette(prof), dither=Image.Dither.NONE).convert("RGB")
        tone = _tone_for(prof, own_canvas, img)
        if tone is not None:
            content = tone(content)
        mask = diff.point(lambda v: 0 if v <= 40 else 255)
    else:
        # `repaint=False`: the picture is already on the film's own background, so it is placed and
        # **nothing else is done to it** — not its colours, and not its near-background pixels either.
        # Measured before this existed: a schematic the app had drawn itself came out 78.7% identical,
        # the missing 21% being its heading band and its darker field, which the mask removed because
        # both sit within tolerance of the canvas. The whole rectangle is pasted instead, which is
        # seamless precisely because the picture's background *is* the canvas.
        # `admit()` only asks for this when the picture is already in the film's world, so it is not a
        # way to skip work — there is no work to do.
        content, mask = img, None
    canvas = Image.new("RGB", size, tuple(prof.get("film_canvas_rgb") or FILM_CANVAS))
    canvas.paste(content, ((fw - content.width) // 2, (fh - content.height) // 2), mask)
    canvas.save(Path(out))
    return Path(out)


def save_profile(prof: dict, path: "str | Path") -> Path:
    """Write the style beside the film, so a re-cut months later is the same film."""
    import json
    p = Path(path)
    p.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------- reporting a folder
def report(paths: "list[Path] | tuple[Path, ...]", size: tuple[int, int]) -> dict:
    """Measure many pictures at once: the totals a folder can be judged by.

    This is what the `lint-figures` script prints and what the tests assert against, so "the film's
    pictures got more consistent" is a number rather than an impression.
    """
    rows = []
    for p in paths:
        try:
            row = audit(p)
        except Exception as e:                      # unreadable is a finding, not a crash
            rows.append({"path": str(p), "error": f"{type(e).__name__}: {e}"})
            continue
        frame, delta = _against(row, size)
        row["ratio_delta"] = round(delta, 4)
        row["verdict"] = ("redraw" if row.get("flat")
                          else "cover" if delta <= ASPECT_TOLERANCE else "plate")
        rows.append(row)
    good = [r for r in rows if "error" not in r]
    return {
        "frame": f"{size[0]}x{size[1]}",
        "count": len(rows),
        "unreadable": len(rows) - len(good),
        "off_shape": sum(1 for r in good if r["verdict"] != "cover"),
        "to_redraw": sum(1 for r in good if r["verdict"] == "redraw"),
        "to_plate": sum(1 for r in good if r["verdict"] == "plate"),
        "worst_delta": max((r["ratio_delta"] for r in good), default=0.0),
        # The count of distinct backgrounds is the headline number of the complaint this module
        # exists for: it was 8 out of 30. After every still is placed by this module it is 1, because
        # every path through `admit` writes the film's own background.
        "backgrounds": sorted({r["background"] for r in good}),
        "colour_spread": (max((r["colours"] for r in good), default=0)
                          - min((r["colours"] for r in good), default=0)),
        "small": sum(1 for r in good if r["short_edge"] < MIN_SHORT_EDGE),
        "soft": sum(1 for r in good if MIN_SHORT_EDGE <= r["short_edge"] < SOFT_SHORT_EDGE),
        "rows": rows,
    }
