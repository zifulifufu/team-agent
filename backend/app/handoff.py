"""Handing the process engineer's findings to a coding agent that can actually change things.

`advisor.py` asks a command-line model a question **read-only**: it comes back with an opinion about
why a run went wrong, and nothing on disk moves. That is the right shape for a second opinion and
the wrong shape for a fix — the ledger stays exactly as it was, and the same defect is measured
again in the next round.

This module is the other half. It takes what that ledger says is still open, writes it up as a work
order, and hands it to a program that **can edit files**: one of the two coding agents this machine
already has. Two of them, and they do not wait for each other — whichever gets there first is the
one that fixed it.

Four decisions are deliberate, because "let another program change my project" is easy to get
wrong:

  * **The directory is the group's own workspace and nothing else.** `Store.workspace_dir(gid)`,
    passed in by the caller. No model names a path here, and the only alternative to a real
    directory is a refusal — the same rule `advisor.py` follows, for the same reason.
  * **It may write, but only inside that directory.** `codex` runs under its own `workspace-write`
    sandbox; the WorkBuddy engine is started with the permission mode that stops asking. Neither is
    given a directory outside the group, so neither can reach past one.
  * **Nobody is asked to approve each edit.** This runs only from a button a person pressed — the
    press *is* the approval. The member-facing path (`ask_advisor`) still asks every single time,
    and the two must not be confused: one is a person deciding to let a program loose in a folder
    they can see, the other is a member quietly reaching for a subprocess.
  * **The ledger is not rewritten.** What it records is what is wrong with how the group works, not
    who was called in to help. Whether a fix worked is a question for the next round, which measures
    it; writing "handed over" into the entries would inflate the count of open defects and turn a
    measurement into a claim.

The environment is built by `external.build_env`, which is the one place in this program that knows
how to start WorkBuddy's bundled engine: it ships inside the app bundle, so the `codebuddy` command
is usually not on anybody's PATH, and it needs *its own* node on PATH or it dies with a missing
optional dependency. Building that a second time here is how the two would drift apart.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import advisor, bindirs, external, i18n, proclog

# ------------------------------------------------------------------ the two agents
# What each one is started with. Read as: "how do you tell this program to fix something, without
# being able to answer a prompt". Both are non-interactive by construction — a question asked in the
# middle of one of these is a hang, not a dialogue.
ARGV = {
    "workbuddy": [
        "-p", "--output-format", "text",
        # `acceptEdits` stops asking about file writes but still stops on a shell command, and a
        # run that stops mid-repair leaves the directory half-changed. A person pressed the button;
        # this is the mode where that button means what it says.
        "--permission-mode", "bypassPermissions",
        # A repair that needs forty turns is a repair that is not going well, but cutting it off
        # early is worse than letting it finish.
        "--max-turns", "40",
        # The engine would otherwise load the machine's MCP servers before saying anything, which is
        # minutes of latency per call and connectors nobody asked for.
        "--strict-mcp-config",
    ],
    "codex": [
        "exec",
        # codex's own sandbox: writes inside the working directory, no network, nothing outside.
        "-s", "workspace-write",
        "--skip-git-repo-check",
        # The trailing `-` means "read the prompt from stdin" — the same rule as `advisor.py`, so a
        # work order can never be read as a command line.
        "-",
    ],
}
ORDER = ("workbuddy", "codex")
LABEL = {
    "workbuddy": ("WorkBuddy", "WorkBuddy"),
    "codex": ("codex", "codex"),
}
INSTALL = {
    "workbuddy": ("Install the WorkBuddy desktop app, or point the hand-off at your own `codebuddy`.",
                  "装上 WorkBuddy 桌面应用,或者把外派指向你自己的 `codebuddy`。"),
    "codex": ("npm install -g @openai/codex", "npm install -g @openai/codex"),
}

# How much of one problem goes into the work order, and how many problems go in at all. Both are
# caps on a prompt that a command-line agent has to read in one go: past this the tail of the list
# is context it never gets to, and a list nobody finishes reading is not a work order.
ENTRY_CHARS = 1200
MAX_ENTRIES = 20
# How much of the answer comes back into the panel. The whole transcript is not the point; "what
# did it change" is.
MAX_CHARS = 6000

# Where a proxy might be listening when this app was started from the Finder and inherited no
# variables at all. Clash's 7890 first: on this machine that is the one that reaches the model
# services, and a coding agent that cannot reach one fails in a way that reads like the feature is
# broken. Only probed when nothing is set, and whatever is applied is reported back.
PROXY_PORTS = (7890, 7897, 1087)


@dataclass
class Sent:
    """One agent's answer: what it was, whether it worked, and what it said."""
    target: str = ""
    label: str = ""
    ok: bool = False
    text: str = ""
    seconds: float = 0.0
    code: int | None = None
    note: str = ""

    def brief(self) -> dict:
        return {"target": self.target, "label": self.label, "ok": self.ok, "text": self.text,
                "seconds": self.seconds, "note": self.note}


# ------------------------------------------------------------------ what gets handed over
def problems(workspace: Path | str) -> list[proclog.Entry]:
    """The entries still to be dealt with, newest first. `fixed` is **not** included.

    `fixed` means somebody already changed something and it has not been re-checked yet; sending it
    out again would ask a second agent to redo work that is waiting on a measurement, not on a fix.
    `verified` and `wontfix` are closed by definition. So it is `open` and nothing else.
    """
    path = proclog.unit(Path(workspace))
    if not path.is_file():
        return []
    try:
        entries = proclog.read(path)
    except OSError:
        return []
    mine = [e for e in entries if e.status == "open"]
    mine.sort(key=lambda e: ({"blocker": 0, "major": 1, "minor": 2}.get(e.severity, 3), e.id))
    return mine


def _entry_block(e: proclog.Entry) -> str:
    """One entry as a paragraph of the work order. Empty fields are left out — a heading with
    nothing under it reads as "there is more here" and costs the reader a scan."""
    sev = proclog.SEVERITY_LABEL.get(e.severity, ("", ""))[0]
    stage = proclog.STAGE_LABEL.get(e.stage, ("", ""))[0]
    lines = [f"### {e.id} · {e.title}   [{sev} / {stage}]"]
    for name, value in (("Symptom", e.symptom), ("Evidence", e.evidence), ("Cause", e.cause),
                        ("Suggested fix", e.fix), ("Hint", e.hint)):
        value = str(value or "").strip()
        if value:
            lines.append(f"- {name}: {value[:ENTRY_CHARS]}")
    if e.seen > 1:
        lines.append(f"- Seen: {e.seen} times")
    return "\n".join(lines)


def brief(*, group: str, folder: Path | str, entries: list[proclog.Entry], minutes: int) -> str:
    """The work order: what this team is, where it works, what is wrong, and the rules.

    Written to be *acted on* rather than agreed with — the same discipline `advisor.preamble` uses,
    for the same reason. A run where every problem is reported as plausible and nothing changed is
    the failure mode this shape exists to prevent, so the instruction is to fix, and the escape
    hatch is to say why not.
    """
    where = str(folder)
    head = i18n.pick_now(
        f"You are a coding agent being handed real work by the process engineer of a team of AI "
        f"agents. The team works in this directory: {where}\n"
        f"Its process log — what went wrong while the team was working — lives there too "
        f"(`{proclog.LOG_NAMES[0]}` / `{proclog.LOG_NAMES_ZH[0]}`). Below are the entries that are "
        f"still open, copied out for you so you do not have to go looking.\n"
        f"**Fix them.** Change the files that are wrong.\n\n"
        "The rules:\n"
        f"- Work only inside {where}. Do not touch anything outside it, and do not delete a file "
        "you are not sure about.\n"
        "- Every entry carries evidence — a file, or a line of the log. Reproduce the problem from "
        "that evidence first, then fix it. Do not fix what you have not seen.\n"
        "- When you are done, say which files you changed and why. Name them by path.\n"
        "- If a problem is not really a defect, or is beyond what you can change here, say so and "
        "why. Do not pretend to have fixed it, and do not rewrite the process log to close it.\n"
        f"- You have about {minutes} minutes. Work in order of severity.",
        f"你是一个编程智能体,现在有一个 AI 成员团队的程序工程师把真实的任务交给你。"
        f"这个团队的工作目录是:{where}\n"
        f"团队的流程日志(记录它干活时出了什么毛病)也在那里(`{proclog.LOG_NAMES_ZH[0]}` / "
        f"`{proclog.LOG_NAMES[0]}`)。下面是**还没有解决**的条目,已经抄给你,不用你自己去找。\n"
        f"**请把它们修掉**,改掉那些不对的文件。\n\n"
        "规矩:\n"
        f"- 只在 {where} 里面工作。不要碰它之外的任何东西,也不要删掉自己没有把握的文件。\n"
        "- 每条都带着证据 —— 某个文件,或日志里的某一行。先按证据把问题复现出来,再改。"
        "没亲眼看到的不要改。\n"
        "- 改完要说清楚:改了哪些文件、为什么这么改。**按路径点名**。\n"
        "- 如果某条其实不是毛病,或者超出了你在这里能改的范围,直接说明理由。"
        "不要假装改好了,也不要去改流程日志把它关掉。\n"
        f"- 你大约有 {minutes} 分钟。按严重程度从前往后做。",
    )
    body = "\n\n".join(_entry_block(e) for e in entries[:MAX_ENTRIES])
    if len(entries) > MAX_ENTRIES:
        body += "\n\n" + i18n.pick_now(
            f"({len(entries) - MAX_ENTRIES} more open entries are in the log — fix these first.)",
            f"(日志里还有 {len(entries) - MAX_ENTRIES} 条未解决 —— 先把上面这些修掉。)")
    return f"{head}\n\n{i18n.pick_now('--- open entries ---', '--- 未解决的条目 ---')}\n{body}"


# ------------------------------------------------------------------ running one agent
def _launcher(key: str) -> external.Launcher | None:
    """Which program to start for this agent — **one** function, so the command and the environment
    cannot disagree about which engine is being run (`build_env` puts this launcher's own node in
    front of PATH, and running one engine under another one's node is the crash in
    `external.preferred_node`).
    """
    if key == "workbuddy":
        return external.find_launcher("")
    if key == "codex":
        exe = bindirs.tool("codex")
        return external.launcher_for(exe, node=external.preferred_node()) if exe else None
    return None


def command(key: str, folder: Path | str) -> tuple[list[str], str] | None:
    """How to start `key`, and what to call it — or `None` when it is not on this machine.

    `workbuddy` goes through `external.find_launcher` because that is the one place that knows the
    engine ships inside the app bundle (see the module docstring). `codex` is an ordinary program
    and is looked up the ordinary way.
    """
    lc = _launcher(key)
    if not lc:
        return None
    folder = str(folder)
    if key == "workbuddy":
        return ([*lc.argv, *ARGV["workbuddy"], "--add-dir", folder], LABEL[key][0])
    if key == "codex":
        argv = ARGV["codex"]
        # `-C` right before the trailing `-` (which is what says "the prompt is on stdin"): the flag
        # takes a value, and a value-less flag after it would be read as the directory.
        return ([*lc.argv, *argv[:-1], "-C", folder, argv[-1]], LABEL[key][0])
    return None


def agent_env(key: str) -> tuple[dict[str, str], str]:
    """The environment one agent runs in, plus a sentence about how it reaches the network.

    Through `external.build_env`, which keeps this program's token and every provider key out of the
    child and puts the right node in front of PATH. The one addition is the proxy: when this app was
    opened from the Finder it inherited no `HTTP_PROXY` at all, and both of these agents need to
    reach a model service. So a listening local proxy is used, and **said out loud** in the note —
    an outbound call routed through the user's own proxy is a fact worth stating, not a detail to
    discover later.
    """
    env = external.build_env({}, _launcher(key))
    note, extra = _proxy()
    env.update(extra)
    return env, note


def _proxy() -> tuple[str, dict[str, str]]:
    """A local proxy to route through, when the environment names none. ("", {}) when it does."""
    existing = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy") and v}
    if existing:
        return "", {}
    for port in PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            continue
        url = f"http://127.0.0.1:{port}"
        return (i18n.pick_now(f"through the local proxy on port {port}",
                              f"经本机 {port} 端口代理"),
                {"HTTP_PROXY": url, "HTTPS_PROXY": url, "ALL_PROXY": url,
                 "http_proxy": url, "https_proxy": url, "all_proxy": url,
                 # The app's own endpoints are loopback; a proxy in front of those is the one way
                 # this could break something that was working.
                 "NO_PROXY": "127.0.0.1,localhost,::1"})
    return "", {}


def send(
    key: str, *, folder: Path | str, text: str, timeout: float = 1800,
) -> Sent:
    """Hand one work order to one agent and wait for it. Blocking — called from a worker thread.

    On timeout the **whole process group** is killed: an agent is a node process that spawned other
    processes, and killing only the parent leaves the rest of them running and still thinking.
    """
    label = LABEL.get(key, (key, key))[0]
    cmd = command(key, folder)
    if cmd is None:
        return Sent(target=key, label=label,
                    text=i18n.pick_now(
                        f"{label} is not on this machine. " + INSTALL[key][0],
                        f"这台机器上没有 {label}。" + INSTALL[key][1]))
    argv, label = cmd
    env, note = agent_env(key)
    started = time.time()
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is built here, from fixed parts and a real path
            argv, cwd=str(folder), env=env, text=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        out, err = proc.communicate(input=text, timeout=timeout)
    except FileNotFoundError:
        return Sent(target=key, label=label, note=note,
                    text=i18n.pick_now(f"Could not start `{argv[0]}` — is it installed and "
                                       f"executable?", f"启动不了 `{argv[0]}` —— 它装了吗、可执行吗?"))
    except subprocess.TimeoutExpired:
        advisor.kill_group(proc)
        return Sent(target=key, label=label, note=note, seconds=round(time.time() - started, 1),
                    text=i18n.pick_now(
                        f"{label} was still working after {int(timeout)} seconds and was stopped. "
                        "What it had done so far is in the directory; read what changed before "
                        "sending it the same list again.",
                        f"{label} 干了 {int(timeout)} 秒还没结束,已终止。它做到哪一步就留在目录里了 —— "
                        "再发同一份清单之前,先看看它改了什么。"))
    except OSError as e:
        return Sent(target=key, label=label, note=note,
                    text=i18n.pick_now(f"{label} could not be run: {e}", f"{label} 跑不起来:{e}"))
    seconds = round(time.time() - started, 1)
    body = (out or "").strip()
    if not body:
        # ⚠️ **Exit code 0 with an empty stdout is not success**, and this engine uses exactly that
        # shape for "I am not signed in" — measured 2026-09-27: `codebuddy -p …` wrote
        # `Authentication required. Please use /login command to sign in to your account` to
        # **stderr** and exited **0**. Reporting an empty answer there is indistinguishable from a
        # clean run that changed nothing, which is the one failure mode this project keeps paying
        # for. So an empty answer goes through the same explainer the external agent members use,
        # which knows the sign-in routes that actually work on this machine (the engine ships inside
        # the app bundle, so "run codebuddy in a terminal" is advice that fails on the first
        # keystroke).
        who = _launcher(key)
        return Sent(target=key, label=label, note=note, seconds=seconds, code=proc.returncode,
                    text=external.explain_failure(
                        proc.returncode, err or "", "",
                        login_cmd=" ".join(shlex.quote(a) for a in argv),
                        signin=external.has_signin_screen(who) if key == "workbuddy" else True,
                        # ⚠️ Not a member: the closing advice must not send the reader to a settings
                        # panel that does not exist for a button.
                        member=False))
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + i18n.pick_now(f"\n\n[cut at {MAX_CHARS} characters]",
                                               f"\n\n[已截到 {MAX_CHARS} 字]")
    return Sent(target=key, label=label, ok=True, text=body, seconds=seconds,
                code=proc.returncode, note=note)


def send_all(
    keys: list[str], *, folder: Path | str, text: str, timeout: float = 1800,
) -> list[Sent]:
    """Hand the same work order to several agents **at once**, and take whichever answers.

    At once on purpose: they are independent programs, and waiting for the first to fail before
    starting the second is time spent on nothing. ⚠️ They do share the directory — two agents
    editing the same files can collide, and the honest thing is to say so rather than to serialise
    them quietly and call it parallelism.
    """
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        return list(pool.map(lambda k: send(k, folder=folder, text=text, timeout=timeout), keys))


def installed() -> list[str]:
    """Which of the known agents this machine has. Same order as `ORDER`."""
    return [k for k in ORDER if command(k, ".") is not None]


def info() -> dict:
    """What the settings page shows: who would be dispatched, and who would not."""
    have = installed()
    return {
        "targets": [{"key": k, "label": LABEL[k][0], "ready": k in have,
                     "install": INSTALL[k][0]} for k in ORDER],
        "ready": have,
    }
