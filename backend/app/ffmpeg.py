"""Where `ffmpeg` is, and how to talk to it — the one place that knows.

Two callers need this and they must not import each other: the assembler (joining finished shots
into a film) and the animation engine (drawing frames here and muxing them into a clip). So the
binary plumbing lives in this leaf module and both import it.

The pair of rules below is what makes the app work when it was launched from Finder, where there is
no Homebrew on the PATH: ask `bindirs` for the program, and look for `ffprobe` beside `ffmpeg`
first. That also makes a hand-built ffmpeg (a folder dropped somewhere) work.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Callable

from . import i18n
from .bindirs import tool as _tool


def path() -> str:
    return _tool("ffmpeg") or ""


def probe_path() -> str:
    """`ffprobe`, next to whichever `ffmpeg` was found (see the module docstring)."""
    found = _tool("ffprobe")
    if found:
        return found
    ff = path()
    if ff:
        beside = Path(ff).with_name("ffprobe")
        if beside.is_file():
            return str(beside)
    return ""


def available() -> tuple[str, str]:
    """(why it can be used, why it cannot). Exactly one of the two is non-empty."""
    ff = path()
    if not ff:
        return "", i18n.pick_now(
            "Drawing an animation and assembling a film both need ffmpeg on this machine, and there "
            "is none. Install it (`brew install ffmpeg`) and the tools appear by themselves — "
            "nothing else is needed: the narration comes from the system's own speech, and the "
            "subtitles are drawn here.",
            "画动画和把片段拼成成片都需要本机有 ffmpeg,现在没有。装一个(`brew install ffmpeg`),"
            "这些工具就会自己出现——不需要别的东西:旁白用系统自带的语音,字幕在这里画。")
    if not probe_path():
        return "", i18n.pick_now(
            "ffmpeg is here but ffprobe is not, and without it the length of nothing can be "
            "measured. They are installed together: `brew install ffmpeg`.",
            "有 ffmpeg 但没有 ffprobe,没有它什么都量不出时长。两者是一起装的:`brew install ffmpeg`。")
    return ff, ""


def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a short command (ffprobe, `say`, an encoder probe) and return (code, combined output)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout:g}s"
    except OSError as e:
        return -1, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


_ENCODERS: dict[str, list[str]] = {}


def encoder() -> list[str]:
    """VideoToolbox when this build has it, libx264 otherwise — decided by asking, not by hoping.

    The hardware encoder is several times faster and matters here because a three-minute vertical
    film is 4300 frames, and an animation is drawn frame by frame before that; a build without it
    still works, just slower. Cached per resolved binary, so a test that points `path()` somewhere
    else is not served an answer about the real one.
    """
    ff = path()
    cached = _ENCODERS.get(ff)
    if cached is not None:
        return list(cached)
    code, out = run([ff, "-hide_banner", "-encoders"], 20)
    if code == 0 and "h264_videotoolbox" in out:
        chosen = ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-allow_sw", "1"]
    else:
        chosen = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    _ENCODERS[ff] = chosen
    return list(chosen)


async def pipe(cmd: list[str], next_frame: Callable[[], bytes | None], timeout: float) -> tuple[int, str]:
    """Feed raw frame bytes to a command's stdin — how a drawn animation becomes a clip.

    Frames are handed over as they are drawn instead of being parked in a folder first: twenty
    seconds at 24fps is 480 PNGs that nothing will ever read again, and encoding them would then
    take as long as drawing them. `-f rawvideo` on ffmpeg's side is what makes this a pipe rather
    than a muxing problem.

    `next_frame` returns one raw frame, or `None` when there are no more — and it is called **in a
    thread**, twice over: drawing a frame is tens of milliseconds of pure CPU, and the stdout of
    ffmpeg has to be read while it happens. Doing either on the event loop would stall every other
    conversation in the app for as long as the animation takes.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    async def body() -> bytes:
        reader = asyncio.ensure_future(proc.stdout.read())
        try:
            while True:
                chunk = await asyncio.to_thread(next_frame)
                if chunk is None:
                    break
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass                      # ffmpeg stopped early; whatever it printed is the real error
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, AttributeError):
                pass
        return await reader

    try:
        out = await asyncio.wait_for(body(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"timeout after {timeout:g}s"
    await proc.wait()
    return (proc.returncode or 0), out.decode("utf-8", "replace").strip()
