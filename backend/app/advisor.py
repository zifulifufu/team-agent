"""Asking another AI for a second opinion — codex, Claude Code, or any command line the user wires in.

Why this is a subprocess and not a provider row: the members of a group talk to *models*. What a
process engineer needs from the outside is different — a second pair of eyes on the machinery, with
its own tools, its own way of reading a repository, and a prompt written by somebody else. That is
what `codex` and `claude` are, and both of them are command-line programs on this machine already.

Three deliberate limits, because "let another AI look at this" is easy to get wrong:

  * **The prompt goes in on stdin, the answer comes out on stdout.** No shell, no argument quoting to
    get wrong, no chance of a question containing `; rm -rf …` being read as a command. `shlex.split`
    turns the configured command into an argv; nothing else is interpreted.
  * **The directory handed over is the group's workspace, never a path from the model.** A member's
    file access is the workspace; a tool that let a member name any directory on the disk and have
    another program read it would be a wider door than everything else in this app.
  * **Read-only is enforced by the command's own tool set**, not by asking nicely: `claude` is given
    `--tools Read,Grep,Glob` (Edit/Write/Bash do not exist in that session) and `codex` runs with
    `-s read-only`. A command the user writes themselves is their own business — this module says so
    instead of pretending to have checked it.

Nothing is offered when no such program is installed: the settings row names the command that would
install one, which is more use than a tool that fails on first call.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import bindirs, i18n

# What is on this machine, in the order it is preferred. `claude` first because it is the cheaper and
# quieter of the two to consult: on this machine it is pointed at an ordinary API endpoint, while
# `codex` reaches ChatGPT, may need the user's proxy, and loads its own MCP servers and skills before
# it says anything — measured at ~157k input tokens for the word "PONG".
CANDIDATES: dict[str, dict] = {
    "claude": {
        "exe": "claude",
        # `-p` reads the prompt from stdin; the tool list *is* the read-only guarantee, and
        # `--strict-mcp-config` keeps third-party MCP tools out of the session.
        "args": ["-p", "--tools", "Read,Grep,Glob", "--strict-mcp-config"],
        "install": "npm install -g @anthropic-ai/claude-code",
    },
    "codex": {
        "exe": "codex",
        # `exec` is the non-interactive mode, `-s read-only` its own sandbox, `-C {dir}` the folder it
        # works in, and the trailing `-` means "read the prompt from stdin" (the prompt is never
        # placed on the command line).
        "args": ["exec", "-s", "read-only", "-C", "{dir}", "--skip-git-repo-check", "-"],
        "install": "npm install -g @openai/codex",
    },
}
PREFERENCE = ("claude", "codex")
# What the answer is clipped to before it goes back into the conversation. A long report is still
# useful, but it is also context: past this the member should ask a narrower question instead.
MAX_CHARS = 20000


@dataclass
class Answer:
    text: str
    ok: bool = False
    label: str = ""
    seconds: float = 0.0
    code: int | None = None


def found() -> list[str]:
    """Which of the known command-line AIs this machine has, in preference order."""
    return [name for name in PREFERENCE if bindirs.tool(CANDIDATES[name]["exe"])]


def command(settings: dict | None = None, *, folder: Path | str = "") -> tuple[list[str], str] | None:
    """The argv to run and a label for it: the user's own command if there is one, else the first
    installed candidate.

    A command the user wrote wins outright, because the point of the field is to reach a program this
    module has never heard of. `{dir}` stands for the working directory and `{prompt}` for the
    question — leave `{prompt}` out and it is written to stdin instead, which is the right shape for
    both candidates above.
    """
    custom = str((settings or {}).get("advisor_cmd") or "").strip()
    if custom:
        argv = shlex.split(custom)
        if not argv:
            return None
        return ([a.replace("{dir}", str(folder)) for a in argv], argv[0])
    for name in found():
        spec = CANDIDATES[name]
        exe = bindirs.tool(spec["exe"]) or spec["exe"]
        return ([exe, *[a.replace("{dir}", str(folder)) for a in spec["args"]]], name)
    return None


def available(settings: dict | None = None, *, folder: Path | str = "") -> tuple[bool, str]:
    """Can an outside model be asked anything at all, and if not, why not — as a sentence the
    settings page can show next to the field."""
    cmd = command(settings, folder=folder)
    if cmd is None:
        want = " / ".join(CANDIDATES[n]["install"] for n in PREFERENCE)
        return False, i18n.pick_now(
            f"No command-line model found on this machine (looked for {', '.join(PREFERENCE)}). "
            f"Install one: {want} — or write your own command in the field below.",
            f"这台机器上没找到命令行模型(找过 {', '.join(PREFERENCE)})。装一个:{want} —— "
            f"或者把你自己的命令写在下面那一栏里。")
    argv, label = cmd
    if str((settings or {}).get("advisor_cmd") or "").strip():
        # A hand-written command is only checked for "is the program there at all": what it does
        # afterwards is the user's own arrangement, and guessing at its permissions would be a lie.
        first = argv[0]
        if os.sep in first or first.startswith("~"):
            if not Path(first.replace("{dir}", str(folder))).expanduser().is_file():
                return False, i18n.pick_now(
                    f"That command does not exist: {first}", f"这个命令不存在:{first}")
        elif bindirs.tool(first) is None and first not in ("env", "sudo"):
            return False, i18n.pick_now(
                f"`{first}` is not on this machine's PATH — check the command (an app started from "
                f"Finder does not see a shell's aliases).",
                f"`{first}` 不在这台机器的 PATH 上 —— 请检查命令(从访达启动的应用看不到你 shell 里的别名)。")
    return True, i18n.pick_now(f"Will ask: {label} (read-only)", f"将询问:{label}(只读)")


def preamble(label: str, folder: Path | str, group: str = "") -> str:
    """The framing every question goes out with.

    Written to get *findings* rather than an essay, and to make it say "nothing here" out loud —
    the same discipline the group's own auditing follows, for the same reason: a silent answer and
    a clean answer must not look alike.
    """
    where = str(folder)
    return i18n.pick_now(
        f"You are {label}, consulted read-only by the process engineer of a small team of AI agents "
        f"({group or 'a group chat'}). That team works in this directory: {where}. You may read "
        "anything under it; do not change, create or delete anything, and do not run commands that "
        "have effects outside this directory.\n"
        "The question is about the *process* — why a piece of work came out wrong, or what to change "
        "in the way the team works — not about the content of the documents themselves.\n"
        "Answer with findings, and for each one: the concrete evidence (file and what is in it, or "
        "the exact line of the record), why it breaks the flow, and one sentence on the fix. Say "
        "explicitly when a category you looked at has nothing wrong with it. Mark anything you could "
        "not verify as `uncertain:`. Do not invent file paths, tool names or numbers: if you have not "
        "seen it, say so. At most 8 findings, most serious first.",
        f"你是 {label},被一个由 AI 成员组成的小团队的流程工程师以只读方式咨询(团队:{group or '一个群聊'})。"
        f"这个团队的工作目录是:{where}。你可以读它下面的任何东西;不要修改、新建或删除任何文件,"
        "也不要执行会对这个目录之外产生影响的操作。\n"
        "问题是关于**流程**的 —— 某一步为什么做错了、团队的做事方式要改什么 —— 不是文档内容本身。\n"
        "请给出若干条发现,每条写清:具体依据(哪个文件、里面是什么,或记录里的哪一行)、它为什么破坏流程、"
        "以及一句话的修法。你查看过但没有问题的类别,请明确写「无发现」。没能核实的标 `uncertain:`。"
        "不要编造文件名、工具名或数字:没看到就说没看到。最多 8 条,最严重的在前。",
    )


def ask(
    settings: dict | None, *, question: str, folder: Path | str, group: str = "",
    timeout: float = 600, extra: str = "",
) -> Answer:
    """Run one question through the outside model and bring its answer back.

    Blocking on purpose: it is called from a worker thread (the tool layer), and a second process
    answering at the same time is not something this app needs. On timeout the whole process group is
    killed — a CLI that has already started thinking would otherwise keep going and keep billing.
    """
    cmd = command(settings, folder=folder)
    if cmd is None:
        ok, why = available(settings, folder=folder)
        return Answer(text=why, ok=False, label="")
    argv, label = cmd
    body = preamble(label, folder, group)
    if extra:
        body += "\n\n" + i18n.pick_now("What the caller added:\n", "调用者补充的内容:\n") + str(extra)
    body += "\n\n" + i18n.pick_now("The question:\n", "问题:\n") + str(question or "").strip()
    uses_prompt = any("{prompt}" in a for a in argv)
    argv = [a.replace("{prompt}", str(question or "")) for a in argv] if uses_prompt else argv

    env = _env(folder)
    started = time.time()
    proc = None
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is either ours or the user's own command
            argv, cwd=str(folder) or None, env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        out, err = proc.communicate(input="" if uses_prompt else body, timeout=timeout)
    except FileNotFoundError:
        return Answer(text=i18n.pick_now(f"`{argv[0]}` could not be started — is it installed and "
                                         f"executable?", f"启动不了 `{argv[0]}` —— 它装了吗、可执行吗?"),
                      ok=False, label=label)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return Answer(text=i18n.pick_now(
            f"{label} did not answer within {int(timeout)} seconds and was stopped. Narrow the "
            f"question, or raise the time limit in Settings → General.",
            f"{label} 在 {int(timeout)} 秒内没有回答,已终止。把问题问得更窄一些,"
            f"或在「设置 → 通用」里把时限调大。"), ok=False, label=label)
    except OSError as e:
        return Answer(text=i18n.pick_now(f"{label} could not be run: {e}", f"{label} 跑不起来:{e}"),
                      ok=False, label=label)
    seconds = round(time.time() - started, 1)
    text = (out or "").strip()
    if not text:
        # ⚠️ **An empty answer is not the same thing as a failed exit code**, and this family of
        # engines uses exactly that shape for "not signed in": the sentence goes to stderr and the
        # process still exits **0** (measured 2026-09-27 — see `handoff.send`, which learned it the
        # hard way). Keying off `returncode != 0` dropped the one line that explained the silence.
        tail = "\n".join((err or "").strip().splitlines()[-8:]) or i18n.pick_now(
            "(it printed nothing at all)", "(它什么都没打印)")
        return Answer(
            text=i18n.pick_now(
                f"{label} said nothing (exit code {proc.returncode}). Its last lines were:\n{tail}",
                f"{label} 什么都没说(退出码 {proc.returncode})。它最后几行是:\n{tail}"),
            ok=False, label=label, seconds=seconds, code=proc.returncode)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + i18n.pick_now(
            f"\n\n[cut at {MAX_CHARS} characters]", f"\n\n[已截到 {MAX_CHARS} 字]")
    return Answer(text=text, ok=bool(text), label=label, seconds=seconds, code=proc.returncode)


def _env(folder: Path | str) -> dict[str, str]:
    """The environment the outside model runs in.

    Built the same way a member's own code is (see `coderun.child_env`): PATH widened with the
    install prefixes, the app's token and API keys not handed over. Proxy variables *are* passed
    through when they are set, which is the opposite of what code run by a member gets — a CLI that
    talks to a model service is exactly the case where the user's own proxy is the right answer, and
    with the variables dropped it fails in a way that looks like a network outage.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env["PATH"] = bindirs.search_path(env["PATH"])
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        if k in os.environ:
            env.setdefault(k, os.environ[k])
    env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"   # no update checks inside a tool call
    return env


def kill_group(proc: subprocess.Popen | None) -> None:
    """Stop the whole process group. A CLI is a wrapper around node, which is a wrapper around the
    model call: killing only the parent leaves the rest running.

    Public because the hand-off to a coding agent (`handoff.py`) needs exactly this and must not
    grow a second copy of it — the two would drift, and a half-killed agent keeps billing.
    """
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def info(settings: dict | None = None, *, folder: Path | str = "") -> dict:
    """What the settings page shows next to the field: what would run, and the install command when
    nothing would."""
    ok, why = available(settings, folder=folder)
    cmd = command(settings, folder=folder)
    return {
        "ready": ok,
        "reason": why,
        "label": (cmd[1] if cmd else ""),
        "installed": found(),
        "install": " / ".join(CANDIDATES[n]["install"] for n in PREFERENCE),
    }
