"""The text inside a picture, read on this machine, for nothing.

Not "seeing" — and half of this module exists to keep the two apart. A model that can look says
what a picture *is*; this says which letters are printed on it. Told "here is what the picture
says" when all that happened was a character-recognition pass, a member reads a chart's axis
labels and believes it has understood the chart, and then reports on the chart. So every sentence
this module produces names which of the two happened, and `render` is the only way it is meant to
reach a prompt — there is no bare text accessor on purpose.

Why it is worth having: on a machine with no vision model and cloud vision off, an attached
screenshot was a dead end. `vision.reason_missing` names the setting to change, which is right for
a picture somebody wants *understood*, and useless to a member that only needed the words in an
error dialog or the columns of a table. macOS ships the recogniser that powers Preview's Live
Text, so that case costs nothing, needs no account, and sends nothing off the machine.

The program is looked up through `bindirs.tool`, the same one list every other external binary is
found from (it already contains `~/.local/bin`). Setting `TEAM_AGENT_OCR` overrides it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import i18n
from .bindirs import tool as _tool

OVERRIDE_ENV = "TEAM_AGENT_OCR"

# Asked for in this order. ⚠️ There is no quick mode to fall back to: measured 2026-09-27 on
# macOS 26, the accurate recogniser lists 30 languages including Chinese, and the fast one lists six
# Latin languages and **no Chinese at all** — so asking for fast on a Chinese screenshot returns
# "no text", which reads exactly like a picture that genuinely has none.
LANGS = "zh-Hans,en-US"

# Lines below this are decoration rather than content: measured on this project's own plates, a
# drawn heading came back at 0.30 and a real subtitle at 0.50-1.00. They are counted rather than
# dropped silently — see `read` — because text that quietly disappears is the one failure nobody
# can see, and a caller that knows how many lines were left out can say so.
MIN_CONF = 0.35

# Bounds, because this goes into a prompt that other things also have to fit inside.
MAX_IMAGES = 12
MAX_CHARS = 4000
TIMEOUT = 60.0

# Whether a given binary is the one this module knows how to talk to. Cached per resolved path so a
# test that points `path()` elsewhere is not served an answer about the real one.
_PROBED: dict[str, bool] = {}


class OcrError(RuntimeError):
    """Nothing to read with, or nothing readable — always carries the sentence to show the user."""


def path() -> str:
    """The program, or ""."""
    return (os.environ.get(OVERRIDE_ENV) or "").strip() or (_tool("ocr") or "")


def probe() -> tuple[bool, str]:
    """Is the program at `path()` the one this module knows how to drive?

    ⚠️ **The name is not evidence.** `bindirs.tool` finds *any* executable called `ocr`; a different
    tool of the same name would be handed `--json` and produce output this parser would read as a
    blank page — that is, "the picture has no text", which is precisely the class of silent wrong
    answer the rest of this app keeps having to design against. So it is asked once, and its answer
    is checked. Same shape as `ffmpeg.encoder()`, which decides by asking rather than by hoping.
    """
    exe = path()
    if not exe:
        return False, ""
    if exe not in _PROBED:
        try:
            p = subprocess.run([exe, "--languages"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            _PROBED[exe] = False
        else:
            _PROBED[exe] = "accurate" in (p.stdout or "")
    return _PROBED[exe], exe


def available() -> tuple[str, str]:
    """(the program to use, why it cannot be used). Exactly one of the two is non-empty.

    Same shape as `ffmpeg.available`, for the same reason: "it did not work" without the one thing
    that would fix it is a dead end for a reader who has never built this.
    """
    exe = path()
    if not exe:
        return "", i18n.pick_now(
            "Reading the text inside a picture happens here and needs no model, but this machine "
            "has no `ocr` program. Build it once (it is a small Swift wrapper around the text "
            "recogniser macOS already ships) and the capability appears by itself — the recipe is in "
            "`~/.local/opt/macos-ocr/README.md`.",
            "读图片里的文字在本机完成、不需要模型,但这台机器上没有 `ocr` 程序。编译一次即可"
            "(它是包着 macOS 自带文字识别的一小段 Swift),编译好后这个能力会自己出现 —— "
            "做法见 `~/.local/opt/macos-ocr/README.md`。")
    ok, _ = probe()
    if not ok:
        return "", i18n.pick_now(
            f"There is a program at {exe} called `ocr`, but it is not the one this app expects (it "
            f"is judged by its own `--languages` answer). Point {OVERRIDE_ENV} at the right binary, "
            "or build the one in `~/.local/opt/macos-ocr/README.md`.",
            f"{exe} 处有个叫 `ocr` 的程序,但它不是本程序要的那个(判据是它自己对 `--languages` "
            f"的回答)。请把 {OVERRIDE_ENV} 指向正确的二进制,或按 "
            "`~/.local/opt/macos-ocr/README.md` 编译一个。")
    return exe, ""


def read(paths: list[Path | str], *, timeout: float = TIMEOUT) -> list[dict]:
    """The text on each of these pictures: one entry per input, in the order given.

    Each entry is `{"file", "text", "lines", "dropped", "error"}` — `dropped` being the count of
    lines left out for being below `MIN_CONF`, so a caller can mention them instead of pretending
    they were never there. A file that could not be read comes back with `error` set and empty text
    rather than being left out of the list: an answer about seven of eight pictures that does not
    say which one is missing is worse than an answer about eight.

    Runs one process for the whole batch. Files are matched back by the exact string handed to the
    program, so a file it refused simply has no entry — which is how it is detected.
    """
    exe, why = available()
    if not exe:
        raise OcrError(why)
    wanted = [str(Path(p)) for p in paths][:MAX_IMAGES]
    if not wanted:
        return []
    try:
        proc = subprocess.run([exe, "--json", *wanted], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OcrError(i18n.pick_now(
            f"Reading text out of {len(wanted)} picture(s) did not finish within {timeout:g}s.",
            f"从 {len(wanted)} 张图片里读文字,{timeout:g} 秒内没读完。")) from None
    except OSError as e:
        raise OcrError(i18n.pick_now(f"Could not run the `ocr` program: {e}",
                                     f"跑不了 `ocr` 程序:{e}")) from None

    docs: dict[str, dict] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            docs[str(got.get("file") or "")] = got

    out: list[dict] = []
    for raw in wanted:
        doc = docs.get(raw)
        if doc is None:
            out.append({"file": Path(raw).name, "text": "", "lines": [], "dropped": 0,
                        "error": i18n.pick_now("this picture could not be read",
                                               "这张图片读不出")})
            continue
        lines, dropped = [], 0
        for item in doc.get("lines") or []:
            text = str((item or {}).get("text") or "").strip()
            if not text:
                continue
            if float((item or {}).get("conf") or 0.0) < MIN_CONF:
                dropped += 1
                continue
            lines.append(text)
        out.append({"file": Path(raw).name, "text": "\n".join(lines), "lines": lines,
                    "dropped": dropped, "error": ""})
    return out


def render(blocks: list[dict]) -> str:
    """What a member should read instead of the picture — text, and the limits of that text.

    The only supported way for this module's output to reach a prompt. It always says the same
    three things: where the text came from, that it is text and not a look at the picture, and
    whether anything was left out or not found. A member that is told the third one will ask for a
    vision model when it needs one; a member that is not told will guess.
    """
    if not blocks:
        return ""
    # Two lists on purpose: what was read (`found`, with the picture's name), and what could not be
    # (`missed`). Everything is in the answer either way — a report on seven of eight pictures that
    # does not say which one is missing is worse than one that mentions all eight.
    found: list[str] = []
    missed: list[str] = []
    for b in blocks:
        if b.get("error"):
            missed.append(i18n.pick_now(f"[{b['file']}] {b['error']}.",
                                        f"[{b['file']}] {b['error']}。"))
            continue
        if not b.get("text"):
            missed.append(i18n.pick_now(f"[{b['file']}] no text found in this picture.",
                                        f"[{b['file']}] 这张图里没有读出文字。"))
            continue
        got = f"[{b['file']}]\n{b['text']}"
        if b.get("dropped"):
            got += i18n.pick_now(
                f"\n(and {b['dropped']} line(s) of this picture read too faintly to trust, left out)",
                f"\n(另有 {b['dropped']} 行读得太糊、没敢采信,已略去)")
        found.append(got)

    head = i18n.pick_now(
        "[Text read off these pictures by this machine's own OCR. These are the words printed on "
        "them, NOT a description of what they show — nobody here has looked at the pictures. If the "
        "answer needs the picture itself understood (layout, colour, what is happening), say so "
        "rather than inferring it from this text.]",
        "【以下文字是本机 OCR 从图里读出来的。它们是图上的字,不是对画面的描述 —— 没有人看过这些图。"
        "如果问题需要真正看懂画面(版式、配色、发生了什么),请直说,不要拿这些文字去推。】")
    out = "\n\n".join([*found, *missed])
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + i18n.pick_now(
            f"\n\n[cut at {MAX_CHARS} characters]", f"\n\n[已截到 {MAX_CHARS} 字]")
    return head + "\n\n" + out if out else head
