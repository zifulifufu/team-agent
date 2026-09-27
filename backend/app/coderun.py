"""Running a member's code in a confined workspace.

The model writes a program, this module saves it under the workspace directory and runs it
with the interpreter, then hands back the output. Three properties matter more than the
convenience:

  * the working directory can never escape the workspace, whatever the model passes in;
  * the child gets a trimmed environment, so the app's own token and any API keys in the
    parent process are not readable from inside the run;
  * a timeout kills the whole process group, not just the interpreter — a `sleep` in a
    subprocess would otherwise outlive the run (and the group is reaped after a normal exit
    too, so nothing outlives the call);
  * writes done *by this module* stay in the workspace as well. A member can create symlinks
    inside its own workspace, so every path this file opens is created without following
    links and re-checked for containment — otherwise the app itself would write through a
    link the member planted and land outside the boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

# What the live view in the chat gets while a program is still running: the text as it appears.
# It may be a coroutine function (the caller emits over a socket) or a plain one.
Output = Callable[[str], "Awaitable[None] | None"]

from . import i18n
from .bindirs import search_path

SUFFIX = {"python": ".py", "shell": ".sh"}
INTERPRETER = {"python": [sys.executable], "shell": ["/bin/sh", "-c"]}


def base_dir(data_dir: Path, settings: dict) -> Path:
    """The directory the per-group workspaces live under.

    `code_workdir` is a base, not the workspace itself: each group gets its own folder inside it,
    so one group's code and files never sit next to another's.

    ⚠️ **Resolved, and that is the whole point of doing it here.** Every writer downstream compares a
    path it built itself against this root, and most of them resolve their own side first (they have
    to: the check is what keeps a member from writing outside the workspace). If this side keeps the
    spelling the user typed, the two disagree the moment a component is a symlink — macOS makes
    `/tmp`, `/var` and `/etc` symlinks, and a picked workspace may sit under one. Measured: a group
    whose workspace was under `/tmp` could not write a single file. `docwrite` wrote it, then failed
    to compute its own relative path, so `write_document` reported "writing the file failed" **six
    times in one round while the file was on disk**, and the task was marked failed with it. One
    canonical root fixes every such comparison at once, which is why it is here rather than at each
    of the call sites.
    """
    raw = str(settings.get("code_workdir") or "").strip()
    return (Path(raw).expanduser() if raw else Path(data_dir) / "workspaces").resolve()


# A group id is generated as 12 hex characters. The check is here anyway: a restored backup can
# carry any id it likes, and `base / gid` with a value like "../.." would silently point outside.
GID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_gid(gid: str) -> bool:
    return bool(GID_RE.match(gid or ""))


def inside(root: Path, target: Path) -> bool:
    """Is `target` inside `root`, after resolving links? Both must already exist for a link to
    be followed; a path that does not exist yet is resolved lexically, which is what we want for
    a file we are about to create."""
    r, t = root.resolve(), target.resolve()
    return t == r or r in t.parents


def normalise_workspace(raw: object) -> Path | None:
    """The directory a user pointed at, or None when they did not choose one.

    `expanduser` and `resolve` rather than storing the string as typed: the value is written to the
    database and read back by a later process, so a `~` must not survive into it, and two spellings
    of the same folder must not look like two different workspaces.
    """
    s = str(raw or "").strip()
    return Path(s).expanduser().resolve() if s else None


def check_workspace(path: Path) -> str:
    """Why this directory cannot be a group's workspace, or "" when it can.

    It has to exist. A path the app will write into is not something to find out about later, and
    creating the missing directories instead would turn a typo (`/User/me/prj`) into a stray tree
    on disk that nothing points at. The app-managed workspace is the case where the app *is*
    allowed to make the folder, and that one is not passed through here.
    """
    if not path.exists():
        return i18n.pick_now(
            f"There is no directory at \"{path}\". Create it first, or pick one that exists.",
            f"「{path}」不存在。请先创建它,或另选一个已存在的目录。",
        )
    if not path.is_dir():
        return i18n.pick_now(
            f"\"{path}\" is a file, not a directory.", f"「{path}」是一个文件,不是目录。"
        )
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return i18n.pick_now(
            f"This programme may not read, write or enter \"{path}\".",
            f"本程序没有「{path}」的读、写或进入权限。",
        )
    return ""


def workspace_path(data_dir: Path, settings: dict, gid: str = "", explicit: object = "") -> Path:
    """A group's workspace, without touching the filesystem.

    Reading is separated from creating so a page that only wants to show the path (the Permissions
    page, the member dock) does not create a directory as a side effect of being opened. With no
    group there is nowhere to run, and the base is returned for display only.

    `explicit` is a directory the user picked for this group. It wins over the app-managed default
    (`<code_workdir or data dir>/<group id>`), and that is the point of it: what an agent writes
    should land where the user keeps that project, not inside the app's data directory.
    """
    if gid and not safe_gid(gid):
        raise ValueError(f"unusable group id: {gid!r}")
    chosen = normalise_workspace(explicit)
    if chosen:
        return chosen
    base = base_dir(data_dir, settings)
    return base / gid if gid else base


def scratch_dir(workspace: Path, name: str) -> tuple[Path | None, str]:
    """A directory this module owns inside the workspace (`.runs`, `.tmp`).

    Refused rather than quietly repaired when the path is a symlink or a file: the workspace
    belongs to the user, and silently deleting something they linked there is not this module's
    call. The point is only that the app must never write *through* such a link.
    """
    target = workspace / name
    try:
        if target.is_symlink():
            return None, i18n.pick_now(
                f"\"{target}\" is a symlink. Runs keep their scratch files in a real directory "
                "inside the workspace; remove the link and try again.",
                f"「{target}」是一个符号链接。运行用的临时文件必须放在工作目录里的真实目录中;请删掉这个链接再试。",
            )
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, i18n.pick_now(f"Could not prepare {target}: {e}", f"无法准备工作目录 {target}:{e}")
    if not inside(workspace, target):
        return None, i18n.pick_now(
            f"\"{target}\" resolves outside the workspace, so nothing was run.",
            f"「{target}」解析后在工作目录之外,没有执行。",
        )
    return target, ""


def safe_slug(text: str, fallback: str) -> str:
    """A folder name for a task, from its title.

    CJK is kept — the user's own words are the best label — and an id is appended by the caller,
    so two tasks with the same title never share a folder.
    """
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", (text or "").strip()).strip("-._")
    cleaned = cleaned[:40] or fallback
    return f"{cleaned}-{fallback}"


def make_dir(workspace: Path, target: Path) -> bool:
    """Create a directory inside the workspace. Refuses to create it *through* a link.

    Returns whether it created anything (False when it was already there), and raises on a path
    that leaves the workspace — the caller has usually checked already, this is the second look
    that every other write in this module also takes.
    """
    if not inside(workspace, target):
        raise ValueError(f"outside the workspace: {target}")
    parts = [p for p in target.relative_to(workspace).parts if p not in ("", ".")]
    walked = workspace
    for name in parts:
        walked = walked / name
        if walked.is_symlink():
            raise OSError(f"{walked} is a symlink")
        if walked.is_dir():
            continue
        if walked.exists():
            raise OSError(f"{walked} is a file, not a folder")
        walked.mkdir()
    if not inside(workspace, target):
        raise ValueError(f"resolves outside the workspace: {target}")
    return bool(parts)


def write_exclusive(path: Path, data: str) -> None:
    """Create a file without following a link that already sits at that name.

    `Path.write_text` opens with O_TRUNC, which *follows* a symlink — a member that planted a
    link at a predictable script name would have had the app truncate whatever it pointed at.
    O_EXCL|O_NOFOLLOW makes that an error instead, and O_CREAT means a fresh file.
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


def kill_group(pgid: int) -> None:
    """Kill a whole process group. `start_new_session=True` made the child its own leader, so the
    group id is the pid it was spawned with — kept by the caller, because after the leader exits
    `os.getpgid(pid)` raises and the grandchildren would survive."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass                      # already gone
    except PermissionError:
        try:
            os.kill(pgid, signal.SIGKILL)
        except OSError:
            pass


def workspace_dir(data_dir: Path, settings: dict, gid: str = "", explicit: object = "") -> Path:
    """A group's workspace, made if it is missing. The model may only ever use this as its cwd."""
    base = workspace_path(data_dir, settings, gid, explicit)
    if not gid:
        raise ValueError("a code run needs a group workspace")
    base.mkdir(parents=True, exist_ok=True)
    if not normalise_workspace(explicit) and not inside(base_dir(data_dir, settings), base):
        # Only the app-managed case is checked against the base: a directory the user picked is by
        # definition outside it. What still has to hold is the rule below — nothing is ever written
        # outside the workspace — which every writer enforces against the workspace itself.
        raise ValueError(f"workspace escapes its base: {base}")
    return base


def ensure_workspaces(data_dir: Path, settings: dict, targets: Iterable[tuple[str, str]]) -> int:
    """Give every group an **app-managed** workspace; returns how many were missing.

    Groups used to get their folder only when a member first ran code, so most had none and "the
    group's workspace" was a promise the app never kept. Called when a group is created, and once
    at startup for the groups that already existed (the same shape as the other backfills).

    `targets` is `(group id, the directory its user picked)`. A group with a picked directory is
    skipped on purpose: that folder belongs to the user, and recreating it at every start would
    resurrect one they deleted deliberately. It is made again when something actually needs it
    (`workspace_dir`), which is a decision somebody can undo — a startup backfill is not.
    """
    made = 0
    for gid, explicit in targets:
        try:
            if str(explicit or "").strip():
                continue
            if not safe_gid(gid):
                continue
            if not workspace_path(data_dir, settings, gid).is_dir():
                workspace_dir(data_dir, settings, gid)
                made += 1
        except (OSError, ValueError):
            continue
    return made


def resolve_cwd(workspace: Path, sub: str) -> tuple[Path | None, str]:
    """Turn the model's `cwd` into a directory inside `workspace`, or explain why not.

    An absolute path is refused rather than quietly reinterpreted as relative: a model that
    asks for `/etc` and silently gets `<workspace>/etc` would be told it succeeded while
    looking at a different directory than it thinks.
    """
    sub = (sub or "").strip()
    root = workspace.resolve()
    if sub in ("", "."):
        return root, ""
    if sub.startswith("/") or sub.startswith("~"):
        return None, i18n.pick_now(
            f"'{sub}' is an absolute path; runs happen inside the workspace, so give a path relative to it ({root}).",
            f"「{sub}」是绝对路径;代码只能在工作目录里运行,请给相对路径(相对 {root})。",
        )
    target = (root / sub).resolve()
    if target != root and root not in target.parents:
        return None, i18n.pick_now(
            f"'{sub}' is outside the workspace, so it is not allowed. Work inside {root}.",
            f"「{sub}」在工作目录之外,不允许。请在 {root} 里操作。",
        )
    target.mkdir(parents=True, exist_ok=True)
    return target, ""


def child_env(tmp_dir: Path) -> dict[str, str]:
    """A deliberately small environment: no TEAM_AGENT_TOKEN, no inherited API keys.

    A member that needs a key should be told to ask for it explicitly; picking one up
    silently from the app's own process is exactly the accident this prevents.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    # The inherited PATH is the group's, and when this app was started from the Finder that is
    # launchd's minimal one, on which `node`, `npx`, `uvx` and `ffmpeg` are all absent however
    # well they are installed — the same trap `bindirs` exists for. It matters here because a
    # member writing a video project runs `npx remotion render`, and "command not found" is not
    # an answer to a machine that has the tool. This widens convenience, not permission: the
    # child is a program the member wrote, and it may already call any of these by absolute path.
    env["PATH"] = search_path(env["PATH"])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Piped stdout is block-buffered, so a python program that prints progress would hand it over
    # in 8 KB lumps — hours of work showing as nothing, then everything at once. Unbuffered output
    # is what makes the live view in the chat show anything at all. Inherited by grandchildren,
    # which `-u` on the interpreter itself would not reach.
    env["PYTHONUNBUFFERED"] = "1"
    env["TMPDIR"] = str(tmp_dir)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    return env


def command_for(language: str, path: Path, code: str) -> list[str] | None:
    if language == "python":
        return [*INTERPRETER["python"], str(path)]
    if language == "shell":
        return [*INTERPRETER["shell"], code]
    return None


async def run(language: str, code: str, cwd: Path, workspace: Path, timeout: float, limit: int,
              on_output: Output | None = None) -> tuple[str, bool]:
    """Run one program and return (text for the model, ok).

    `on_output` receives the child's output in chunks *while it runs*. A build or a download takes
    minutes often enough that waiting for the exit code leaves the user watching a spinner with no
    idea what is happening — and "the member is running something" is not an answer when the thing
    it is running is what they want to see. Passing it changes nothing about the result: the text
    is accumulated either way, and the return value is the same.
    """
    if language not in SUFFIX:
        return i18n.pick_now(f"Unsupported language {language}; use python or shell.", f"不支持的语言 {language},请用 python 或 shell。"), False
    if not code.strip():
        return i18n.pick_now("The program is empty.", "程序是空的。"), False
    runs, why = scratch_dir(workspace, ".runs")
    if runs is None:
        return why, False
    tmp_dir, why = scratch_dir(workspace, ".tmp")
    if tmp_dir is None:
        return why, False
    path = runs / f"run{os.getpid()}_{len(list(runs.iterdir()))}{SUFFIX[language]}"
    if language == "python":
        try:
            write_exclusive(path, code)
        except OSError as e:
            return i18n.pick_now(
                f"Could not write the program into {runs}: {e}",
                f"无法把程序写入 {runs}:{e}",
            ), False
    cmd = command_for(language, path, code)
    assert cmd is not None

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=child_env(tmp_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,          # own process group, so the timeout can kill the tree
    )
    pgid = proc.pid                     # the group leader: read it now, it is gone once it exits
    timed_out = False
    chunks: list[str] = []

    async def pump() -> None:
        """Read the child's output as it appears, not only at the end.

        `read(4096)` returns as soon as *some* bytes are there, so a program that prints and then
        keeps working is visible while it works; `communicate()` would hold everything back until
        the process exits. Both the model's copy and the live view come from this one read, so the
        two can never disagree.
        """
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            chunks.append(text)
            if on_output is not None:
                result = on_output(text)
                if inspect.isawaitable(result):
                    await result

    try:
        await asyncio.wait_for(pump(), timeout)
        await proc.wait()               # the stream is at EOF; collect the exit status
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        # Always reap the whole group, not only on the timeout path. A program that leaves a
        # child behind (`sh -c "sleep 999 &"`) would otherwise keep running after the call
        # returned, while the tool promises the run has ended. Everything the child needs is
        # already in `chunks`, so there is nothing left to wait for.
        kill_group(pgid)
    if timed_out:
        await proc.wait()
        return i18n.pick_now(
            f"It did not finish within {int(timeout)} seconds and was killed. Long-running or interactive programs are not supported.",
            f"运行超过 {int(timeout)} 秒,已被终止。不支持长时间运行或需要交互的程序。",
        ), False

    text = "".join(chunks)
    ok = proc.returncode == 0
    if len(text) > limit:
        text = text[:limit] + i18n.pick_now(f"\n…(output cut at {limit} characters)", f"\n…(输出已截断到 {limit} 字)")
    where = i18n.pick_now(f"exit code {proc.returncode}", f"退出码 {proc.returncode}")
    body = text.strip() or i18n.pick_now("(no output)", "(没有输出)")
    return f"[{where}]\n{body}", ok
