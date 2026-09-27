"""Reading someone else's video — its format and the way it is put together — so the group can work
the same way.

The point is *measurement first*. A model asked "what style is this video" answers with adjectives; a
model handed "9 shots in 42 seconds, 4.7s each, 1080x1920, subtitles burned in 8% above the lower
edge, cuts on the beat" is being told something it can actually follow. So this module does the
counting with ffprobe/ffmpeg — length, frame size, frame rate, how many cuts and how far apart, how
much of it is speech — and hands those numbers to the model along with sampled frames. Everything the
model says about *style* then rests on numbers, and the numbers are re-checkable by anyone.

What this deliberately does not do: copy the reference. It reads the form — aspect, rhythm, where the
text sits, how a shot is framed, how the audio is used — and the spec it produces says so out loud.
Re-using somebody's layout and pacing is how film language works; re-using their footage is not, and
the spec has a section for "what has to be made fresh" so the difference is stated rather than left to
whoever runs the next step.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from . import assemble, ffmpeg, i18n

# How much of a reference is read. A tool call is not a film club: the first few minutes carry the
# format, and a two-hour download inside a member's turn would time out on someone else's patience.
MAX_SECONDS = 600.0
# Frames handed to the model. Eight is enough to see a cut, a caption, a colour scheme and a camera
# move twice over, and it keeps the request small enough to send in one go.
FRAMES = 8
# Scene-change threshold. Low enough to catch a hard cut, high enough not to fire on a camera pan.
SCENE = 0.30
# Where a downloaded reference goes, inside the group's workspace, so the whole group can watch it.
REF_DIR = "参考片"
# The 'should not be copied' list travels with the spec — see the module docstring.
RIGHTS_NOTE_EN = ("Reuse the format — aspect, pacing, caption placement, the shape of a shot — and "
                  "make the actual pictures, footage and music fresh for this film.")
RIGHTS_NOTE_ZH = "可以照搬的是版式与节奏(画幅、镜头长度、字幕位置、构图方式);画面素材、音乐必须为本片另做。"


def available() -> tuple[bool, str]:
    """Whether a reference can be read at all. Measured, not assumed."""
    if not shutil.which(ffmpeg.path()) and not Path(ffmpeg.path()).is_file():
        return False, i18n.pick_now("ffmpeg is missing, so a video cannot be measured.",
                                    "缺少 ffmpeg,量不了视频。")
    if not assemble.ffprobe_path():
        return False, i18n.pick_now("ffprobe is missing, so a video cannot be measured.",
                                    "缺少 ffprobe,量不了视频。")
    return True, ""


def downloader() -> str | None:
    """`yt-dlp`, if this machine has it — the only way a platform link becomes a file."""
    return shutil.which("yt-dlp") or shutil.which("ytdlp")


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ------------------------------------------------------------------ getting a file
def download(url: str, dest: Path, *, limit_mb: int = 300, timeout: float = 600) -> tuple[Path, dict]:
    """Fetch a platform video into `dest`. `(file, metadata)`; raises `StudyError` with the tool's own
    last words when it cannot.

    The info JSON is asked for as well as the media, in one call: it carries the title and the uploader,
    which is how the finished spec can say where it was learned from. Attribution is not decoration —
    "study this and do something like it" is a claim about whose work this is.
    """
    exe = downloader()
    if not exe:
        raise StudyError(i18n.pick_now(
            "This machine has no `yt-dlp`, so a link cannot be turned into a file. Install it "
            "(macOS: `brew install yt-dlp`, or `uv tool install yt-dlp` / `pipx install yt-dlp`) and "
            "try again — or hand over a video file instead.",
            "这台机器没有 `yt-dlp`,所以链接变不成文件。装一个再试(macOS:`brew install yt-dlp`,"
            "或 `uv tool install yt-dlp` / `pipx install yt-dlp`),或者直接给一个视频文件。"))
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "source.%(ext)s"
    cmd = [exe, "--no-playlist", "--no-progress", "--write-info-json", "--restrict-filenames",
           "--max-filesize", f"{limit_mb}M", "-o", str(out), url]
    code, text = _run(cmd, timeout)
    found = [p for p in sorted(dest.iterdir()) if p.is_file() and p.suffix.lower() in
             (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".ts", ".mp3", ".m4a", ".wav")]
    if code != 0 or not found:
        raise StudyError(i18n.pick_now(
            f"`yt-dlp` could not fetch that link: {text.strip()[-400:]}",
            f"`yt-dlp` 没能取到那个链接:{text.strip()[-400:]}"))
    media = max(found, key=lambda p: p.stat().st_size)
    meta: dict = {}
    info = dest / "source.info.json"
    if info.is_file():
        try:
            raw = json.loads(info.read_text(encoding="utf-8", errors="replace"))
            meta = {k: raw.get(k) for k in ("title", "uploader", "duration", "webpage_url", "extractor")}
        except ValueError:
            meta = {}
    # One canonical name, and one file: a second study of the same link must not leave the previous
    # container behind. (It did — a run that got `.mkv` where the first got `.mp4` left both, and the
    # finder above picks by size, so a later study could read the older film.) The info JSON is kept:
    # it is metadata, and it is overwritten with each fetch.
    clean = dest / f"source{media.suffix.lower()}"
    for other in dest.glob("source.*"):
        if other != clean and other.is_file() and other.suffix.lower() != ".json":
            other.unlink(missing_ok=True)
    if media != clean:
        media.replace(clean)
        media = clean
    return media, meta


# ------------------------------------------------------------------ measuring it
def measure(path: Path) -> dict:
    """What the file *is*: length, frame size, frame rate, whether it has sound, how loud it is.

    Read from ffprobe and ffmpeg rather than guessed from the picture, because these are the numbers
    the spec's claims rest on ("a cut every 1.8 seconds" has to come from counting cuts).
    """
    info = assemble.probe(path)
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    fps = 0.0
    fp = assemble.ffprobe_path()
    if fp:
        code, out = _run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=r_frame_rate,avg_frame_rate,nb_frames", "-of", "json", str(path)], 60)
        if code == 0:
            try:
                st = (json.loads(out).get("streams") or [{}])[0]
                rate = str(st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1")
                num, _, den = rate.partition("/")
                fps = round(float(num) / float(den or 1), 2) if float(den or 1) else 0.0
            except (ValueError, TypeError):
                fps = 0.0
    loud = {"mean_db": None, "max_db": None}
    if info.get("audio"):
        # Volume tells apart the three ways a short film handles sound: narration, music bed, silence.
        code, out = _run([ffmpeg.path(), "-hide_banner", "-nostats", "-i", str(path),
                          "-af", "volumedetect", "-f", "null", "-"], 120)
        if code == 0:
            m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
            x = re.search(r"max_volume:\s*(-?[\d.]+) dB", out)
            loud = {"mean_db": float(m.group(1)) if m else None,
                    "max_db": float(x.group(1)) if x else None}
    return {
        "seconds": round(float(info.get("seconds") or 0), 2),
        "width": width, "height": height,
        "aspect": _aspect(width, height),
        "fps": fps,
        "has_audio": bool(info.get("audio")),
        "mean_db": loud["mean_db"], "max_db": loud["max_db"],
        "bytes": path.stat().st_size if path.is_file() else 0,
    }


def _aspect(w: int, h: int) -> str:
    """The name of the shape, in the words a brief uses: 9:16, 16:9, 1:1, 4:5."""
    if not w or not h:
        return ""
    from math import gcd
    g = gcd(w, h)
    a, b = w // g, h // g
    for known in ((9, 16), (16, 9), (1, 1), (4, 5), (3, 4), (4, 3), (21, 9), (2, 3)):
        if abs(a / b - known[0] / known[1]) < 0.02:
            return f"{known[0]}:{known[1]}"
    return f"{a}:{b}"


def cuts(path: Path, *, max_seconds: float = MAX_SECONDS, threshold: float = SCENE) -> list[float]:
    """Where the picture changes hard, in seconds. This is the film's rhythm, counted.

    Scene detection is ffmpeg's own: frames whose difference crosses the threshold. Counting them
    gives "one shot every 1.8s" instead of "fast-paced", and a storyboard can be built to match a
    number.
    """
    code, out = _run([ffmpeg.path(), "-hide_banner", "-nostats", "-t", str(max_seconds), "-i", str(path),
                      "-filter:v", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"], 180)
    if code != 0:
        return []
    return [round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", out)]


def frames(path: Path, n: int = FRAMES, *, max_seconds: float = MAX_SECONDS,
           seconds: float = 0.0) -> list[tuple[float, bytes]]:
    """`n` frames spread evenly across the readable part, as `(time, png bytes)`.

    One ffmpeg call rather than n: `fps=1/step` samples at a fixed interval, which is the same thing as
    asking for evenly spaced frames and costs one process instead of eight. Written to a temporary
    directory — a study must not leave pictures behind in the group's workspace, where a later
    `assemble_video` would have to be told to ignore them.
    """
    if not path.is_file() or n < 1:
        return []
    window = min(seconds or max_seconds, max_seconds) or max_seconds
    step = max(0.05, window / n)
    out_dir = Path(tempfile.mkdtemp(prefix="ta-study-"))
    try:
        code, _ = _run([ffmpeg.path(), "-hide_banner", "-nostats", "-y", "-i", str(path),
                        "-vf", f"fps=1/{step:.4f},scale=512:-2", "-frames:v", str(n), "-q:v", "4",
                        str(out_dir / "f%02d.jpg")], 180)
        got: list[tuple[float, bytes]] = []
        for i, f in enumerate(sorted(out_dir.glob("f*.jpg"))):
            got.append((round(i * step, 2), f.read_bytes()))
        return got
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def speech(path: Path, workspace: Path | None = None) -> tuple[str, str]:
    """What the audio says, and why not when it cannot be read. Never guessed: a transcript that was
    not produced has to say so, or the spec invents narration the reference never had.

    With a `workspace`, the reading is remembered next to the file (`attachments.speech_key`), which
    matters here more than anywhere else: transcribing a reference is the slowest single step of
    studying it, and studying the same reference twice — once to see the format, again after a
    question about it — used to pay for it twice. Nothing is remembered when it failed: a machine
    that installs a transcriber later must not be held to a failure it has since fixed.
    """
    from . import attachments
    if workspace is None:
        return attachments.transcribe_with_reason(path)
    key = attachments.speech_key(workspace, path)
    remembered = attachments.cached_speech(workspace, key)
    if remembered:
        return remembered, ""
    text, why = attachments.transcribe_with_reason(path)
    if text:
        attachments.remember_speech(workspace, key, text)
    return text, why


# ------------------------------------------------------------------ saying it to a model
def frame_labels(pics: Sequence[tuple[float, bytes]]) -> str:
    """The times of the frames, as one line to put in the prompt.

    ⚠️ They belong in the *prompt*, not in the tuple's first slot: `vision.describe` takes
    `(mime, bytes)` there, so a label like `"t=3s"` reaches the provider as the media type and the call
    is rejected with "Input should be 'image/jpeg'…". The first version of this did exactly that, and
    the failure only shows up on a real provider.
    """
    return i18n.pick_now(
        "Frames in order, with their time in the reference: "
        + ", ".join(f"#{i + 1} t={t:g}s" for i, (t, _) in enumerate(pics)),
        "下面是按顺序的帧,以及它们在参考片里的时刻:"
        + ",".join(f"#{i + 1} t={t:g}s" for i, (t, _) in enumerate(pics)))


def as_pictures(pics: Sequence[tuple[float, bytes]]) -> list[tuple[str, bytes]]:
    """`(mime, bytes)` for the vision call. The frames are JPEGs; the time goes in the prompt."""
    return [("image/jpeg", data) for _t, data in pics]


def facts_text(f: dict, ctx: dict) -> str:
    """The measured part of the brief, in a form a model can lean on."""
    lines = [
        i18n.pick_now(f"Length: {f['seconds']}s", f"时长:{f['seconds']} 秒"),
        i18n.pick_now(f"Picture: {f['width']}x{f['height']} ({f['aspect']}), {f['fps']} fps",
                      f"画面:{f['width']}x{f['height']}({f['aspect']}),{f['fps']} fps"),
        i18n.pick_now(f"Sound: {'yes' if f['has_audio'] else 'none'}"
                      + (f", mean {f['mean_db']} dB / peak {f['max_db']} dB"
                         if f["mean_db"] is not None else ""),
                      f"声音:{'有' if f['has_audio'] else '没有'}"
                      + (f",平均 {f['mean_db']} dB / 峰值 {f['max_db']} dB"
                         if f["mean_db"] is not None else "")),
    ]
    shots = ctx.get("shots") or []
    if ctx.get("read_seconds"):
        lines.append(i18n.pick_now(
            f"Analysed the first {ctx['read_seconds']:g}s of a {f['seconds']:g}s piece.",
            f"只分析了前 {ctx['read_seconds']:g} 秒(全片 {f['seconds']:g} 秒)。"))
    if shots:
        gaps = [round(b - a, 2) for a, b in zip([0.0, *shots], [*shots, ctx["read_seconds"] or f["seconds"]])]
        lines.append(i18n.pick_now(
            f"Cuts at {shots[:24]} — {len(shots)} cuts, so about {round((ctx['read_seconds'] or f['seconds']) / max(1, len(shots)), 2)}s per shot "
            f"(median {sorted(gaps)[len(gaps) // 2]:.2f}s).",
            f"切点在第 {shots[:24]} 秒 —— 共 {len(shots)} 次切换,平均每镜约 "
            f"{round((ctx['read_seconds'] or f['seconds']) / max(1, len(shots)), 2)} 秒"
            f"(中位 {sorted(gaps)[len(gaps) // 2]:.2f} 秒)。"))
    if ctx.get("transcript"):
        n = len(re.sub(r"\s+", "", ctx["transcript"]))
        lines.append(i18n.pick_now(
            f"Speech transcribed ({n} characters in {ctx['read_seconds'] or f['seconds']:g}s, about "
            f"{n / max(1.0, ctx['read_seconds'] or f['seconds']):.1f} characters per second): "
            + ctx["transcript"][:400],
            f"有说话内容({n} 字 / {ctx['read_seconds'] or f['seconds']:g} 秒,约每秒 "
            f"{n / max(1.0, ctx['read_seconds'] or f['seconds']):.1f} 字):" + ctx["transcript"][:400]))
    elif ctx.get("speech_why"):
        lines.append(i18n.pick_now(f"Speech could not be read: {ctx['speech_why']}",
                                   f"说话内容没读到:{ctx['speech_why']}"))
    return "\n".join(lines)


LOOK_EN = (
    "These are frames from one video, in order, each labelled with its time in seconds. Describe each "
    "for someone who cannot see them, in one or two lines: what is on screen, where the text sits and "
    "how big it is, the colours, how the frame is composed (subject placement, depth, whether it is a "
    "photo, a drawing, an animation or a screen recording). Say what is the same across frames and what "
    "changes — that is the format. Do not flatter it and do not describe what it 'might' be."
)
LOOK_ZH = (
    "下面这一组是同一个视频里的若干帧,按时间排列,每张都标了秒数。请逐帧如实描述(写给看不见的人),一两句即可:"
    "画面里是什么、文字在什么位置多大、颜色、构图(主体位置、景深、是实拍/手绘/动画/录屏)。"
    "**同时说明哪些帧之间是一样的、哪些在变** —— 那就是它的版式与节奏。不要夸,也不要写「可能是」。"
)

SPEC_EN = (
    "You are writing a production brief from the frames, the timings and the numbers below, so another "
    "team can make a *new* video in the same format. Answer in this exact markdown structure and keep "
    "every heading, filling in what you can see and writing `unknown` where the evidence does not "
    "show it:\n"
    "## Format\naspect, length, how many shots and how long each, the overall look\n"
    "## Shot list\none line per shot: what is in it, how it is framed, any camera move, how long\n"
    "## On-screen text\nexactly where the captions sit, size and weight relative to the frame, style, "
    "whether they are burned in, how much text per screen\n"
    "## Sound\nnarration / music / ambience / silence, pace of speech, how sound meets the cuts\n"
    "## What makes it recognisable\nthe two or three things that would have to be true of the new video\n"
    "## What must be made fresh\nfootage, music, people — never reuse the reference's own material\n"
)
SPEC_ZH = (
    "请根据下面的帧、时间点和数字,写一份**制作规格**,让另一个团队能照着做出**新的**、同格式的视频。"
    "严格用下面这套 markdown 结构,标题一个都不要少;能看出来的写清楚,证据不足的地方写 `unknown`:\n"
    "## 版式\n画幅、时长、几个镜头、每镜多长、整体观感\n"
    "## 分镜\n每镜一行:画面内容、构图、有无运镜、多长\n"
    "## 屏上文字\n字幕/标题的确切位置、相对画面的大小与字重、样式、是否烧进画面、一屏大约几个字\n"
    "## 声音\n旁白/音乐/环境声/无声、语速、声音与切点的关系\n"
    "## 让人一眼认出的特征\n新的视频必须满足的两三条\n"
    "## 必须另做的部分\n画面素材、音乐、人物 —— 绝不沿用参考片自己的素材\n"
)


class StudyError(Exception):
    """Anything that stops a reference being read, with the reason already written for a reader."""


def doc_name(title: str) -> str:
    """A file name for the spec, from the reference's own title."""
    slug = re.sub(r"[\\/:*?\"<>|\s]+", "-", (title or "参考片").strip())[:48].strip("-")
    return f"参考风格-{slug or '参考片'}.md"


def write_spec(workspace: Path, name: str, *, title: str, url: str, facts: dict, body: str) -> Path:
    """Put the spec in the group's workspace — where the whole group can find it, and where the
    knowledge base picks it up on its own, so a member that never saw this call can still search it."""
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / name
    head = [f"# {i18n.pick_now('Reference format', '参考风格')}: {title or name}"]
    if url:
        head.append(i18n.pick_now(f"Source: {url}", f"来源:{url}"))
    head.append(i18n.pick_now(
        f"Measured: {facts['seconds']}s, {facts['width']}x{facts['height']} ({facts['aspect']}), "
        f"{facts['fps']} fps, sound: {'yes' if facts['has_audio'] else 'no'}",
        f"实测:{facts['seconds']} 秒,{facts['width']}x{facts['height']}({facts['aspect']}),"
        f"{facts['fps']} fps,声音:{'有' if facts['has_audio'] else '没有'}"))
    head.append(i18n.pick_now(RIGHTS_NOTE_EN, RIGHTS_NOTE_ZH))
    path.write_text("\n\n".join(head) + "\n\n" + body.strip() + "\n", encoding="utf-8")
    return path


def look_call(store: Any, router: Any) -> Callable[[list[tuple[str, bytes]], str], Awaitable[str]]:
    """`vision.describe` bound to this app's store and router — the injected 'eyes'.

    Kept as one line so the module stays testable: a test hands in its own callable and never needs a
    model. ⚠️ It is **async** and must be awaited — `vision.describe` is a coroutine, and pushing it
    through `asyncio.to_thread` (as the first version of the caller did) hands back the coroutine
    itself, so the brief comes out as `'coroutine' object has no attribute 'strip'` **after** the whole
    download and frame pass have already been paid for. Anything slow *around* the model call goes in
    a thread; the model call itself is awaited.
    """
    from . import vision

    async def look(pics: list[tuple[str, bytes]], prompt: str) -> str:
        return await vision.describe(store, router, pics, prompt)

    return look
