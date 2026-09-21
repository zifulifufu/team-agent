"""Running a member's code in a confined workspace.

The model writes a program, this module saves it under the workspace directory and runs it
with the interpreter, then hands back the output. Three properties matter more than the
convenience:

  * the working directory can never escape the workspace, whatever the model passes in;
  * the child gets a trimmed environment, so the app's own token and any API keys in the
    parent process are not readable from inside the run;
  * a timeout kills the whole process group, not just the interpreter — a `sleep` in a
    subprocess would otherwise outlive the run.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from . import i18n

SUFFIX = {"python": ".py", "shell": ".sh"}
INTERPRETER = {"python": [sys.executable], "shell": ["/bin/sh", "-c"]}


def workspace_path(data_dir: Path, settings: dict) -> Path:
    """Where runs happen, without touching the filesystem.

    Reading is separated from creating so that a page which only wants to show the path
    (the Permissions page) does not create a directory as a side effect of being opened.
    """
    raw = str(settings.get("code_workdir") or "").strip()
    return Path(raw).expanduser() if raw else Path(data_dir) / "workspace"


def workspace_dir(data_dir: Path, settings: dict) -> Path:
    """Where runs happen. The model may only ever use this directory as its cwd."""
    base = workspace_path(data_dir, settings)
    base.mkdir(parents=True, exist_ok=True)
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


def child_env(workspace: Path) -> dict[str, str]:
    """A deliberately small environment: no TEAM_AGENT_TOKEN, no inherited API keys.

    A member that needs a key should be told to ask for it explicitly; picking one up
    silently from the app's own process is exactly the accident this prevents.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(workspace / ".tmp")
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    (workspace / ".tmp").mkdir(exist_ok=True)
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
    runs = workspace / ".runs"
    runs.mkdir(exist_ok=True)
    path = runs / f"run{os.getpid()}_{len(list(runs.iterdir()))}{SUFFIX[language]}"
    if language == "python":
        path.write_text(code, encoding="utf-8")
    cmd = command_for(language, path, code)
    assert cmd is not None

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=child_env(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,          # own process group, so the timeout can kill the tree
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
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
