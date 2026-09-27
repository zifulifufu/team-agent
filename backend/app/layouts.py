"""Layout templates for `assemble_video` — **how a film looks**, as opposed to what is in it.

`assemble.py` has one set of numbers for how a film looks: subtitle size and plate, the title card's
colours, where a credit sits, how long a still is held. Those numbers are taste, and taste differs by
audience and by occasion. A public-science clip for a phone wants large subtitles low on the frame
and a disclaimer at the end; an internal case review wants a small credit and no opening card at all;
a lecture recording wants a different aspect and a slower pace. Right now changing any of that is a
code change, which means it is never changed.

So they live in files: `<data dir>/layouts/*.json`, one layout each, picked by name on the tool call.
Everything is optional and every default equals what the code did before this module existed, so a
film made without naming a layout is byte-for-byte the film it used to be.

⚠️ Three rules, each about the same failure — a film that claims a look it does not have:
  * a file that cannot be used is **named, with its reason**, never skipped. A layout that is quietly
    not loaded assembles with the default look while the caller believes it got the clinic one.
  * a value out of range is an **error**, not a clamp. "Subtitles at 1.4 of the height" is a typo,
    and a film that silently ignores it is one nobody can debug from the output.
  * an unknown layout name is an **error** too, and it lists what does exist. There is no sensible
    default here: `assemble.py` has one, and using it while the caller asked for another is the
    silent substitution this project treats as the expensive mistake.

What a layout *cannot* change is the parts that are not taste: the `4n+1`-style arithmetic, the
encoder, the concat. Those are correctness.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import i18n

__all__ = ["BUILTIN", "DEFAULT", "use_folder", "reload", "available", "names", "row", "errors",
           "style", "LAYOUT_SUFFIX", "MAX_BYTES"]

LAYOUT_SUFFIX = ".json"
MAX_BYTES = 256_000          # a layout is a page of numbers; a quarter of a megabyte is a mistake

# A colour the drawing code will accept. Kept here rather than imported from `assemble` so this
# module stays a leaf — it is read by the tool layer before anything is assembled.
_COLOR = "#RRGGBB"


def _c(value: object, default: str, field: str) -> str:
    v = str(value if value is not None else default).strip()
    if not isinstance(value, str) or (value and not (len(v) == 7 and v.startswith("#"))):
        raise ValueError(i18n.pick_now(
            f"`{field}` must be a colour like \"#0E1B2A\", not {value!r}",
            f"`{field}` 必须是像「#0E1B2A」这样的颜色,{value!r} 不是"))
    return v


def _n(value: object, default: float, field: str, *, low: float, high: float) -> float:
    if value is None or value == "":
        return float(default)
    try:
        got = float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(i18n.pick_now(f"`{field}` must be a number, not {value!r}",
                                       f"`{field}` 必须是个数字,{value!r} 不是")) from None
    if not (low <= got <= high):
        raise ValueError(i18n.pick_now(
            f"`{field}` is {got:g}; it has to be between {low:g} and {high:g}",
            f"`{field}` 是 {got:g},必须在 {low:g} 到 {high:g} 之间"))
    return got


def _text(value: object) -> str:
    return str(value or "").strip()


def _enum(value: object, default: str, field: str, allowed: tuple[str, ...]) -> str:
    got = _text(value).lower() or default
    if got not in allowed:
        raise ValueError(i18n.pick_now(
            f"`{field}` is \"{got}\"; use one of: {', '.join(allowed)}",
            f"`{field}` 是「{got}」;可用:{'、'.join(allowed)}"))
    return got


def _style_body(body: dict) -> dict:
    """One layout's numbers, checked. Raises `ValueError` whose text is the whole diagnosis.

    Ranges are wide but real. A subtitle font at `0.045` of the frame height is what looking at a
    finished frame produced; the bounds below are there to catch a typo (`4.5`, `0.45`) rather than
    to enforce taste.
    """
    sub = body.get("subtitle") or {}
    card = body.get("card") or {}
    credit = body.get("credit") or {}
    pace = body.get("pace") or {}
    band = body.get("music") or {}
    opening = body.get("opening") or {}
    closing = body.get("closing") or {}
    transition = body.get("transition") or {}
    if not isinstance(sub, dict) or not isinstance(card, dict) or not isinstance(credit, dict) \
            or not isinstance(pace, dict) or not isinstance(opening, dict) \
            or not isinstance(closing, dict) or not isinstance(transition, dict):
        raise ValueError(i18n.pick_now(
            "`subtitle`, `card`, `credit`, `pace`, `opening`, `closing` and `transition` must each "
            "be an object",
            "`subtitle`、`card`、`credit`、`pace`、`opening`、`closing`、`transition` 每一个都必须是对象"))
    return {
        "title": _text(body.get("title")),
        "note": _text(body.get("note")),
        "size": _text(body.get("size")),
        "fps": int(_n(body.get("fps"), 0, "fps", low=0, high=60)),
        "fit": _text(body.get("fit")),
        "subtitle": {
            "font_scale": _n(sub.get("font_scale"), 0.045, "subtitle.font_scale", low=0.015, high=0.12),
            # Where the block's *bottom* sits, as a share of the height. 0.86 is the measured
            # default: a generated clip often carries its own caption along the bottom edge, and
            # every vertical platform covers roughly the last tenth with its own UI.
            "bottom": _n(sub.get("bottom"), 0.86, "subtitle.bottom", low=0.3, high=0.98),
            "wrap": _n(sub.get("wrap"), 0.86, "subtitle.wrap", low=0.4, high=1.0),
            "box": _c(sub.get("box", "#000000"), "#000000", "subtitle.box") if "box" in sub
                   else "#000000",
            "alpha": int(_n(sub.get("alpha"), 130, "subtitle.alpha", low=0, high=255)),
            "colour": _c(sub.get("colour", "#FFFFFF"), "#FFFFFF", "subtitle.colour")
                      if "colour" in sub else "#FFFFFF",
        },
        "card": {
            "background": _c(card.get("background", "#0E1B2A"), "#0E1B2A", "card.background"),
            "accent": _c(card.get("accent", "#2E7CF6"), "#2E7CF6", "card.accent"),
            "title_scale": _n(card.get("title_scale"), 0.062, "card.title_scale",
                              low=0.02, high=0.15),
            "subtitle_scale": _n(card.get("subtitle_scale"), 0.032, "card.subtitle_scale",
                                 low=0.012, high=0.09),
        },
        "pace": {
            "still": _n(pace.get("still"), 0.0, "pace.still", low=0, high=60),
            "max_shot_seconds": _n(pace.get("max_shot_seconds"), 0.0, "pace.max_shot_seconds",
                                   low=0, high=600),
            # How fast a still drifts when a shot asks for `motion`. 0.00035 per frame is the value
            # that was hardcoded: it reaches 1.12x over a four-second still, which reads as a slow
            # push rather than a zoom. This is the knob a "cinematic long take" is asking for.
            "zoom": _n(pace.get("zoom"), 0.00035, "pace.zoom", low=0.0, high=0.005),
            "zoom_max": _n(pace.get("zoom_max"), 1.12, "pace.zoom_max", low=1.0, high=2.0),
        },
        # `cut` is what this app has always done (a plain concat, no transition at all). `fade` is a
        # dip through black at each end of every shot, applied to the **composite** so the captions
        # and the credit line fade with the picture rather than sitting at full brightness on a
        # black frame. A cross-dissolve between shots needs one filter graph over the whole film and
        # is not implemented — naming it here would be offering a knob that does nothing.
        "transition": {
            "kind": _enum(transition.get("kind"), "cut", "transition.kind", ("cut", "fade")),
            "seconds": _n(transition.get("seconds"), 0.4, "transition.seconds",
                          low=0.05, high=2.0),
        },
        "opening": {"title": _text(opening.get("title")), "subtitle": _text(opening.get("subtitle"))},
        "closing": {"title": _text(closing.get("title")), "subtitle": _text(closing.get("subtitle"))},
        # The standing line: who made it, whose picture it is, which unit it belongs to. It goes on
        # every shot rather than on a card, because a card is the one thing a re-cut can drop —
        # and this is the line that has to survive a re-cut.
        "credit": {
            "text": _text(credit.get("text")),
            "position": (_text(credit.get("position")) or "bottom").lower(),
            "scale": _n(credit.get("scale"), 0.026, "credit.scale", low=0.010, high=0.08),
            "colour": _c(credit.get("colour", "#FFFFFF"), "#FFFFFF", "credit.colour"),
        },
        # How a music bed sits under the film, when the caller names a track. Same split as the
        # title card: **the layout owns the mixing** — level, fades, and whether the music gets out
        # of the narrator's way — and **the caller owns the choice of track**. "Which piece" changes
        # with every film; "how loud under a voice" does not, and a film that answered it per film
        # would be mixed differently by whoever happened to be driving.
        "music": {
            # Relative to the narration, not to the encoder's ceiling: -16 dB under a voice is a
            # bed you notice only when it stops, which is what a bed is for.
            "volume_db": _n(band.get("volume_db"), -16.0, "music.volume_db", low=-40.0, high=6.0),
            "fade_in": _n(band.get("fade_in"), 1.5, "music.fade_in", low=0.0, high=20.0),
            "fade_out": _n(band.get("fade_out"), 2.5, "music.fade_out", low=0.0, high=20.0),
            "mix": _n(band.get("mix"), 1.0, "music.mix", low=0.0, high=2.0),
            # Ducking, driven by the timeline instead of guessed at by a compressor: each shot
            # records how long its narration actually ran (`voice_seconds`), so the music can be
            # lowered over exactly those spans. 0 disables it, and is what you want when there is
            # no narration at all. `db` rather than a ratio, because "how far down" is the thing a
            # person can hear and state; the compressor's ratio is not.
            "duck_db": _n(band.get("duck_db"), -10.0, "music.duck_db", low=-30.0, high=0.0),
            # A little overlap past the last syllable, so the music swells back rather than snapping
            # up the instant the voice stops.
            "duck_tail": _n(band.get("duck_tail"), 0.35, "music.duck_tail", low=0.0, high=3.0),
        },
    }


BUILTIN: dict[str, dict] = {
    # What `assemble_video` did before layouts existed. Named rather than implicit so a caller can
    # say "the default one" out loud, and so the tool's schema can offer it. It describes itself
    # too: it is one of the names in a dropdown, and a dropdown entry with no label is one nobody
    # picks on purpose.
    "default": {**_style_body({}), "title": "Default (unchanged)",
                "note": "Exactly what this app did before layouts existed: plain captions at the "
                        "measured height, no cards, no credit, no per-shot ceiling."},
    # A vertical clip for a general audience on a phone: bigger subtitles, held lower, a slower
    # still, and room for the line that says where the picture came from.
    "public-science": _style_body({
        "title": "Public science (vertical)",
        "note": "Large subtitles low on the frame, slower stills, a standing credit line.",
        "size": "1080x1920",
        "subtitle": {"font_scale": 0.055, "bottom": 0.84, "alpha": 150},
        "pace": {"still": 4.5, "max_shot_seconds": 12},
        "credit": {"position": "top", "scale": 0.024},
    }),
    # An internal case review: no opening card to sit through, a small credit, a quicker cut, and
    # 16:9 because it will be watched on a monitor rather than held in a hand.
    "case-review": _style_body({
        "title": "Case review (16:9, internal)",
        "note": "Landscape, small captions, no title card, tighter shots.",
        "size": "1920x1080",
        "subtitle": {"font_scale": 0.030, "bottom": 0.90, "alpha": 110, "wrap": 0.80},
        "card": {"background": "#101418", "accent": "#4A6B8A"},
        "pace": {"still": 3.0, "max_shot_seconds": 20},
        "credit": {"position": "bottom", "scale": 0.020},
    }),
    # A lecture or a conference talk: quiet, unadorned, and it ends on the speaker's own words with
    # the standing line already on every frame.
    "lecture": _style_body({
        "title": "Lecture (16:9)",
        "note": "Landscape, plain captions, a title card and a closing card.",
        "size": "1920x1080",
        "subtitle": {"font_scale": 0.034, "bottom": 0.88, "alpha": 120},
        "card": {"background": "#FFFFFF", "accent": "#1F6FEB"},
        "pace": {"still": 5.0},
        "opening": {"title": "", "subtitle": ""},
        "credit": {"position": "bottom", "scale": 0.022},
    }),
}

DEFAULT = "default"

_USER: dict[str, dict] = {}
_USER_ERRORS: list[dict] = []
_FOLDER: "Path | None" = None
_SIGN: tuple = ()


def use_folder(folder: "str | Path | None") -> dict:
    """Where the user's own layouts live. Called once at startup; safe to call again."""
    global _FOLDER, _SIGN
    _FOLDER = Path(folder) if folder else None
    _SIGN = ()
    if _FOLDER is not None:
        try:
            _FOLDER.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _note_folder_error(i18n.pick_now(
                f"the folder could not be created ({type(e).__name__}: {e}), so no layout of yours "
                "can be loaded",
                f"这个目录建不出来({type(e).__name__}: {e}),所以你的版式一份都读不到"))
    return reload(force=True)


def _note_folder_error(why: str) -> None:
    global _USER_ERRORS
    _USER_ERRORS = [e for e in _USER_ERRORS if e["file"] != "layouts/"] + \
                   [{"file": "layouts/", "why": why}]


def _sign() -> tuple:
    if _FOLDER is None or not _FOLDER.is_dir():
        return ()
    out = []
    for p in sorted(_FOLDER.glob("*" + LAYOUT_SUFFIX)):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def reload(*, force: bool = False) -> dict:
    """Re-read the folder if it changed. `{loaded: [names], errors: [{file, why}]}`."""
    global _USER, _USER_ERRORS, _SIGN
    sign = _sign()
    if not force and sign == _SIGN:
        return {"loaded": sorted(_USER), "errors": list(_USER_ERRORS)}
    _SIGN = sign
    rows: dict[str, dict] = {}
    errors: list[dict] = [e for e in _USER_ERRORS if e["file"] == "layouts/"]
    for path in (sorted(_FOLDER.glob("*" + LAYOUT_SUFFIX)) if _FOLDER else []):
        try:
            raw = path.read_bytes()
        except OSError as e:
            errors.append({"file": path.name, "why": f"{type(e).__name__}: {e}"})
            continue
        if len(raw) > MAX_BYTES:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"the file is {len(raw) // 1024} KB; a layout is a page of numbers",
                f"这个文件有 {len(raw) // 1024} KB,而一份版式只有一页数字")})
            continue
        try:
            body = json.loads(raw)
        except ValueError as e:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"not valid JSON: {e}", f"不是合法的 JSON:{e}")})
            continue
        name = path.stem
        if name in BUILTIN:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"the name \"{name}\" is already taken by a layout this app ships — rename the file",
                f"「{name}」这个名字已经被本程序自带的版式占了 —— 请给文件改个名")})
            continue
        if not isinstance(body, dict):
            errors.append({"file": path.name, "why": i18n.pick_now(
                "the file must contain a JSON object", "文件内容必须是一个 JSON 对象")})
            continue
        try:
            rows[name] = _style_body(body)
        except ValueError as e:
            errors.append({"file": path.name, "why": str(e)})
    _USER = rows
    _USER_ERRORS = errors
    return {"loaded": sorted(rows), "errors": errors}


def available() -> dict[str, dict]:
    """Every layout that can be used: the ones we ship, plus the user's own folder."""
    reload()
    return {**BUILTIN, **_USER}


def names() -> list[str]:
    return sorted(available())


def row(name: str) -> "dict | None":
    return available().get((name or "").strip())


def errors() -> list[dict]:
    reload()
    return list(_USER_ERRORS)


def style(name: str) -> dict:
    """The numbers for one layout, by name. Raises `ValueError` for a name that does not exist.

    No fallback on purpose. `assemble.py` has a default look and it is called `default`; quietly
    giving it to a caller who asked for `clinic` would produce a film that is *nearly* what was
    asked for, which is the one outcome nobody notices.
    """
    want = (name or "").strip() or DEFAULT
    got = row(want)
    if got is None:
        broken = [e for e in errors() if e["file"] == want + LAYOUT_SUFFIX]
        why = (" " + broken[0]["why"]) if broken else ""
        raise ValueError(i18n.pick_now(
            f"There is no layout called \"{want}\".{why} Known layouts: {', '.join(names())}.",
            f"没有叫「{want}」的版式。{why}现有的版式:{'、'.join(names())}。"))
    return got
