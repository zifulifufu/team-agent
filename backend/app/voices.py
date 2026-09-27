"""Cloned voices — someone's own voice, kept as an asset a film can be narrated in.

macOS's `say` gives this app narration for free, and it is why `assemble.py` was able to time a film
before it rendered anything. What it cannot give is *your* voice. A zero-shot cloning engine can, and
this module is the part that makes the result **an asset rather than a command**: a voice is a folder
with a reference recording and its transcript, named once and then usable by name.

⚠️ **Two namespaces, and telling them apart is the whole point of `parse`.** `Tingting` is a macOS
system voice; `voice:clinic-zh` is one of these assets. They go to completely different programs, and
the failure mode of confusing them is the one this codebase treats as expensive: `say -v voice:clinic-zh`
*parses fine* and then fails inside the recorder — or worse, a system voice name that happens to
resolve inside the clone engine produces a read in a voice nobody chose. So a `voice:` name that is
not in the registry is an **error naming the ones that are**, never a fallback.

⚠️ **What a voice's licence is, is the user's business, not ours to assume.** The shipped engine
(`omnivoice`, upstream OmniVoice) has **Apache-2.0 code and CC-BY-NC weights**, and a cloned voice
is a recording of a real person who has to have agreed to it. Both facts are the user's to satisfy;
this module's job is to keep the reference audio local and never upload it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import bindirs, i18n

__all__ = ["ENGINES", "VOICE_FOLDER_NAME", "use_folder", "reload", "available", "names", "row",
           "errors", "parse", "is_clone", "audio_suffix", "folder", "audio_of", "argv_for",
           "binary", "reason_missing", "CLONE_PREFIX", "MAX_REF_SECONDS"]

VOICE_FOLDER_NAME = "voices"
MANIFEST = "voice.json"
# A clone is made from a *sample*, and the engines are all built around a handful of seconds. Past a
# minute it is not a sample, it is a recording, and the engines either truncate it silently or get
# much worse — so the limit is enforced here with a reason rather than left to the engine's mood.
MAX_REF_SECONDS = 60.0

# One row per engine, and the row is the contract: the argument names below are the ones the shipped
# CLI actually declares (note `--ref_audio`, with underscores — argparse does not accept
# `--ref-audio`, and a renamed flag fails at synthesis time, after the plan has been timed).
ENGINES: dict[str, dict] = {
    "omnivoice": {
        "bin": "omnivoice-infer",
        "needs": ("python",),
        "install": "cd <VoiceStudio clone> && uv sync   # then use its .venv/bin/omnivoice-infer",
        "install_zh": "cd <VoiceStudio 克隆目录> && uv sync   # 之后用它 .venv/bin/omnivoice-infer",
        "licence": "AGPL-3.0 code; the shipped OmniVoice weights are CC-BY-NC (non-commercial)",
        "notes": "Zero-shot: the reference recording IS the voice. No training step, so a voice is "
                 "a file rather than a run of the model.",
    },
    # Qwen3-TTS(阿里 Qwen 团队开源,Apache-2.0)。加它的理由是一条**实测**的差距:长稿稳定性 ——
    # 官方报告里中文长语音的 WER 是 1.52,而 VoxCPM 是 4.84。旁白是整段念的,念错一个字就要重录
    # 一整段,所以「长稿不错字」排在「音色最像」前面。
    # ⚠️ 本机没有 CUDA:默认 device=cpu、dtype=float32(低精度在 Apple Silicon 上出过音质问题),
    # 慢一点但不猜。它的入口 `qwen-tts-say` 住在自己的 venv 里,所以只能靠 `bin` 或成员设置里的路径找到。
    "qwen3tts": {
        "bin": "qwen-tts-say",
        "needs": ("python",),
        "install": "cd ~/Documents/GitHub/Qwen3-TTS && uv venv --python 3.12 .venv && "
                   "uv pip install -U qwen-tts   # 再下 4.5GB 权重",
        "install_zh": "cd ~/Documents/GitHub/Qwen3-TTS && uv venv --python 3.12 .venv && "
                      "uv pip install -U qwen-tts   # 再下 4.5GB 权重",
        "licence": "Apache-2.0",
        "notes": "The reference recording IS the voice (3-second zero-shot clone); give it the "
                 "reference's transcript too and the clone keeps more of the original detail. "
                 "Runs on CPU on this machine, so it is slower than a GPU but needs no cloud.",
    },
}

_USER: dict[str, dict] = {}
_USER_ERRORS: list[dict] = []
_FOLDER: "Path | None" = None
_SIGN: tuple = ()


def use_folder(folder: "str | Path | None") -> dict:
    """Where the voices live. Called once at startup; safe to call again."""
    global _FOLDER, _SIGN
    _FOLDER = Path(folder) if folder else None
    _SIGN = ()
    if _FOLDER is not None:
        try:
            _FOLDER.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _note("voices/", i18n.pick_now(
                f"the folder could not be created ({type(e).__name__}: {e}), so no voice of yours "
                "can be loaded",
                f"这个目录建不出来({type(e).__name__}: {e}),所以你的音色一个都读不到"))
    return reload(force=True)


def _note(name: str, why: str) -> None:
    global _USER_ERRORS
    _USER_ERRORS = [e for e in _USER_ERRORS if e["file"] != name] + [{"file": name, "why": why}]


def _sign() -> tuple:
    if _FOLDER is None or not _FOLDER.is_dir():
        return ()
    out = []
    for d in sorted(_FOLDER.iterdir()):
        man = d / MANIFEST
        try:
            st = man.stat()
        except (OSError, NotADirectoryError):
            continue
        out.append((d.name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def _parse(name: str, body: object) -> dict:
    """One voice, checked. Raises `ValueError` whose text is the whole diagnosis."""
    if not isinstance(body, dict):
        raise ValueError(i18n.pick_now("the file must contain a JSON object",
                                       "文件内容必须是一个 JSON 对象"))
    ref = str(body.get("ref_audio") or "").strip()
    if not ref:
        raise ValueError(i18n.pick_now(
            "`ref_audio` is required: a zero-shot clone **is** the reference recording, so there is "
            "nothing to clone without one",
            "必须有 `ref_audio`:零样本克隆靠的就是那段参考录音,没有它就没有可克隆的东西"))
    engine = str(body.get("engine") or "omnivoice").strip().lower()
    if engine not in ENGINES:
        raise ValueError(i18n.pick_now(
            f"`engine` is \"{engine}\", which this app does not know. Use one of: "
            f"{', '.join(sorted(ENGINES))}",
            f"`engine` 是「{engine}」,本程序不认识。可用:{'、'.join(sorted(ENGINES))}"))
    return {
        "name": name,
        "ref_audio": ref,
        "ref_text": str(body.get("ref_text") or "").strip(),
        "note": str(body.get("note") or "").strip(),
        "engine": engine,
        # Optional, and the reason it exists: a cloning engine usually lives in the project's own
        # virtualenv (`<clone>/.venv/bin/omnivoice-infer`), which is on nobody's PATH. Without this
        # the voice layer is complete and the engine is unfindable — which reads as "the narration
        # broke" rather than "point me at the program".
        "bin": str(body.get("bin") or "").strip(),
        "device": str(body.get("device") or "").strip(),
        "language": str(body.get("language") or "").strip(),
        "timeout": float(body.get("timeout") or 900),
    }


def reload(*, force: bool = False) -> dict:
    """Re-read the folder if it changed. `{loaded: [names], errors: [{file, why}]}`."""
    global _USER, _USER_ERRORS, _SIGN
    sign = _sign()
    if not force and sign == _SIGN:
        return {"loaded": sorted(_USER), "errors": list(_USER_ERRORS)}
    _SIGN = sign
    rows: dict[str, dict] = {}
    errors: list[dict] = [e for e in _USER_ERRORS if e["file"] == "voices/"]
    for d in (sorted(_FOLDER.iterdir()) if _FOLDER and _FOLDER.is_dir() else []):
        if not d.is_dir():
            continue
        man = d / MANIFEST
        if not man.is_file():
            errors.append({"file": d.name, "why": i18n.pick_now(
                f"there is no {MANIFEST} in this folder", f"这个目录里没有 {MANIFEST}")})
            continue
        try:
            body = json.loads(man.read_text(encoding="utf-8"))
        except ValueError as e:
            errors.append({"file": d.name, "why": i18n.pick_now(
                f"{MANIFEST} is not valid JSON: {e}", f"{MANIFEST} 不是合法的 JSON:{e}")})
            continue
        except OSError as e:
            errors.append({"file": d.name, "why": f"{type(e).__name__}: {e}"})
            continue
        try:
            row = _parse(d.name, body)
            ref = (d / row["ref_audio"]).resolve()
            if not ref.is_relative_to(d.resolve()):
                raise ValueError(i18n.pick_now(
                    f"`ref_audio` points outside the voice's own folder ({row['ref_audio']})",
                    f"`ref_audio` 指到了这个音色目录之外({row['ref_audio']})"))
            if not ref.is_file():
                raise ValueError(i18n.pick_now(
                    f"there is no file at `ref_audio` = \"{row['ref_audio']}\"",
                    f"`ref_audio` = 「{row['ref_audio']}」那里没有文件"))
            row["path"] = str(ref)
        except ValueError as e:
            errors.append({"file": d.name, "why": str(e)})
            continue
        rows[d.name] = row
    _USER = rows
    _USER_ERRORS = errors
    return {"loaded": sorted(rows), "errors": errors}


def available() -> dict[str, dict]:
    reload()
    return dict(_USER)


def names() -> list[str]:
    return sorted(available())


def row(name: str) -> "dict | None":
    return available().get((name or "").strip())


def errors() -> list[dict]:
    reload()
    return list(_USER_ERRORS)


def folder(name: str) -> "Path | None":
    return (_FOLDER / name) if _FOLDER else None


# ---------------------------------------------------------------- the two namespaces
CLONE_PREFIX = "voice:"


def is_clone(value: str) -> bool:
    return (value or "").strip().lower().startswith(CLONE_PREFIX)


def audio_suffix(value: str) -> str:
    """What container the narrator will write for this voice.

    `.aiff` for `say` (which is what it produces), `.wav` for a cloned voice (which is what every
    cloning engine here produces). The caller names the file from this rather than guessing, because
    a `.aiff` holding RIFF/WAVE is a file whose name lies about its contents — and one that ffmpeg
    reads happily, so nothing would ever point it out.
    """
    return ".wav" if is_clone(value) else ".aiff"


def parse(value: str) -> tuple[str, "dict | None"]:
    """("system", None) for a macOS voice, ("clone", row) for one of these.

    Raises `ValueError` for a `voice:` name that is not in the registry. It must not fall through to
    the system voices and it must not fall through to the engine: both would narrate the film in a
    voice the caller did not ask for, and the film would be finished before anybody noticed.
    """
    want = (value or "").strip()
    if not is_clone(want):
        return "system", None
    name = want[len(CLONE_PREFIX):].strip()
    got = row(name)
    if got is None:
        broken = [e for e in errors() if e["file"] == name]
        why = (" " + broken[0]["why"]) if broken else ""
        raise ValueError(i18n.pick_now(
            f"There is no cloned voice called \"{name}\".{why} Known voices: "
            f"{', '.join(names()) or '(none yet)'} — without the `voice:` prefix a name means a "
            "macOS system voice instead.",
            f"没有叫「{name}」的克隆音色。{why}现有的音色:{'、'.join(names()) or '(还没有)'} —— "
            "不带 `voice:` 前缀的名字指的是 macOS 系统嗓音。"))
    return "clone", got


def audio_of(value: str) -> "Path | None":
    """The reference recording of a cloned voice, or None for a system voice."""
    kind, got = parse(value)
    return Path(got["path"]) if kind == "clone" and got else None


# ---------------------------------------------------------------- the engine
def argv_for(row_: dict, text: str, out: Path) -> list[str]:
    """The command that synthesises one line in this voice.

    Pure and separate from running it, so the flags can be checked against the engine's own
    documentation without a model download — which is how a renamed argument (`--ref_audio`, not
    `--ref-audio`) gets caught here instead of after a film has been timed.
    """
    engine = ENGINES[row_["engine"]]
    ref_flag = "--ref-audio" if row_["engine"] == "qwen3tts" else "--ref_audio"
    text_flag = "--ref-text" if row_["engine"] == "qwen3tts" else "--ref_text"
    cmd = [engine["bin"], "--text", text, "--output", str(out),
           ref_flag, str(row_["path"])]
    if row_.get("ref_text"):
        cmd += [text_flag, str(row_["ref_text"])]
    if row_.get("language"):
        cmd += ["--language", str(row_["language"])]
    if row_.get("device"):
        cmd += ["--device", str(row_["device"])]
    return cmd


def binary(engine: str = "omnivoice", override: str = "") -> str:
    """The engine's executable path, or "" when this machine does not have it.

    `override` is a voice's own `bin`, checked first: a cloning engine normally lives in the
    project's virtualenv and is on nobody's PATH, so "install it and it will be found" is only true
    for the ones that were installed as a *user* tool. A path that is not an executable file is
    treated as absent rather than trusted — passing a non-executable to `subprocess` fails with a
    message about the file, not about the configuration that produced it.

    Otherwise asked of `bindirs` rather than of `PATH` so it finds the same things every other child
    process this app starts finds — a tool installed by `uv tool install` or `pipx` lands in
    `~/.local/bin`, which is on that list and often not on the shell's.
    """
    want = (override or "").strip()
    if want:
        path = Path(want).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else ""
    name = ENGINES.get(engine, {}).get("bin") or ""
    return bindirs.tool(name) or "" if name else ""


def reason_missing(engine: str = "omnivoice") -> str:
    e = ENGINES.get(engine) or {}
    return i18n.pick_now(
        f"Cloned voices need `{e.get('bin')}`, which this app cannot find. Install it with: "
        f"{e.get('install')} — or, if it is already installed inside a project's virtualenv (where "
        "it is on nobody's PATH), put its full path in the voice's own `bin` field.",
        f"克隆音色需要 `{e.get('bin')}`,本程序找不到它。安装命令:{e.get('install_zh')} —— 或者,"
        "如果它已经装在某个项目自己的 venv 里(那里的东西不在 PATH 上),把这个音色的 `bin` 填成它的完整路径。")
