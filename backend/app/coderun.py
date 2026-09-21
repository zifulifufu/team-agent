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
import os
import re
import signal
import sys
from pathlib import Path

from . import i18n

SUFFIX = {"python": ".py", "shell": ".sh"}
INTERPRETER = {"python": [sys.executable], "shell": ["/bin/sh", "-c"]}


def base_dir(data_dir: Path, settings: dict) -> Path:
    """The directory the per-group workspaces live under.

    `code_workdir` is a base, not the workspace itself: each group gets its own folder inside it,
    so one group's code and files never sit next to another's.
    """
    raw = str(settings.get("code_workdir") or "").strip()
    return Path(raw).expanduser() if raw else Path(data_dir) / "workspaces"


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


def workspace_path(data_dir: Path, settings: dict, gid: str = "") -> Path:
    """A group's workspace, without touching the filesystem.

    Reading is separated from creating so a page that only wants to show the path (the
    Permissions page) does not create a directory as a side effect of being opened. With no
    group there is nowhere to run, and the base is returned for display only.
    """
    if gid and not safe_gid(gid):
        raise ValueError(f"unusable group id: {gid!r}")
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


def workspace_dir(data_dir: Path, settings: dict, gid: str = "") -> Path:
    """A group's workspace. The model may only ever use this directory as its cwd."""
    base = workspace_path(data_dir, settings, gid)
    if not gid:
        raise ValueError("a code run needs a group workspace")
    base.mkdir(parents=True, exist_ok=True)
    if not inside(base_dir(data_dir, settings), base):
        raise ValueError(f"workspace escapes its base: {base}")
    return base


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
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(tmp_dir)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    return env


def command_for(language: str, path: Path, code: str) -> list[str] | None:
    if language == "python":
        return [*INTERPRETER["python"], str(path)]
    if language == "shell":
        return [*INTERPRETER["shell"], code]
    return None


async def run(language: str, code: str, cwd: Path, workspace: Path, timeout: float, limit: int) -> tuple[str, bool]:
    """Run one program and return (text for the model, ok)."""
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
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        out = b""
    finally:
        # Always reap the whole group, not only on the timeout path. A program that leaves a
        # child behind (`sh -c "sleep 999 &"`) would otherwise keep running after the call
        # returned, while the tool promises the run has ended. Everything the child needs is
        # already in `out`, so there is nothing left to wait for.
        kill_group(pgid)
    if timed_out:
        await proc.wait()
        return i18n.pick_now(
            f"It did not finish within {int(timeout)} seconds and was killed. Long-running or interactive programs are not supported.",
            f"运行超过 {int(timeout)} 秒,已被终止。不支持长时间运行或需要交互的程序。",
        ), False

    text = (out or b"").decode("utf-8", errors="replace")
    ok = proc.returncode == 0
    if len(text) > limit:
        text = text[:limit] + i18n.pick_now(f"\n…(output cut at {limit} characters)", f"\n…(输出已截断到 {limit} 字)")
    where = i18n.pick_now(f"exit code {proc.returncode}", f"退出码 {proc.returncode}")
    body = text.strip() or i18n.pick_now("(no output)", "(没有输出)")
    return f"[{where}]\n{body}", ok
