"""Turning a drawing into a real clip: draw the frames here, pipe them to ffmpeg.

Why this module exists at all: a teaching film made of stills is a slide show, and a slide show is
not a film. Blood that does not flow, a bulge that is already there in the first frame, a coil that
appears fully wound — none of that shows the thing the narration is describing. And asking a
diffusion model to animate it would mean asking it to invent the anatomy it moves, which is the
mistake this whole path exists to avoid.

So the motion is computed by `figure.drawer` and only the encoding lives here. That gives three
properties a generated clip cannot offer:

* **the anatomy is the one the still showed** — same geometry, same anchors, same labels, so a frame
  a reviewer approved and the animation that follows are the same picture;
* **it is reviewable before it is rendered** — a poster frame is written out beside the clip, because
  a still can be looked at and changed while a rendered clip can only be thrown away;
* **it is free and repeatable** — no key, no per-second charge, and the same input gives the same
  frames, so "make the coil slower" is a number to turn rather than a re-roll.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from . import ffmpeg, figure, i18n

# Re-exported so the tool layer has one import for the whole vocabulary.
ANIMATIONS = figure.ANIMATIONS
PARTS = figure.PARTS
CAMERAS = figure.CAMERAS
DEFAULT_FPS = 24
# The longest animation this can draw.
#
# ⚠️ It has to reach **at least** as far as `assemble.MAX_SHOT` (600), because an animation is drawn
# to whatever length its shot ended up needing — `assemble.py:622` says so in as many words ("drawn
# to whatever length the shot ended up needing, so it is never shorter than its slot"). A lower
# ceiling here does not shorten the film; it shortens **the drawing** while the sidecar `.srt`, the
# music ducking, the concat arithmetic and the reported length all still count the longer time. That
# is exactly what a 120 s ceiling under a 600 s ceiling did (measured 2026-09-27): from the affected
# shot onward, every time-coded artefact was out by the same amount, and nothing said so.
#
# The ceiling is not a resource wall: measured the same day, 6 s at 1080x1920 draws in ~1.9 s, so
# 600 s is roughly three minutes of Pillow. A test holds it and `assemble.MAX_SHOT` together.
MAX_SECONDS = 600.0

# What each drawing shows, in the words a model needs to pick between them. Prose lives here rather
# than in `figure` because it is tool-facing text (it appears in a tool description and in the error a
# wrong name produces), and because `figure` must stay drawable without any tool vocabulary in it.
ANIMATION_INFO: dict[str, dict[str, str]] = {
    "blood_flow": {
        "en": "Blood moving along the vessel — the flow keeps going. Use it for \"this artery still "
              "supplies the brain\".",
        "zh": "血液沿血管持续流动——用于「这条动脉仍在供血」。",
    },
    "aneurysm_grow": {
        "en": "A weak spot in the wall bulging out, from a small blister to the sac. Faintly outlines "
              "where the wall was, so a single frame also shows that it grew.",
        "zh": "管壁薄弱处逐渐鼓出,从小突起长成囊状。会画出原来管壁位置的虚线,所以单帧也能看出「它长大了」。",
    },
    "coil_fill": {
        "en": "A coil being wound into the sac: the loops fill it progressively, with the feeding wire "
              "visible between the catheter tip and the leading loop.",
        "zh": "弹簧圈一圈圈填进瘤腔:圈数随时间增加,导管尖端与正在推送的那一圈之间能看到导丝。",
    },
    "catheter_advance": {
        "en": "The microcatheter travelling along the lumen and turning into the sac, with blood still "
              "flowing past it.",
        "zh": "微导管沿血管腔前进、再拐进瘤腔,同时血流仍从旁边通过。",
    },
    "contrast_fill": {
        "en": "Contrast running up the vessel and opacifying the sac — the angiogram finding, as motion.",
        "zh": "造影剂沿血管上行、瘤腔逐渐显影——把「造影看到什么」做成运动。",
    },
}


def describe(lang: str = "") -> str:
    """The vocabulary as prose — for a tool description and for the error a wrong name produces."""
    lang = lang or i18n.current()
    return "\n".join(f"- {k}: " + (ANIMATION_INFO[k]["zh"] if lang == "zh" else ANIMATION_INFO[k]["en"])
                     for k in ANIMATIONS)


def spec_of(raw: Any) -> dict:
    """Validate one shot's `anim` value: a name, or an object with `kind` plus parameters.

    Raises with the whole vocabulary in the message, because the reader is usually a model that has to
    pick one, and a list is the only useful kind of error for that. Older names (`aneurysm_coiling`…)
    are accepted, so a storyboard written before the drawings could move keeps working.
    """
    if isinstance(raw, str):
        raw = {"kind": raw}
    if not isinstance(raw, dict):
        raise figure.FigureError(i18n.pick_now(
            f"`anim` is either the name of an animation or an object with a `kind`. It can be one of: "
            f"{', '.join(ANIMATIONS)}.",
            f"`anim` 要么是动画的名字,要么是一个带 `kind` 的对象。可用:{', '.join(ANIMATIONS)}。"))
    nested = dict(raw.get("params") or {})
    kind = figure.canonical(str(raw.get("kind") or raw.get("type") or ""))
    params: dict[str, Any] = dict(nested)
    for key in ("camera", "particles", "coil_turns", "sac_at_x", "sac_r", "show", "labels", "still_at"):
        if key in raw:
            params[key] = raw[key]
    # Validated here as well as in the drawer: a storyboard is checked before anything is drawn, so a
    # wrong camera name comes back as a sentence about the shot rather than half an hour later.
    params["camera"] = figure.check_camera(params.get("camera") or figure.ANIMATION_CAMERA[kind])
    parts = params.get("show") or []
    if isinstance(parts, str):
        parts = [parts]
    unknown = [str(x) for x in parts if str(x).strip().lower() not in figure.PARTS]
    if unknown:
        raise figure.FigureError(i18n.pick_now(
            f"`show` cannot contain {', '.join(unknown)} — the parts are: {', '.join(figure.PARTS)}.",
            f"`show` 里不能有 {', '.join(unknown)} —— 可用的是:{', '.join(figure.PARTS)}。"))
    params["show"] = [str(x).strip().lower() for x in parts]
    params["labels"] = [x for x in (params.get("labels") or []) if isinstance(x, dict)]
    return {"kind": kind, "params": params}


async def render(out: Path, spec: dict, *, size: tuple[int, int], fps: int = DEFAULT_FPS,
                 seconds: float = 4.0, heading: str = "", caption: str = "", credit: str = "",
                 still: Path | None = None, timeout: float = 900.0) -> dict:
    """Draw the animation as a real clip — `seconds` of motion at `fps` — plus a poster still.

    The still is what the review step of the workflow is built on, so it is always written: a frame
    can be looked at and changed, a rendered clip can only be thrown away.
    """
    # ⚠️ Argument first, resources second: "that length cannot be drawn" is answerable on any machine,
    # and checking for ffmpeg first let a missing binary mask a request that could never be honoured.
    seconds = float(seconds)
    if seconds > MAX_SECONDS:
        # ⚠️ **Refused, not clamped.** Clamping is what produced the bug this ceiling exists to
        # describe: the caller carried on believing the shot was its original length, and every
        # artefact timed against it was wrong from there on. A runtime that cannot honour a request
        # has to say so — silently returning something shorter is the one outcome nobody can detect.
        raise figure.FigureError(i18n.pick_now(
            f"That animation would run {seconds:g}s; the longest drawable here is {MAX_SECONDS:g}s. "
            f"Split it into two shots.",
            f"这段动画要 {seconds:g} 秒,而这里最长能画 {MAX_SECONDS:g} 秒。请拆成两个镜头。"))
    seconds = max(0.5, seconds)
    ff, why = ffmpeg.available()
    if not ff:
        raise figure.FigureError(why)
    spec = spec_of(spec) if "kind" not in spec else spec
    kind = figure.canonical(spec["kind"])
    raw_params = dict(spec.get("params") or {})
    params = {**raw_params, "camera": str(raw_params.get("camera") or figure.ANIMATION_CAMERA[kind])}
    labels = [x for x in (params.get("labels") or []) if isinstance(x, dict)]
    frames = max(2, int(round(seconds * fps)))
    at = figure.drawer(kind, size, params, labels)

    def plate_at(t: float):
        return figure.plate(size, heading=heading, caption=caption, credit=credit, draw=at(t))

    if still is not None:
        still.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(plate_at(float(params.get("still_at") or figure.STILL_AT)).save,
                                str(still))

    done = 0

    def next_frame() -> bytes | None:
        """One raw RGB frame, drawn in a worker thread (see `ffmpeg.pipe`)."""
        nonlocal done
        if done >= frames:
            return None
        img = plate_at(done / (frames - 1))
        done += 1
        return img.tobytes()

    w, h = size
    cmd = [ffmpeg.path(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           *ffmpeg.encoder(), "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    code, err = await ffmpeg.pipe(cmd, next_frame, timeout)
    if code != 0 or not Path(out).is_file():
        raise figure.FigureError(i18n.pick_now(
            f"Drawing the animation failed: {err[-300:] or code}",
            f"画动画失败:{err[-300:] or code}"))
    return {"path": str(out), "name": Path(out).name, "kind": kind,
            "seconds": round(done / fps, 2), "frames": done, "fps": fps,
            "still": str(still) if still else "", "size": f"{w}x{h}"}
