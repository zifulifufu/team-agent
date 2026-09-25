"""Members that are a command line on this machine rather than a model (`engine` kind `cmd`).

The four other members this app knows are: a chat model, a **generator** (a media member, which
calls an API), an **external agent** (a chat CLI or a gateway), and the hidden process engineer.
This is a fifth shape, and the user asked for it by name: "make Remotion, HyperFrames, VoiceStudio,
OpenChatCut … selectable members". They are not models and they are not chat partners — they are
**programs that make a file**, so the honest model is: address one, and it runs its own command and
hands back what came out.

Three disciplines shape this file, and each one is a mistake that is otherwise invisible:

* **The command belongs to us, not to the model.** A member's turn arrives as a sentence; turning a
  sentence into a shell command is how an app hands an attacker a shell. So each tool carries an
  `argv` template with at most two slots — `{out}`, which *we* name, and `{text}`, which goes in as
  **one argv element** and never through a shell. There is no `shell=True` anywhere in this module
  and there must not be: `asyncio.create_subprocess_exec` takes a list precisely so nothing is
  re-parsed.
* **Everything it may touch is inside the group's workspace.** The process's working directory is a
  folder *inside* the group's own workspace (`workspace/<tool>/`), and the artifact scan is confined
  to it. A member that could render into `~` would be a file-writing primitive wearing a video
  tool's clothes.
* **Not installed is a first-class answer.** Every one of these is a separate install, and a member
  that silently fails is what makes a user stop trusting the whole list. `probe` therefore answers
  "is it installed, at which path, and if not, exactly what to run" — the same rule the ComfyUI
  provider follows for a missing checkpoint.

⚠️ Nothing here installs anything. `install` is a sentence shown to the user, not a command this
app runs; the user decides when a few hundred megabytes to a few gigabytes come down.

⚠️ Licences differ and two of them matter for anything beyond personal use: Remotion is
source-available with a company-size threshold, VoiceStudio and OpenChatCut are AGPL. That is the
user's call to make, so `licence` is carried in the table and shown — and no code from any of them
is vendored into this repository.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from . import bindirs, i18n

# The kind `external.kind_of` reports for every row here. Named once because four dispatchers in
# `external.py` branch on it (`clean_cfg`, `describe`, `run`, `probe`) and a typo in one of them
# would route a local command into the chat-gateway path — where it would ask for a model name and
# then POST to nothing.
KIND = "cmd"

# One process, and it can be a long one: a five-second Remotion or HyperFrames render is minutes of
# Chromium frames. The cap is the ceiling the user may set, not a default we impose silently.
MIN_TIMEOUT, MAX_TIMEOUT = 60, 3600
DEFAULT_TIMEOUT = 1800

# How much of stdout/stderr is kept for the reply. A renderer is chatty and its middle is noise;
# the tail is what says why it failed.
# The engine that owns the "clone a voice" command shape. `voices.argv_for` builds that
# command; this module only delegates to it, so the flags cannot drift apart.
VOICE_ENGINE = "omnivoice"

OUTPUT_KEEP = 4000

# What a saved file must look like to be handed back. Deliberately a short list of *media*: the
# point of these tools is a film or a voice track, and a tool that leaves a 400 MB `node_modules`
# tree behind must not turn that into a "here is your file" message.
ARTIFACT_EXT = (".mp4", ".mov", ".webm", ".mkv", ".gif", ".wav", ".mp3", ".m4a", ".png", ".jpg")
# A file that is only worth mentioning if it is not a build intermediate. `out/`, `dist/` and the
# renderer's own cache are skipped by name — the alternative is a member that reports 900 files.
_SKIP_DIRS = {"node_modules", ".git", ".cache", ".turbo", "__pycache__", ".remotion", "chrome",
              "chromium", "browser"}


# What the interface calls each kind of file it is handed. One mapping, here, because the list of
# extensions above and this table have to agree: an artifact the UI has no vocabulary for would be
# listed as a bare filename with no player attached to it.
_KIND_OF_EXT = {
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video", ".gif": "video",
    ".wav": "audio", ".mp3": "audio", ".m4a": "audio",
    ".png": "image", ".jpg": "image",
}


def artifact_kind(filename: str) -> str:
    """`video` / `audio` / `image` / `file` for a produced file — the word the message's file entry
    carries, which is what decides whether the user gets a player or a download link."""
    return _KIND_OF_EXT.get(Path(str(filename or "")).suffix.lower(), "file")


def _row(tool: str, *, name: str, name_zh: str, avatar: str, cmd: list[str], install: str,
         install_zh: str, needs: tuple[str, ...], artifacts: tuple[str, ...],
         slot: str, role: str, role_zh: str, prompt: str, prompt_zh: str, tags: list[str],
         licence: str, docs: str, project: str = "", project_zh: str = "",
         verify: list[str] | None = None) -> dict:
    """One tool, in the shape `external.ENGINES` expects plus the four fields only this module
    reads. Written as a function with keyword arguments rather than a bare dict literal of eight
    near-identical entries: the fields are the contract, and a missing one should be a TypeError
    at import rather than a `KeyError` in the middle of a render."""
    return {
        "kind": KIND,
        "name": name, "name_zh": name_zh,
        "avatar": avatar,
        "role": role, "role_zh": role_zh,
        "prompt": prompt, "prompt_zh": prompt_zh,
        "tags": tags,
        # --- shown to the user, never executed by us
        "install": install, "install_zh": install_zh,
        "licence": licence, "docs": docs,
        # --- what this module reads
        "cmd": cmd,                 # argv template; `{out}` and optionally `{text}`
        # The argv that proves the *package* is installed, which is a different question from
        # whether its runtime is. `npx x` with no `--no-install` would happily fetch `x` from the
        # network, so "npx exists" is not an answer — and a probe that answers "ready" for a tool
        # the user has not installed is the one lie this whole table exists to prevent.
        "verify": verify or [],
        "slot": slot,               # "none" | "text" — whether the member's sentence reaches argv
        "needs": needs,             # binaries / runtimes that must exist, for the probe
        "artifacts": artifacts,     # globs, relative to the tool's folder
        "project": project, "project_zh": project_zh,   # what the user must put there first
        "tool": tool,
    }


# ⚠️ Only commands that were read out of the tool's own repository. Nothing here is a guess, and a
# tool whose entry point could not be established is *absent* rather than approximated — the path
# from "we invented a plausible command" to "the user sees a failure they cannot diagnose" is one
# step long.
TOOLS: dict[str, dict] = {
    "hyperframes": _row(
        "hyperframes",
        name="HyperFrames", name_zh="HyperFrames",
        avatar="🎞️",
        # `render` with no `-c` renders the project directory's `index.html`; `-o` is where the mp4
        # goes. Both facts are from `packages/cli/README.md` in the HyperFrames repository.
        cmd=["npx", "hyperframes", "render", "-o", "{out}"],
        install="npm install -g hyperframes", install_zh="npm install -g hyperframes",
        verify=["npx", "--no-install", "hyperframes", "--version"],
        needs=("node22", "ffmpeg"),
        artifacts=("*.mp4", "*.webm"),
        slot="none",
        role="Local tool · HyperFrames", role_zh="本机工具 · HyperFrames",
        prompt=(
            "You are HyperFrames — a command line that renders an HTML/CSS composition into a "
            "deterministic MP4 on this machine. Your turn runs `hyperframes render` inside the "
            "group's own folder for you and hands back whatever it wrote. You cannot write the "
            "composition yourself: if the folder has no `index.html`, say so and say that one has "
            "to be written there first (the project's own skills can write it). Report what "
            "actually happened — the file's name and size if it worked, the renderer's own error "
            "if it did not. Never describe a film you did not see."
        ),
        prompt_zh=(
            "你是 HyperFrames —— 一个把 HTML/CSS 作品渲染成确定性 MP4 的本机命令行。轮到你的回合时,"
            "程序会在本群自己的目录里执行 `hyperframes render`,并把它写出的文件交回群里。"
            "你自己写不了这份作品:如果那个目录里没有 `index.html`,就直接说明,并说明需要先写一个进去"
            "(群里的技能可以代写)。只报告真实发生的事 —— 成功就报文件名和体积,失败就报渲染器自己的报错。"
            "绝不要描述你没看过的片子。"
        ),
        tags=["video", "render", "html"],
        licence="Apache-2.0 (the renderer it drives, Remotion, has its own company-size threshold)",
        docs="https://github.com/HeyGen-Official/hyperframes",
        project="an HTML composition (index.html) in this folder",
        project_zh="这个目录里的一份 HTML 作品(index.html)",
    ),
    "voicestudio": _row(
        "voicestudio",
        name="VoiceStudio", name_zh="VoiceStudio",
        avatar="🗣️",
        # `omnivoice-infer` is a real console script (`pyproject.toml` → `[project.scripts]`), and
        # `--text` / `--output` are its two required arguments. The member's sentence is what gets
        # spoken, which is why this is the one row with a `{text}` slot.
        cmd=["omnivoice-infer", "--text", "{text}", "--output", "{out}"],
        install="cd <VoiceStudio clone> && uv sync   # then use its venv's omnivoice-infer",
        install_zh="cd <VoiceStudio 克隆目录> && uv sync   # 之后用它 venv 里的 omnivoice-infer",
        verify=["omnivoice-infer", "--help"],
        needs=("python",),
        artifacts=("*.wav", "*.mp3", "*.m4a"),
        slot="text",
        role="Local tool · VoiceStudio", role_zh="本机工具 · VoiceStudio",
        prompt=(
            "You are VoiceStudio — local voice cloning and speech synthesis on this machine. Your "
            "turn takes the sentence you were given, speaks it with the installed voice model, and "
            "hands the audio file back to the group. Speak the words as they are: you are the "
            "voice, not the author. If the voice model is not downloaded yet, say that instead of "
            "producing anything — the first run downloads about 2.3 GB."
        ),
        prompt_zh=(
            "你是 VoiceStudio —— 这台机器上的本地声音克隆与语音合成。轮到你的回合时,程序会拿你收到的"
            "那句话,用已装好的语音模型念出来,并把音频文件交回群里。照原样念:你是嗓子,不是作者。"
            "如果语音模型还没下载,就直接说明,不要产出任何东西 —— 第一次运行要下载约 2.3 GB。"
        ),
        tags=["audio", "tts", "voice"],
        licence="AGPL-3.0-only (closed-source commercial use needs a separate licence)",
        docs="https://github.com/omnivoice/VoiceStudio",
    ),
    "openchatcut": _row(
        "openchatcut",
        name="OpenChatCut", name_zh="OpenChatCut",
        avatar="✂️",
        # `occ render [<project>] --out <file>`; the CLI is explicitly built to run with no app, no
        # browser and no MCP session (`cli/main.ts`). ⚠️ It writes only with `--apply` — for a render
        # the output file is the write, so `--out` is enough.
        cmd=["occ", "render", "--out", "{out}"],
        install="cd <OpenChatCut clone> && npm install && npm run build:cli   # needs Node 24",
        install_zh="cd <OpenChatCut 克隆目录> && npm install && npm run build:cli   # 需要 Node 24",
        verify=["occ", "--version"],
        needs=("node24", "ffmpeg"),
        artifacts=("*.mp4", "*.mov", "*.mkv"),
        slot="none",
        role="Local tool · OpenChatCut", role_zh="本机工具 · OpenChatCut",
        prompt=(
            "You are OpenChatCut's offline command line — it renders and exports a video project "
            "without opening the editor. Your turn runs `occ render` in the group's own folder for "
            "you. Say what the exported file is called and how big it is; if there is no project "
            "there, say that a project has to be put there first. Note that this tool needs "
            "Node 24, which is a different runtime from the one this app's interface uses."
        ),
        prompt_zh=(
            "你是 OpenChatCut 的离线命令行 —— 它不开编辑器就能渲染并导出视频工程。轮到你的回合时,"
            "程序会在本群自己的目录里执行 `occ render`。请说明导出的文件叫什么、多大;"
            "如果那儿没有工程,就说明需要先放入一个工程。注意这个工具要 Node 24 —— "
            "和本程序界面用的运行时不是同一个。"
        ),
        tags=["video", "edit", "export"],
        licence="AGPL-3.0-or-later",
        docs="https://github.com/openchatcut/OpenChatCut",
        project="an OpenChatCut project in this folder",
        project_zh="这个目录里的一个 OpenChatCut 工程",
    ),
    "remotion": _row(
        "remotion",
        name="Remotion", name_zh="Remotion",
        avatar="⚛️",
        # `npx remotion render <entry-point> <composition-id> <output-location>`. Both the entry
        # point and the composition id belong to the *project*, so they are settings the user fills
        # in rather than values we could know; `--props` exists but is not wired here.
        cmd=["npx", "remotion", "render", "{entry}", "{composition}", "{out}"],
        install="npm install -g @remotion/cli   # and a Remotion project to render",
        install_zh="npm install -g @remotion/cli   # 还要有一个可渲染的 Remotion 工程",
        verify=["npx", "--no-install", "remotion", "--version"],
        needs=("node22",),
        artifacts=("*.mp4", "*.webm"),
        slot="none",
        role="Local tool · Remotion", role_zh="本机工具 · Remotion",
        prompt=(
            "You are Remotion — render video from React on this machine. Your turn runs "
            "`remotion render` for you, with the entry point and composition id the user configured "
            "and the output name we chose. Remotion renders *a project*: with no project in this "
            "folder there is nothing to render, and saying so is the right answer rather than "
            "describing an imagined film. Report the file's name and size, or the renderer's own "
            "error."
        ),
        prompt_zh=(
            "你是 Remotion —— 在这台机器上用 React 渲染视频。轮到你的回合时,程序会用用户配置的入口"
            "与 composition id、以及我们取好的输出名执行 `remotion render`。Remotion 渲染的是**一个工程**:"
            "这个目录里没有工程就没什么可渲染,直接说明才是正确答案,而不是描述一部想象中的片子。"
            "请报告文件名和体积,或者渲染器自己的报错。"
        ),
        tags=["video", "react", "render"],
        licence="Source-available; free for individuals and organisations of up to 3 people",
        docs="https://www.remotion.dev/docs/cli/render",
        project="a Remotion project, plus its entry point and composition id in the settings",
        project_zh="一个 Remotion 工程,并在设置里填上入口文件与 composition id",
    ),
    "shotcraft": _row(
        "shotcraft",
        name="video-shotcraft", name_zh="video-shotcraft",
        avatar="🎬",
        # This repository is a *skill pack* around a Remotion template, and it has no CLI of its
        # own: the only non-interactive thing it can do is render its own template
        # (`template/package.json` → `"render"`). So the command is that one template, verbatim.
        cmd=["npx", "remotion", "render", "src/index.ts", "AiflPromo", "{out}"],
        install="cd <video-shotcraft clone>/template && npm install",
        install_zh="cd <video-shotcraft 克隆目录>/template && npm install",
        # The template's own Remotion lives in *its* `node_modules`, so this verifies against the
        # tool's folder (`probe` uses it as the working directory whenever it is given one) rather
        # than a global install that this repository never asks for.
        verify=["npx", "--no-install", "remotion", "--version"],
        needs=("node22",),
        artifacts=("*.mp4",),
        slot="none",
        role="Local tool · video-shotcraft", role_zh="本机工具 · video-shotcraft",
        prompt=(
            "You are video-shotcraft's renderer — a fixed Remotion template for cinematic product "
            "films, with 157 shot recipes as its documentation. You render *that* template; the "
            "shot recipes are reading material for whoever edits it, not something you can apply by "
            "yourself. Report the file you produced, or the renderer's own error."
        ),
        prompt_zh=(
            "你是 video-shotcraft 的渲染器 —— 一个固定的 Remotion 模板,用来做有电影感的产品片,"
            "它的 157 张镜头配方是文档。你渲染的就是**那个**模板;那些配方是给改模板的人看的资料,"
            "不是你自己能套用的东西。请报告产出的文件,或渲染器自己的报错。"
        ),
        tags=["video", "remotion", "template"],
        licence="Apache-2.0 (the template renders through Remotion, licensed separately)",
        docs="https://github.com/video-shotcraft/video-shotcraft",
        project="the template's dependencies installed (npm install in its template/ folder)",
        project_zh="模板的依赖要装好(在它的 template/ 目录里 npm install)",
    ),
}

# What "not installed" is called in a `needs` entry, and how to test it. Kept as a table so the
# probe's sentence and the member's own hint cannot drift apart.
_NEEDS: dict[str, tuple[str, str]] = {
    "ffmpeg": ("ffmpeg", "brew install ffmpeg"),
    "node22": ("node", "Node 22+"),
    "node24": ("node", "Node 24 (this tool requires it)"),
    "python": ("python3", "Python 3.11+"),
}


def tools() -> dict[str, dict]:
    """The catalog, keyed by the engine id used in `agents.engine`."""
    return TOOLS


def row(engine: str) -> dict | None:
    return TOOLS.get(str(engine or "").strip())


def folder_name(engine: str) -> str:
    """The folder inside the group's workspace this tool works in. One folder per tool so two of
    them cannot overwrite each other's project, and so the artifact scan has a boundary that is
    not the whole workspace."""
    return re.sub(r"[^a-z0-9_-]", "", str(engine or "").lower()) or "tool"


def work_dir(workspace: Path, engine: str) -> Path:
    """Where the command runs: a real directory inside the group's workspace, created on demand.

    Refuses a symlink for the same reason `media.save_bytes` does — a member with `run_code` could
    have replaced it with one pointing at `~`, and everything below (cwd, artifact scan) would then
    be outside the workspace while looking inside it.
    """
    d = Path(workspace) / folder_name(engine)
    if d.is_symlink():
        raise LocalToolError(i18n.pick_now(
            f"\"{d}\" is a symlink, so nothing was run. A tool's folder has to be a real directory "
            "inside the group's workspace.",
            f"「{d}」是一个符号链接,所以没有执行。工具目录必须是工作目录里的真实目录。",
        ))
    d.mkdir(parents=True, exist_ok=True)
    return d


class LocalToolError(Exception):
    """A failure worth showing the user: the message is already in the request language."""


def _safe_out(engine: str, stamp: str = "") -> str:
    """The output file name. **Ours**, never the model's: it appears as an argv element, so letting
    a sentence reach it would be the one place a stray word could change the command's meaning."""
    return f"{folder_name(engine)}-{stamp or time.strftime('%Y%m%d-%H%M%S')}.mp4"


def argv_for(engine: str, *, out: str, instruction: str, ref_audio: str = "",
             ref_text: str = "") -> list[str]:
    """The command to run, with the slots filled.

    `{text}` is the member's sentence and goes in as **one element** — the whole reason this is a
    list and not a string. It is truncated at a sensible length rather than rejected: a long
    sentence is a normal thing to want spoken. An empty instruction leaves the slot as an empty
    argument, which is what these CLIs read as "nothing given" and answer with their own usage
    error — a better failure than one we invent.

    `ref_audio` is a cloned voice's reference recording. It is not a `{slot}` in the table: the
    *shape* of a cloning command belongs to whoever owns the engine, and `voices.argv_for` already
    owns it — so this delegates rather than keeping a second copy of the same flags that could
    drift (`--ref_audio`, with underscores; argparse does not accept `--ref-audio`). A table with a
    second spelling of the same command is how one of them ends up out of date.
    """
    r = row(engine)
    if r is None:
        raise LocalToolError(i18n.pick_now(f"Unknown local tool \"{engine}\".", f"未知的本机工具「{engine}」。"))
    text = " ".join(str(instruction or "").split())[:1000]
    if ref_audio and r["tool"] == "voicestudio":
        from . import voices
        return voices.argv_for({"engine": VOICE_ENGINE, "name": engine, "path": ref_audio,
                                "ref_text": ref_text},
                               text, Path(out))
    out_argv: list[str] = []
    for tok in r["cmd"]:
        if tok == "{out}":
            out_argv.append(out)
        elif tok == "{text}":
            out_argv.append(text)
        elif tok.startswith("{"):
            # A slot added to the table without a rule here would otherwise be passed to the
            # command as a literal `{entry}`, which fails with a message about a missing file.
            raise LocalToolError(i18n.pick_now(
                f"\"{engine}\" asks for a \"{tok}\" this build cannot fill in, so nothing was run.",
                f"「{engine}」需要一个这个版本填不了的「{tok}」,所以没有执行。",
            ))
        else:
            out_argv.append(tok)
    return out_argv


def _collect(root: Path) -> list[dict]:
    """The media files a run left behind, newest first, with the build trees skipped.

    A renderer leaves thousands of intermediates; what the group wants is the film. The extension
    list is the filter, and `_SKIP_DIRS` keeps the walk out of `node_modules` in the first place —
    which matters because walking one is the difference between a fast turn and a slow one.
    """
    found: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith(ARTIFACT_EXT):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            found.append({"path": p, "name": p.name, "bytes": st.st_size, "mtime": st.st_mtime,
                          "rel": str(p.relative_to(root))})
    found.sort(key=lambda f: f["mtime"], reverse=True)
    return found


async def run(engine: str, *, workspace: Path, instruction: str, timeout: int,
              on_line=None) -> dict:
    """Run one turn of a local tool. Returns what to say and what came out.

    Only files that appear **during this run** are reported: a tool folder accumulates output, and
    handing back last week's film as this turn's result is the kind of quiet wrongness that makes
    every later message suspect. `mtime` is the test — it is what the folder has to offer without
    keeping state of our own.
    """
    r = row(engine)
    if r is None:
        raise LocalToolError(i18n.pick_now(f"Unknown local tool \"{engine}\".", f"未知的本机工具「{engine}」。"))
    cwd = work_dir(Path(workspace), engine)
    out = _safe_out(engine)
    argv = argv_for(engine, out=out, instruction=instruction)
    started = time.time()
    env = _env(cwd)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name == "posix"), limit=16 * 1024 * 1024,
        )
    except OSError as e:
        raise LocalToolError(i18n.pick_now(
            f"Could not start \"{argv[0]}\": {e}. Is it installed? {r['install']}",
            f"无法启动「{argv[0]}」:{e}。装了吗?{r['install_zh']}",
        )) from None
    tail = bytearray()
    try:
        assert proc.stdout
        while chunk := await proc.stdout.read(4096):
            tail.extend(chunk)
            if len(tail) > OUTPUT_KEEP:
                del tail[:-OUTPUT_KEEP]
            if on_line is not None:
                await on_line(chunk.decode("utf-8", "replace"))
        rc = await asyncio.wait_for(proc.wait(), timeout=max(MIN_TIMEOUT, int(timeout)))
    except asyncio.TimeoutError:
        _kill(proc)
        raise LocalToolError(i18n.pick_now(
            f"\"{r['name']}\" was still running after {int(timeout)}s, so it was stopped. A render "
            "is minutes long — raise its timeout in the member's settings if this machine is just "
            "slow.",
            f"「{r['name']}」跑了 {int(timeout)} 秒还没结束,已停止。渲染本来就要几分钟 —— "
            "如果只是这台机器慢,请在成员的设置里把超时调大。",
        )) from None
    said = tail.decode("utf-8", "replace")
    files = [f for f in _collect(cwd) if f["mtime"] >= started - 1]
    return {"ok": rc == 0, "exit": rc, "text": said, "files": files, "command": argv,
            "cwd": cwd, "seconds": round(time.time() - started, 1),
            "install": r["install"], "install_zh": r["install_zh"], "name": r["name"]}


def _kill(proc) -> None:
    """Stop the process **and its children**. A renderer spawns Chromium or ffmpeg; killing only the
    parent leaves those running for the rest of the session, which on a local machine means a fan
    that never stops."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), 15)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _env(cwd: Path) -> dict[str, str]:
    """The child's environment: this app's own minimal one (`coderun.child_env`, which already
    carries the install prefixes from `bindirs`), plus a `HOME` inside the tool's folder.

    Overriding `HOME` is not decoration: npm, bun and Chrome all keep caches under `$HOME`, and a
    render that writes a few hundred megabytes into the user's real home directory (leaving it
    there) is a side effect a group chat has no business having. Everything the run needs lives
    under the folder it is already confined to.
    """
    env = dict(_child_env(cwd))
    env["HOME"] = str(cwd)
    env.setdefault("PATH", bindirs.search_path())
    return env


def _child_env(cwd: Path) -> dict[str, str]:
    """`coderun.child_env` wants a scratch directory for its own temporaries; this module has no
    per-call one, so the tool's folder serves. Imported lazily because `coderun` pulls in the whole
    execution sandbox, and this module is imported by `external` at startup."""
    from .coderun import child_env
    tmp = cwd / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return child_env(tmp)


# ------------------------------------------------------------------ the probe
def _binary(name: str) -> str:
    return shutil.which(name) or bindirs.tool(name) or ""


def _probe_cwd(engine: str, workspace: Path | None) -> Path:
    """Where a check runs. The tool's own folder when the caller knows the group — a project-local
    install is real, and `npx --no-install` only finds one from inside that project — and the home
    directory otherwise, which is where a global install is found from anywhere.

    ⚠️ Deliberately *not* `work_dir` when there is no workspace: creating a folder inside somebody's
    home directory as a side effect of pressing "test" would be a side effect nobody asked for.
    """
    if workspace is not None:
        return work_dir(Path(workspace), engine)
    return Path.home()


def _try(argv: list[str], *, cwd: Path, timeout: int = 25) -> tuple[int, str, str]:
    """Run one short check command and report `(exit code, stdout, stderr)`.

    Never raises. A probe is a *question*, so "no" has to be a return value rather than an
    exception — an exception here would surface as "the test button is broken" instead of "this
    tool is not installed".

    The environment is a deliberately thin one (PATH from `bindirs`, `HOME` at the working
    directory) rather than `coderun.child_env`: this is a version string, not a member's program,
    and the full child environment would create a scratch directory for it to write into.
    """
    import subprocess
    env = {"PATH": bindirs.search_path(), "HOME": str(cwd), "LANG": os.environ.get("LANG", "en_US.UTF-8")}
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", i18n.pick_now(f"timed out after {timeout}s", f"超过 {timeout} 秒没有响应")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def _node_major() -> int:
    """The major version of the `node` the child will actually get.

    ⚠️ `node --version` rather than `sys.version` of anything else: the version that matters is the
    one on the PATH this app hands to children, which is not necessarily the one in the user's
    shell (`coderun.child_env` exists because a Finder-launched app sees launchd's minimal PATH).
    Returns 0 when there is no node at all, which reads as "node is missing" below.
    """
    path = _binary("node")
    if not path:
        return 0
    rc, out, _ = _try([path, "--version"], cwd=Path.home(), timeout=10)
    if rc != 0:
        return 0
    try:
        return int(re.sub(r"[^0-9]", "", out.split(".")[0] or "0") or 0)
    except ValueError:
        return 0


def probe(engine: str, *, workspace: Path | None = None) -> dict:
    """Can this tool run? `{found, path, version, hint, install}` — the same shape the external
    agent dialog already renders, so the interface needs no new vocabulary for it.

    It answers three questions in the order that lets the user act: is the runtime there, is the
    tool itself there, and does the folder it will work in have anything to work on. A missing
    project is a *hint*, not a failure: the member is legitimately installed and simply has nothing
    to render yet, and that is a different thing to fix.
    """
    r = row(engine)
    if r is None:
        return {"found": False, "path": "", "via": KIND, "version": "",
                "hint": i18n.pick_now(f"Unknown local tool \"{engine}\".", f"未知的本机工具「{engine}」。"),
                "install": "", "install_zh": "", "name": engine}
    missing: list[str] = []
    notes: list[str] = []
    for need in r["needs"]:
        binary, what = _NEEDS.get(need, (need, need))
        if not _binary(binary):
            missing.append(f"{binary} — {what}")
            continue
        if need == "node24":
            major = _node_major()
            if major and major < 24:
                missing.append(i18n.pick_now(
                    f"Node 24 (this machine's node is {major}) — {what}",
                    f"Node 24(这台机器上的 node 是 {major})—— {what}"))
        elif need == "node22":
            major = _node_major()
            if major and major < 22:
                missing.append(i18n.pick_now(
                    f"Node 22+ (this machine's node is {major})", f"Node 22+(这台机器上的 node 是 {major})"))
    # The tool's own executable, which is a different question from its runtime: `npx hyperframes`
    # needs node *and* the package. `-y` would let npx fetch it silently, turning "not installed"
    # into a several-hundred-megabyte download in the middle of a turn, so it is not used.
    head = r["cmd"][0]
    path = _binary(head)
    if not path:
        missing.append(head)
    # And now the tool itself, which is a different question from its runtime: a machine with node
    # and npm does not have HyperFrames. Answered by running the row's own `verify` — with
    # `--no-install` where npm is involved, so this stays a check and never becomes a download in
    # the middle of a settings page.
    version = ""
    if not missing and r.get("verify"):
        rc, said, err = _try(r["verify"], cwd=_probe_cwd(engine, workspace))
        if rc == 0:
            version = (said.strip().splitlines() or [""])[0][:120]
        else:
            # A non-zero exit is reported as "not found", which is the actionable reading: either
            # it is genuinely absent, or it is installed somewhere this check cannot see — and both
            # are answered by the same install command.
            missing.append(r["verify"][0] if not str(r["verify"][0]).startswith("npx")
                           else r["verify"][2])
            if err.strip():
                version = err.strip().splitlines()[-1][:160]
    if not missing and not version:
        version = i18n.pick_now("the runtimes it needs are here", "它需要的运行时都在")
    hint = ""
    if missing:
        hint = i18n.pick_now(
            "Not ready: " + "; ".join(missing) + f". Install it with: {r['install']}",
            "还不能用:缺少 " + "; ".join(missing) + f"。安装: {r['install_zh']}",
        )
    elif workspace is not None and r.get("project"):
        d = Path(workspace) / folder_name(engine)
        if not d.is_dir() or not any(d.iterdir()):
            hint = i18n.pick_now(
                f"Installed and ready. This one renders {r['project']} — and that folder is still "
                f"empty, so its first turn will say there is nothing to render.",
                f"已安装、可用。这个工具渲染的是{r['project_zh']} —— 而那个目录现在还是空的,"
                "所以它的第一次回合会告诉你没有可渲染的东西。",
            )
    elif not hint:
        hint = i18n.pick_now("Installed and ready.", "已安装、可用。")
    return {"found": not missing, "path": path, "via": KIND, "version": version, "hint": hint,
            "install": r["install"], "install_zh": r["install_zh"], "name": r["name"],
            "licence": r["licence"], "docs": r.get("docs", "")}
