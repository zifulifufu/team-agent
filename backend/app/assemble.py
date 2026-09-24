"""Turning what a group already has into one film somebody can actually watch.

Three things stand between "the group agreed on a script" and "there is a video":

  * a generated clip is 4-30 seconds while a film is minutes, so several have to become one;
  * nobody recorded the narration;
  * the words are not on screen.

None of those is generation. They are all **assembly** — deterministic, local, and free per run —
so they belong in one tool that every member already has, rather than in a set of instructions each
member would otherwise improvise (twelve different `ffmpeg` command lines, each with its own bugs).

What this module may rely on was measured on the machine, not assumed:

  * `ffmpeg` is present (Homebrew, via `bindirs`), and this build has **no `subtitles`, `ass` or
    `drawtext` filter** — it is compiled without libass and without freetype. Burning text through
    ffmpeg is therefore impossible here whatever the tutorials say, so every line is drawn to a
    transparent PNG with Pillow and laid over the picture with `overlay`, which this build does
    have. That also buys wrapping, a per-language font, and a drop shadow for free.
  * The same build **does** have `h264_videotoolbox`, so encoding is hardware accelerated.
  * macOS `say` speaks Chinese and English offline: `Tingting` for 普通话, `Meijia`/`Sinji` for
    繁體, and the English voices for the rest. It is a preview voice rather than a studio one, and
    the result says so instead of letting a group present it as a finished dub.

The shape of a run is one ffmpeg call per shot and then a concat, rather than one enormous filter
graph: a shot that fails can be re-run on its own, the intermediate files are inspectable, and the
final step is a stream copy when every part came out identical (verified afterwards, with a
re-encode as the fallback if the result does not add up).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import i18n
from .bindirs import tool as _tool
from .coderun import inside, kill_group

SUBDIR = "video"            # where the finished film goes, beside the generated clips
SCRATCH = ".assemble"       # intermediates; the leading dot keeps them out of every scan
MAX_SHOTS = 60
DEFAULT_FPS = 24
FITS = ("blur", "cover", "contain")
# `blur` by default, and this was decided by looking at the group's own footage: its clips are 16:9
# while the film is 9:16, and `cover` would throw away two thirds of every frame — on an anatomical
# animation, the part with the anatomy in it. Blurring the same picture behind a contained copy
# loses nothing and reads as deliberate rather than broken.
DEFAULT_FIT = "blur"
STILL_SECONDS = 4.0         # how long a picture holds when nobody said
MIN_SHOT = 1.0
MAX_SHOT = 600.0
# 9:16 and 1:1 first: this is for the vertical short-video platforms the group is usually making
# for, and a landscape film is the exception rather than the default.
SIZES = ("1080x1920", "720x1280", "1080x1080", "1920x1080", "1280x720")
# Tried in order; the first that Pillow can open wins. PingFang is not on this machine and
# Hiragino Sans GB is, which is exactly why the list is walked rather than assumed.
FONTS = ("/System/Library/Fonts/PingFang.ttc",
         "/System/Library/Fonts/Hiragino Sans GB.ttc",
         "/System/Library/Fonts/STHeiti Medium.ttc",
         "/System/Library/Fonts/Supplemental/Songti.ttc",
         "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
         "/System/Library/Fonts/Helvetica.ttc")
# Real Chinese voices first: the `Eddy (Chinese …)` family are the novelty voices, and a group
# that asked for a medical explainer does not want one of those reading it out.
VOICE_ZH = ("Tingting", "Meijia", "Sinji")
VOICE_EN = ("Samantha", "Alex", "Daniel", "Karen", "Moira")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")   # i18n-keep: script detection


class AssembleError(Exception):
    """Something the user can act on: a missing tool, a file that is not there, a bad shot."""


# --------------------------------------------------------------------- the tools themselves
def ffmpeg_path() -> str:
    return _tool("ffmpeg") or ""


def ffprobe_path() -> str:
    """`ffprobe`, next to whichever `ffmpeg` was found.

    A user who has one almost always has the other, and a Homebrew install puts them side by side —
    so looking next to `ffmpeg` first is more reliable than the PATH, and it is what makes a
    hand-built ffmpeg (a folder dropped somewhere) work as well.
    """
    found = _tool("ffprobe")
    if found:
        return found
    ff = ffmpeg_path()
    if ff:
        beside = Path(ff).with_name("ffprobe")
        if beside.is_file():
            return str(beside)
    return ""


def available() -> tuple[str, str]:
    """(why it can be used, why it cannot). Exactly one of the two is non-empty."""
    ff = ffmpeg_path()
    if not ff:
        return "", i18n.pick_now(
            "Assembling a video needs ffmpeg on this machine, and there is none. Install it "
            "(`brew install ffmpeg`) and the tool appears by itself — nothing else is needed: the "
            "narration comes from the system's own speech, and the subtitles are drawn here.",
            "把片段拼成一条成片需要本机有 ffmpeg,现在没有。装一个(`brew install ffmpeg`),这个工具就会"
            "自己出现——不需要别的东西:旁白用系统自带的语音,字幕在这里画。")
    if not ffprobe_path():
        return "", i18n.pick_now(
            "ffmpeg is here but ffprobe is not, and without it the length of nothing can be "
            "measured. They are installed together: `brew install ffmpeg`.",
            "有 ffmpeg 但没有 ffprobe,没有它什么都量不出时长。两者是一起装的:`brew install ffmpeg`。")
    return ff, ""


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a short command (ffprobe, say) and return (code, combined output)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout:g}s"
    except OSError as e:
        return -1, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def duration_of(path: Path) -> float:
    """How long a media file is, whatever kind it is. 0.0 for a still picture, which is what makes
    a PNG usable as a shot with a length of its own choosing."""
    try:
        return float(probe(path)["seconds"])
    except AssembleError:
        # An audio file has no video stream, so `probe` refuses it on purpose; ask about the
        # container instead. Narration needs its length and nothing else.
        fp = ffprobe_path()
        if not fp:
            return 0.0
        code, out = _run([fp, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], 60)
        if code != 0:
            return 0.0
        try:
            return max(0.0, float(out.strip().splitlines()[0]))
        except (ValueError, IndexError):
            return 0.0


def probe(path: Path) -> dict:
    """What a file is: `{"kind", "seconds", "width", "height", "audio"}`.

    `seconds` is 0.0 for a still picture and for anything unreadable — the caller treats 0 as "no
    length of its own" rather than as an error, because that is exactly how a PNG behaves.
    """
    fp = ffprobe_path()
    if not fp:
        raise AssembleError(i18n.pick_now("ffprobe is missing, so the length cannot be measured.",
                                          "缺少 ffprobe,量不出时长。"))
    code, out = _run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                      str(path)], 60)
    if code != 0:
        raise AssembleError(i18n.pick_now(f"Could not read \"{path.name}\": {out[:200]}",
                                          f"读不出「{path.name}」:{out[:200]}"))
    try:
        data = json.loads(out)
    except ValueError:
        raise AssembleError(i18n.pick_now(f"Could not read \"{path.name}\".",
                                          f"读不出「{path.name}」。")) from None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        seconds = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not video:
        raise AssembleError(i18n.pick_now(
            f"\"{path.name}\" holds no picture, so it cannot be part of the film.",
            f"「{path.name}」里没有画面,不能放进成片。"))
    # A still picture is reported by ffprobe as a stream with no duration; `image2` also says so
    # through the codec. `seconds == 0` after this is the app's own signal for "a picture".
    return {"kind": "image" if seconds <= 0 or video.get("codec_name") in ("png", "mjpeg", "bmp",
                                                                           "gif", "webp", "tiff")
            else "video",
            "seconds": seconds,
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "audio": audio}


# --------------------------------------------------------------------- narration
_voices_cache: list[dict] | None = None


def voices() -> list[dict]:
    """Every voice this machine has, as `{"name", "locale"}`. Measured once per process.

    Parsed from the tool rather than hardcoded, because which voices exist depends on what the
    user downloaded in System Settings: a machine with no Chinese voice installed must get a
    straight answer, not a command that fails with "voice not found".
    """
    global _voices_cache
    if _voices_cache is not None:
        return _voices_cache
    out: list[dict] = []
    code, text = _run(["say", "-v", "?"], 20)
    if code == 0:
        for line in text.splitlines():
            head = line.split("#", 1)[0].strip()
            if not head:
                continue
            bits = head.rsplit(None, 1)
            if len(bits) == 2 and re.match(r"^[a-z]{2}[_-][A-Za-z]{2,}", bits[1]):
                out.append({"name": bits[0].strip(), "locale": bits[1].strip()})
    _voices_cache = out
    return out


def pick_voice(text: str, want: str = "") -> str:
    """The voice to narrate this line with: the user's choice, else by the language on screen.

    An explicit name wins even if it is not in the list — the user may know a voice this listing
    does not show, and refusing it would be this program overruling them about their own machine.
    """
    want = (want or "").strip()
    if want:
        return want
    known = voices()
    if not known:
        raise AssembleError(i18n.pick_now(
            "This machine has no speech voices available to `say`, so the narration cannot be "
            "recorded. Add one under System Settings → Accessibility → Spoken Content.",
            "本机的 `say` 没有可用的语音,所以配不了旁白。可在「系统设置 → 辅助功能 → 朗读内容」里添加。"))
    if CJK_RE.search(text or ""):
        for name in VOICE_ZH:
            if any(v["name"] == name for v in known):
                return name
        for v in known:
            if v["locale"].lower().startswith("zh"):
                return v["name"]
    else:
        for name in VOICE_EN:
            if any(v["name"] == name for v in known):
                return name
        for v in known:
            if v["locale"].lower().startswith("en"):
                return v["name"]
    return known[0]["name"]


async def speak(text: str, out: Path, voice: str, timeout: float) -> float:
    """Record one line of narration locally, and return its real length in seconds.

    `say` rather than a cloud service: it costs nothing, needs no key, works offline, and — the
    part that matters for assembly — it is the *only* route that lets the shot be timed before
    anything is rendered. The upside of a cloud voice is a nicer read; the cost is a dependency,
    a bill and a reason the whole film stops when the network does.
    """
    tmp = out.with_suffix(".txt")
    tmp.write_text(text, encoding="utf-8")
    code, detail = await asyncio.to_thread(
        lambda: _run(["say", "-v", voice, "-f", str(tmp), "-o", str(out)], timeout))
    if code != 0 or not out.is_file():
        raise AssembleError(i18n.pick_now(
            f"The narration could not be recorded with the voice \"{voice}\": {detail[:200]}",
            f"用语音「{voice}」配旁白失败:{detail[:200]}"))
    info = await asyncio.to_thread(duration_of, out)
    return max(info, 0.1)


def narration_seconds(text: str, voice: str = "") -> float:
    """A rough length for a line of narration, used only to plan the timing before anything is
    recorded. The recording's real length replaces it the moment it exists."""
    if CJK_RE.search(text or ""):
        n = len(CJK_RE.findall(text or ""))
        return max(1.0, round(n / 4.5, 2))
    n = len(re.findall(r"\w+", text or ""))
    return max(1.0, round(n / 2.6, 2))


# --------------------------------------------------------------------- words on screen
def _font(size: int):
    from PIL import ImageFont
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _wrap(text: str, font, limit: int, draw) -> list[str]:
    """Break a line to fit `limit` pixels. Character-by-character, because Chinese has no spaces
    and a word-based wrap would put a whole sentence on one line."""
    lines: list[str] = []
    for para in (text or "").split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=font) <= limit:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        lines.append(cur)
    return [ln for ln in lines if ln.strip()] or [""]


def _shadow(draw, xy, text, font, fill, anchor="mm"):
    """Text with a soft drop shadow. A plain white line over a bright operating-theatre shot is
    unreadable, and this is the one line the group will see the most."""
    x, y = xy
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1)):
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 180), anchor=anchor)
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def subtitle_png(text: str, size: tuple[int, int], out: Path, *, font_scale: float = 0.045,
                 box: str = "#000000", alpha: int = 130) -> Path:
    """One transparent full-frame PNG holding one line of subtitle.

    Full-frame on purpose: the `overlay` filter is then a no-op placement, and the position is
    decided here where the text is measured rather than by an expression in a filter graph.

    The block sits at 86% of the height rather than at the very bottom, and that number came out of
    looking at a real assembled frame: a generated clip often carries its own caption along the
    bottom edge, and every vertical platform covers roughly the last tenth with its own UI. At 90%
    the two overlapped; at 86% neither does.
    """
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(max(20, int(h * font_scale)))
    lines = _wrap(text, font, int(w * 0.86), draw)
    line_h = int(max(1, draw.textlength("国", font=font)) * 1.45)
    block = line_h * len(lines)
    bottom = int(h * 0.86)                       # see below: clear of two things that live lower
    top = bottom - block
    pad = int(line_h * 0.35)
    if box and COLOR_RE.match(box) and alpha > 0:
        rgb = tuple(int(box[i:i + 2], 16) for i in (1, 3, 5))
        # One rounded plate behind the whole block, not behind each line: a stack of separate
        # plates reads as three unrelated labels.
        draw.rounded_rectangle([int(w * 0.05), top - pad, int(w * 0.95), bottom + pad],
                               radius=int(line_h * 0.25), fill=(*rgb, alpha))
    for i, line in enumerate(lines):
        _shadow(draw, (w // 2, top + i * line_h + line_h // 2), line, font, (255, 255, 255, 255))
    img.save(out)
    return out


def card_png(title: str, subtitle: str, size: tuple[int, int], out: Path, *,
             background: str = "#0E1B2A", accent: str = "#2E7CF6") -> Path:
    """A title card or a closing card, drawn here so a film can open and close without spending a
    generation on it (and so the text is spelled the way the group agreed, not the way a model
    felt about it)."""
    from PIL import Image, ImageDraw
    w, h = size
    rgb = tuple(int(background[i:i + 2], 16) for i in (1, 3, 5)) if COLOR_RE.match(background) \
        else (14, 27, 42)
    img = Image.new("RGB", (w, h), rgb)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, int(h * 0.42), w, int(h * 0.42) + max(2, h // 400)], fill=accent)
    big = _font(int(h * 0.062))
    small = _font(int(h * 0.032))
    lines = _wrap(title, big, int(w * 0.82), draw)
    top = int(h * 0.42) - int(h * 0.075) * len(lines)
    draw.text((w // 2, top), "\n".join(lines), font=big, fill=(255, 255, 255), anchor="ma",
              align="center")
    if subtitle:
        draw.text((w // 2, int(h * 0.47)), "\n".join(_wrap(subtitle, small, int(w * 0.8), draw)),
                  font=small, fill=(190, 205, 225), anchor="ma", align="center")
    img.save(out)
    return out


# --------------------------------------------------------------------- the plan
def parse_size(value: str, fallback: str = "1080x1920") -> tuple[int, int, str]:
    """A WxH string, checked. Even numbers only: h264 refuses an odd dimension."""
    raw = (value or "").strip().lower().replace(" ", "")
    if not raw:
        raw = fallback
    m = re.match(r"^(\d{2,5})x(\d{2,5})$", raw)
    if not m:
        raise AssembleError(i18n.pick_now(
            f"\"{value}\" is not a size. Write it as 1080x1920.",
            f"「{value}」不是画幅。请写成 1080x1920 这样。"))
    w, h = int(m.group(1)), int(m.group(2))
    if not (240 <= w <= 4096 and 240 <= h <= 4096):
        raise AssembleError(i18n.pick_now("A side of the picture has to be between 240 and 4096.",
                                          "画面边长要在 240 到 4096 之间。"))
    return w - (w % 2), h - (h % 2), f"{w - (w % 2)}x{h - (h % 2)}"


def _local(workspace: Path, value: str, what: str) -> Path:
    """Turn a shot's path into a real file inside the workspace, or explain why not.

    Absolute paths are refused rather than reinterpreted, and the resolved file is re-checked for
    containment: a member can plant a symlink inside its own workspace, and this is the same rule
    every other reader in the app follows.
    """
    rel = (value or "").strip()
    if not rel:
        raise AssembleError(what)
    if rel.startswith("/") or rel.startswith("~"):
        raise AssembleError(i18n.pick_now(
            f"Give \"{rel}\" as a path inside the group's workspace, not an absolute one.",
            f"「{rel}」要写成工作目录内的相对路径,不能用绝对路径。"))
    path = (workspace / rel).resolve()
    if not inside(workspace, path) or not path.is_file():
        raise AssembleError(i18n.pick_now(
            f"There is no file at \"{rel}\" inside the workspace.",
            f"工作目录里没有「{rel}」这个文件。"))
    return path


def make_plan(shots: Any, *, workspace: Path, total: float = 0.0,
              voice: str = "", still: float = STILL_SECONDS,
              fit: str = DEFAULT_FIT) -> list[dict]:
    """Validate the shot list and decide how long each shot runs.

    Length is decided in this order, and the reason is printed beside it in the shot sheet:

      1. what the shot asks for (`seconds`);
      2. otherwise, what the narration needs — a line of commentary chopped off mid-sentence is the
         single most obvious way an assembled film looks broken, so the voice sets the pace;
      3. otherwise, the clip's own length, or the default hold for a picture.
    """
    if not isinstance(shots, list) or not shots:
        raise AssembleError(i18n.pick_now(
            "A film needs a shot list: one entry per shot, in order. Each entry is {\"clip\": "
            "\"video/x.mp4\", \"seconds\": 6, \"say\": \"narration\", \"text\": \"on-screen words\"} "
            "— or {\"title\": \"...\", \"subtitle\": \"...\"} for a card with no footage.",
            "成片需要一份分镜表:按顺序每个镜头一条。每条形如 {\"clip\": \"video/x.mp4\", "
            "\"seconds\": 6, \"say\": \"旁白\", \"text\": \"屏幕字幕\"};没有素材的卡片写 "
            "{\"title\": \"...\", \"subtitle\": \"...\"}。"))
    if len(shots) > MAX_SHOTS:
        raise AssembleError(i18n.pick_now(
            f"That is {len(shots)} shots; one film takes at most {MAX_SHOTS}. Split it into parts "
            "and assemble them one after another.",
            f"这是 {len(shots)} 个镜头,一条成片最多 {MAX_SHOTS} 个。请分成几段,依次装配。"))
    out: list[dict] = []
    for i, raw in enumerate(shots, 1):
        if not isinstance(raw, dict):
            raise AssembleError(i18n.pick_now(f"Shot {i} is not an object.", f"第 {i} 个镜头不是一条记录。"))
        say = str(raw.get("say") or "").strip()
        text = str(raw.get("text") or "").strip() or say
        title = str(raw.get("title") or "").strip()
        clip = str(raw.get("clip") or raw.get("source") or "").strip()
        if not clip and not title:
            raise AssembleError(i18n.pick_now(
                f"Shot {i} has neither a clip nor a title — it would be a blank frame.",
                f"第 {i} 个镜头既没有素材也没有标题,会是一帧空白。"))
        entry: dict[str, Any] = {"no": i, "say": say, "text": text, "title": title,
                                 "subtitle": str(raw.get("subtitle") or "").strip(),
                                 "audio": str(raw.get("audio") or ("voice" if say else "clip")),
                                 "motion": raw.get("motion", bool(raw.get("title"))),
                                 "source": "", "kind": "card", "own": 0.0}
        want_fit = str(raw.get("fit") or fit or DEFAULT_FIT).strip().lower()
        if want_fit not in FITS:
            raise AssembleError(i18n.pick_now(
                f"Shot {i}: \"{want_fit}\" is not a way to fit a picture into the frame. Use one of: "
                f"{', '.join(FITS)}.",
                f"第 {i} 个镜头:「{want_fit}」不是把画面装进画幅的方式。可用:{', '.join(FITS)}。"))
        entry["fit"] = want_fit
        if entry["audio"] not in ("voice", "clip", "mix", "silent"):
            entry["audio"] = "voice" if say else "clip"
        if not say and entry["audio"] == "voice":
            entry["audio"] = "clip"
        if clip:
            path = _local(workspace, clip, i18n.pick_now(
                f"Shot {i} has no clip.", f"第 {i} 个镜头没有素材。"))
            info = probe(path)
            entry.update(source=str(path), kind=info["kind"], own=info["seconds"],
                         has_audio=info["audio"])
        # Narration first, because it may be what decides the length.
        want = raw.get("seconds")
        try:
            asked = float(want) if want not in (None, "") else 0.0
        except (TypeError, ValueError):
            raise AssembleError(i18n.pick_now(
                f"Shot {i}: \"{want}\" is not a number of seconds.",
                f"第 {i} 个镜头:「{want}」不是秒数。")) from None
        needs = narration_seconds(say, voice) if say else 0.0
        if asked > 0:
            entry["seconds"], entry["why"] = asked, "asked"
        elif needs:
            entry["seconds"], entry["why"] = needs, "narration"
        elif entry["kind"] == "video" and entry["own"] > 0:
            entry["seconds"], entry["why"] = entry["own"], "source"
        else:
            entry["seconds"], entry["why"] = still, "default"
        if entry["seconds"] < MIN_SHOT or entry["seconds"] > MAX_SHOT:
            raise AssembleError(i18n.pick_now(
                f"Shot {i} asks for {entry['seconds']:g}s; a shot runs between {MIN_SHOT:g} and "
                f"{MAX_SHOT:g} seconds.",
                f"第 {i} 个镜头要 {entry['seconds']:g} 秒;单个镜头在 {MIN_SHOT:g} 到 {MAX_SHOT:g} 秒之间。"))
        entry["seconds"] = round(entry["seconds"], 2)
        out.append(entry)

    # A target total is a real requirement (the platform, the slot, the brief), so make it, when
    # there is somewhere to put the slack — and say exactly what was stretched when there was.
    if total > 0:
        natural = sum(e["seconds"] for e in out)
        gap = round(total - natural, 2)
        if gap > 0.5:
            holders = [e for e in out if e["kind"] in ("card", "image")]
            if holders:
                extra = gap / len(holders)
                for e in holders:
                    e["seconds"] = round(e["seconds"] + extra, 2)
                out[0]["stretched"] = round(extra, 2)
            else:
                out[0]["short"] = gap
        elif gap < -0.5:
            out[0]["over"] = round(-gap, 2)
    return out


def _timecode(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_of(plan: list[dict]) -> str:
    """The subtitle file, built from the same plan that timed the film — so the sidecar and the
    burned-in text can never disagree about when a line appears."""
    out: list[str] = []
    t = 0.0
    n = 0
    for e in plan:
        if e["text"]:
            n += 1
            out.append(f"{n}\n{_timecode(t)} --> {_timecode(t + e['seconds'])}\n{e['text']}\n")
        t += e["seconds"]
    return "\n".join(out)


# --------------------------------------------------------------------- rendering
async def _ffmpeg(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run ffmpeg as its own process group, so a timeout kills the encoder and not just the
    wrapper (the same reason `coderun` does it)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True)
    pgid = proc.pid
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        kill_group(pgid)
        await proc.wait()
        raise AssembleError(i18n.pick_now(
            f"Rendering did not finish within {int(timeout)}s and was stopped. A film takes roughly "
            "a second of encoding per second of footage; a longer budget can be set under "
            "Permissions & control.",
            f"渲染超过 {int(timeout)} 秒,已停止。大致上一秒素材要一秒编码;可以在「权限与控制」里把预算调长。"))
    finally:
        kill_group(pgid)
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _encoder() -> list[str]:
    """VideoToolbox when this build has it, libx264 otherwise — decided by asking, not by hoping.

    The hardware encoder is several times faster and matters here because a three-minute vertical
    film is 4300 frames; a build without it still works, just slower.
    """
    ff = ffmpeg_path()
    code, out = _run([ff, "-hide_banner", "-encoders"], 20)
    if code == 0 and "h264_videotoolbox" in out:
        return ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]


def _fit_chain(fit: str, w: int, h: int) -> str:
    """One graph fragment that ends in `[vbase]`, putting a source of any shape into the frame.

    `cover` fills the frame by cropping; `contain` keeps all of it with black bars; `blur` keeps all
    of it and fills the rest with a blurred copy of itself. The last one is the default because the
    groups this is used by film landscape material for vertical platforms, and cropping two thirds of
    an anatomical animation away is the wrong kind of quiet.
    """
    cover = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if fit == "cover":
        return f"[0:v]{cover}[vbase]"
    if fit == "contain":
        return f"[0:v]{contain_chain(w, h)}[vbase]"
    # `blur`: the contained copy over the blurred one — and the copy is deliberately **not** padded
    # here. `overlay` centres it by itself, while a padded copy is opaque black everywhere the
    # picture is not, so it would cover the blurred background completely: the film came out with
    # plain black bars and the blur never appeared at all. (The pixel test in the suite is what
    # caught it; a screenshot at a glance reads as "letterboxed", which looks plausible.)
    return ";".join([
        "[0:v]split=2[bgsrc][fgsrc]",
        f"[bgsrc]{cover},boxblur=luma_radius=24:luma_power=2,eq=brightness=-0.12[bg]",
        f"[fgsrc]{scale_to(w, h)}[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[vbase]",
    ])


def scale_to(w: int, h: int) -> str:
    """Scale a source down until it fits the frame, keeping its shape."""
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease"


def contain_chain(w: int, h: int) -> str:
    return scale_to(w, h) + f",pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"


async def _render_shot(entry: dict, plan_dir: Path, size: tuple[int, int], fps: int,
                       voice: str, burn: bool, timeout: float) -> tuple[Path, list[str]]:
    """One shot, as a file with exactly the length the plan decided.

    Normalising here (same size, same frame rate, always an audio track) is what makes the concat
    at the end a stream copy instead of a second full encode.
    """
    w, h = size
    notes: list[str] = []
    idx = entry["no"]
    shot = plan_dir / f"s{idx:03d}.mp4"
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]

    # ---- the picture, fitted into the frame. Written as whole graph fragments (each ending in
    # `[vbase]`) rather than one filter chain, because two of the three ways to fit need more than
    # one chain and the blur needs to fork the input.
    pre: list[str] = []
    post: list[str] = []
    if entry["kind"] == "card":
        png = plan_dir / f"s{idx:03d}-card.png"
        await asyncio.to_thread(card_png, entry["title"], entry["subtitle"], size, png)
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{entry['seconds']:g}", "-i", str(png)]
        pre.append("[0:v]null[vbase]")          # a card is drawn at the frame size already
    elif entry["kind"] == "image":
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{entry['seconds'] + 1:g}",
                "-i", entry["source"]]
        pre.append(_fit_chain(entry["fit"], w, h))
    else:
        cmd += ["-i", entry["source"]]
        pre.append(_fit_chain(entry["fit"], w, h))
        if entry["own"] + 0.05 < entry["seconds"]:
            # A clip shorter than its slot holds its last frame rather than cutting the narration
            # off — the reason a shot's length follows the voice in the first place.
            post.append(f"tpad=stop_mode=clone:stop_duration={entry['seconds'] - entry['own'] + 0.5:g}")
    post += ["setsar=1", f"fps={fps}"]
    if entry["kind"] == "image" and entry.get("motion"):
        # A slow push on a still: without it a slide reads as a mistake, with it as a shot. After the
        # fitting rather than before, so it pushes the whole composition and not just the inset.
        post.append(f"zoompan=z='min(1+0.00035*on,1.12)':x='iw/2-(iw/zoom/2)'"
                    f":y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps}")

    # ---- the narration is recorded first: its real length can change how long this shot is, and
    # everything below (the silence pad, the audio graph) is sized from that decision.
    audio_in: Path | None = None
    speaking = bool(entry["say"]) and entry["audio"] in ("voice", "mix")
    if speaking:
        audio_in = plan_dir / f"s{idx:03d}-voice.aiff"
        entry["voice"] = pick_voice(entry["say"], voice)
        entry["voice_seconds"] = await speak(entry["say"], audio_in, entry["voice"],
                                             min(120.0, timeout))
        if entry["voice_seconds"] > entry["seconds"] + 0.15:
            # A line of narration chopped off mid-sentence is the most obvious way an assembled
            # film looks broken, so the narration always wins over the length that was asked for —
            # including over an explicit `seconds`. It is said out loud rather than done quietly,
            # because the total the group planned has just changed.
            was = entry["seconds"]
            entry["seconds"] = round(entry["voice_seconds"] + 0.35, 2)
            notes.append(i18n.pick_now(
                f"Shot {idx} was lengthened from {was:g}s to {entry['seconds']:g}s: its narration "
                f"needs {entry['voice_seconds']:.1f}s, and cutting it off mid-sentence is worse "
                "than running long.",
                f"第 {idx} 个镜头从 {was:g} 秒延长到 {entry['seconds']:g} 秒:这段旁白要 "
                f"{entry['voice_seconds']:.1f} 秒,把话截断比超时长更糟。"))

    # ---- the remaining inputs, counted as they are added so the graph can name them by index
    n_in = 1
    sub_index: int | None = None
    if burn and entry["text"]:
        sub = plan_dir / f"s{idx:03d}-sub.png"
        await asyncio.to_thread(subtitle_png, entry["text"], size, sub)
        sub_index, n_in = n_in, n_in + 1
        cmd += ["-i", str(sub)]
    voice_index: int | None = None
    if speaking and audio_in:
        voice_index, n_in = n_in, n_in + 1
        cmd += ["-i", str(audio_in)]
    source_audio = entry["kind"] == "video" and bool(entry.get("has_audio"))
    keep_clip = entry["audio"] in ("clip", "mix") and source_audio
    silence_index: int | None = None
    if voice_index is None and not keep_clip:
        # Every shot gets an audio track even when it has nothing to say, because the concat at the
        # end is a stream copy: one shot without a track would make every later one silent.
        silence_index, n_in = n_in, n_in + 1
        cmd += ["-f", "lavfi", "-t", f"{entry['seconds']:g}", "-i", "anullsrc=r=44100:cl=stereo"]

    # ---- the graph
    dur = f"{entry['seconds']:g}"
    graph = list(pre)
    # `post` always has at least `setsar` and `fps`, so this fragment always exists.
    graph.append(f"[vbase]{','.join(post)}[vshot]")
    graph.append(f"[vshot][{sub_index}:v]overlay=0:0[vout]" if sub_index is not None
                 else "[vshot]null[vout]")
    if voice_index is not None and entry["audio"] == "mix" and source_audio:
        # Weights rather than amix's own averaging: the voice has to stay intelligible with the
        # clip's sound underneath it, and "half of each" is not a level, it is a guess.
        graph.append(f"[{voice_index}:a][0:a]amix=inputs=2:duration=first:weights=1 0.35[amix]")
        voice_tag = "[amix]"
    elif voice_index is not None:
        voice_tag = f"[{voice_index}:a]"
    elif keep_clip:
        voice_tag = "[0:a]"
    else:
        voice_tag = f"[{silence_index}:a]"
    graph.append(f"{voice_tag}apad=whole_dur={dur}[aout]")

    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
            "-t", dur]
    cmd += _encoder() + ["-pix_fmt", "yuv420p", "-r", str(fps),
                         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                         "-video_track_timescale", "90000", "-shortest", str(shot)]
    code, out = await _ffmpeg(cmd, timeout)
    if code != 0 or not shot.is_file():
        raise AssembleError(i18n.pick_now(
            f"Shot {idx} failed to render: {out.strip()[-300:]}",
            f"第 {idx} 个镜头渲染失败:{out.strip()[-300:]}"))
    if entry["kind"] != "card" and entry.get("own"):
        entry["used"] = round(entry["own"], 2)
    return shot, notes


async def render(workspace: Path, shots: Any, *, size: str = "", total: float = 0.0,
                 fps: int = DEFAULT_FPS, voice: str = "", burn: bool = True, name: str = "",
                 timeout: float = 1800.0, still: float = STILL_SECONDS,
                 fit: str = DEFAULT_FIT) -> dict:
    """Assemble the shot list into one film. Returns what was made, where, and what was noted."""
    ff, why = available()
    if not ff:
        raise AssembleError(why)
    workspace = Path(workspace).resolve()
    tw, th, label = parse_size(size)
    size_px = (tw, th)
    fps = max(12, min(int(fps or DEFAULT_FPS), 60))

    plan = make_plan(shots, workspace=workspace, total=total, voice=voice, still=still, fit=fit)

    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", (name or f"film-{time.strftime('%Y%m%d-%H%M%S')}"))
    stem = stem.strip("-")[:40] or f"film-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = workspace / SUBDIR
    if out_dir.is_symlink():
        raise AssembleError(i18n.pick_now(
            f"\"{out_dir}\" is a symlink, so nothing was written. A film is kept in a real folder "
            "inside the workspace.",
            f"「{out_dir}」是符号链接,所以没有写。成片要放在工作目录里的真实目录中。"))
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_dir = workspace / SCRATCH / stem
    plan_dir.mkdir(parents=True, exist_ok=True)
    if not inside(workspace, out_dir) or not inside(workspace, plan_dir):
        raise AssembleError(i18n.pick_now("The output folder resolves outside the workspace.",
                                          "输出目录解析后在工作目录之外。"))

    notes: list[str] = []
    started = time.time()
    try:
        parts: list[Path] = []
        for entry in plan:
            shot, extra = await _render_shot(entry, plan_dir, size_px, fps, voice, burn, timeout)
            parts.append(shot)
            notes.extend(extra)
        final = out_dir / f"{stem}.mp4"
        await _concat(parts, final, plan_dir, timeout)
        total_seconds = round(sum(e["seconds"] for e in plan), 2)
        # The sidecar is written from the same plan that timed the film, so it is right even when
        # the concat was a re-encode.
        (out_dir / f"{stem}.srt").write_text(srt_of(plan), encoding="utf-8")
        sheet = _shot_sheet(plan, size_px, fps, final, total_seconds, notes)
        (out_dir / f"{stem}.md").write_text(sheet, encoding="utf-8")
        (out_dir / f"{stem}.json").write_text(
            json.dumps({"size": label, "fps": fps, "seconds": total_seconds, "shots": plan},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except AssembleError:
        raise
    except Exception as e:  # noqa: BLE001 — one film must not leave half a folder unexplained
        raise AssembleError(i18n.pick_now(f"Assembling failed: {type(e).__name__}: {e}",
                                          f"装配失败:{type(e).__name__}: {e}")) from e

    size_bytes = final.stat().st_size if final.is_file() else 0
    return {"name": final.name, "path": str(final), "bytes": size_bytes,
            "seconds": total_seconds, "size": label, "fps": fps, "fit": plan[0]["fit"] if plan else "",
            "shots": len(plan), "narrated": sum(1 for e in plan if e["say"]),
            "subtitled": sum(1 for e in plan if e["text"]), "burned": burn,
            "srt": str(out_dir / f"{stem}.srt"), "sheet": str(out_dir / f"{stem}.md"),
            "plan": plan, "notes": notes, "took": round(time.time() - started, 1)}


async def _concat(parts: list[Path], final: Path, plan_dir: Path, timeout: float) -> None:
    """Join the shots, copying the streams when that works and re-encoding when it does not.

    Copying is tried first because it is nearly free and the parts were all rendered to the same
    shape on purpose. It is then **verified** — a copy can succeed and still produce a file that
    stops early, and a three-minute film that plays for forty seconds is worse than a slow one. So
    the length is measured, and the fallback re-encodes through `concat`.
    """
    listing = plan_dir / "parts.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    want = sum((probe(p)["seconds"] for p in parts), 0.0)
    ff = ffmpeg_path()
    code, out = await _ffmpeg([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                               "-safe", "0", "-i", str(listing), "-c", "copy",
                               "-movflags", "+faststart", str(final)], timeout)
    if code == 0 and final.is_file():
        try:
            if abs(probe(final)["seconds"] - want) <= 1.0:
                return
        except AssembleError:
            pass
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", str(listing), "-filter_complex", "[0:v]setpts=PTS-STARTPTS,format=yuv420p[v]",
           "-map", "[v]", "-map", "0:a", *_encoder(),
           "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
           "-video_track_timescale", "90000", "-movflags", "+faststart", str(final)]
    code, out = await _ffmpeg(cmd, timeout)
    if code != 0 or not final.is_file():
        raise AssembleError(i18n.pick_now(
            f"Joining the shots failed: {(out or '').strip()[-300:]}",
            f"拼接片段失败:{(out or '').strip()[-300:]}"))


def _shot_sheet(plan: list[dict], size: tuple[int, int], fps: int, final: Path,
                total: float, notes: list[str]) -> str:
    """The film as a readable sheet: what each shot is, where it came from, what it says.

    It is the editable artefact of the whole pipeline — change a line here and assemble again,
    instead of going back to the group and asking it to remember what it decided.
    """
    w, h = size
    lines = [i18n.pick_now(f"# {final.stem}", f"# {final.stem}"), ""]
    lines.append(i18n.pick_now(
        f"{len(plan)} shots · {total:g}s · {w}×{h} · {fps}fps · {final.name}",
        f"{len(plan)} 个镜头 · {total:g} 秒 · {w}×{h} · {fps}fps · {final.name}"))
    lines.append("")
    lines.append(i18n.pick_now(
        "| # | source | seconds | narration | on-screen |", "| # | 素材 | 秒 | 旁白 | 字幕 |"))
    lines.append("|---|---|---|---|---|")
    for e in plan:
        src = Path(e["source"]).name if e["source"] else i18n.pick_now("(title card)", "(标题卡)")
        say = (e["say"][:38] + "…") if len(e["say"]) > 39 else e["say"]
        text = (e["text"][:38] + "…") if len(e["text"]) > 39 else e["text"]
        lines.append(f"| {e['no']} | {src} | {e['seconds']:g} | {say} | {text} |")
    if notes:
        lines += ["", i18n.pick_now("## Adjustments", "## 调整")] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"
