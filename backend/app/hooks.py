"""Hooks: the user's own code at a few fixed points of a group chat.

A hook lives in the data directory and is one small program:

    hooks/<id>/HOOK.json    which events it wants, its timeout, whether it may block
    hooks/<id>/hook.py      `def handle(event, payload): ...` — one JSON in, one JSON out

Six events, in two kinds. **Observers** are told what happened and their answer is ignored:
`round.start`, `round.end`, `agent.reply`, `tool.called`. **Gates** are asked a question before
something irreversible happens: `pre_tool_use` (may object, or change the arguments) and
`before_send` (may object, or change the text leaving this machine).

Four decisions worth knowing before changing any of this:

* **A gate can only tighten.** The user's own rules (`approvals.policy_for`) are consulted
  first, and a hook has no way to turn a `deny` into a run — there is deliberately no "allow"
  in its vocabulary, only "object". Other people's hook files get copied around; the one thing
  they must never be able to do is open a hole.
* **Everything runs in a subprocess** with the same trimmed environment `coderun` gives a
  member's program: no `TEAM_AGENT_TOKEN`, no API keys inherited from this process, its own
  process group, and a timeout that kills the whole group. A hook that hangs cannot hang the
  app, and a hook cannot read a keychain entry it was never handed.
* **Off unless switched on**, and the panel says so: a hook is somebody's code running on this
  machine, so its state belongs in the list rather than buried in a dialog.
* **A failure is never silent but never fatal**: a broken hook writes a line to the hook log
  and, for a gate, falls back by `on_error` (default: let read-only tools through, hold back
  anything that writes or sends — the same instinct as `perm_mode`).

The hook log (`hook-log.jsonl` in the data directory) keeps one line per run, so "why did it
not fire" is answerable without a debugger.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import i18n
from .approvals import risk_of
from .coderun import INTERPRETER, child_env, kill_group
from .router import redact

# Event -> (kind, default timeout in ms). Names follow the events the chat UI already receives,
# so there is one vocabulary rather than two.
EVENTS: dict[str, tuple[str, int]] = {
    "round.start": ("observe", 1500),
    "round.end": ("observe", 1500),
    "agent.reply": ("observe", 1500),
    "tool.called": ("observe", 1500),
    "pre_tool_use": ("gate", 1000),
    "before_send": ("gate", 1000),
}
# A hook may ask for longer, but not without a ceiling: every gate sits in front of a reply the
# user is waiting for.
MAX_TIMEOUT_MS = 5000
MAX_OUTPUT = 4000                     # characters of a hook's stdout we are willing to parse
# Argument names a gate never sees. A hook judging a tool call needs the real arguments (the
# program about to delete a file has to be readable) but never needs a credential, and a copied
# hook must not be able to harvest one out of a payload.
SECRET_ARG = re.compile(r"(api[_-]?key|token|secret|password|passwd|pwd|credential|authorization)", re.I)

GUIDE = """A hook is one folder with two files:

    hooks/my-hook/HOOK.json
    hooks/my-hook/hook.py

HOOK.json — which events, how long it may take, whether it is on:

    {
      "name": "Note every finished round",
      "events": ["round.end"],
      "enabled": false,
      "timeout_ms": 1500,
      "on_error": "auto",
      "groups": []
    }

hook.py — `handle(event, payload)` receives one dict, and whatever it returns is its answer:

    def handle(event, payload):
        print("debug text is fine, it is not read", file=sys.stderr)
        return None                    # observers may return nothing at all

Events you can ask for:

    round.start   round.end   agent.reply   tool.called
    pre_tool_use  before_send

Gates return one dict:

    {"block": false, "args": {"pattern": "*.md"}}   pre_tool_use: replace these arguments
    {"block": true, "reason": "why"}                object: the call is not made
    {"block": false, "text": "..."}                 before_send: replace the message

There is no way to *allow* something from a hook: a gate can only object or rewrite, and the
user's own permission settings still decide everything else. Turn a hook on in Settings → Hooks.
"""


GUIDE_ZH = """一个钩子就是一个文件夹、两个文件:

    hooks/my-hook/HOOK.json
    hooks/my-hook/hook.py

HOOK.json —— 要哪些事件、最多跑多久、开没开:

    {
      "name": "每轮结束记一行",
      "events": ["round.end"],
      "enabled": false,
      "timeout_ms": 1500,
      "on_error": "auto",
      "groups": []
    }

hook.py —— `handle(event, payload)` 收到一个 dict,它返回什么就是它的答复:

    def handle(event, payload):
        print("随便打印点调试信息没问题,没人读它", file=sys.stderr)
        return None                    # 旁观者可以不返回任何东西

可以登记的事件:

    round.start   round.end   agent.reply   tool.called
    pre_tool_use  before_send

闸门返回一个 dict:

    {"block": false, "args": {"pattern": "*.md"}}   pre_tool_use:改写这次调用的参数
    {"block": true, "reason": "为什么"}              否决:这次调用不会发生
    {"block": false, "text": "..."}                 before_send:替换要发出去的正文

钩子**无法**批准任何东西:闸门只能否决或改写,其余一切仍然由你自己的权限设置决定。
在「设置 → 钩子」里打开它。
"""


def guide(lang: str | None = None) -> str:
    """The how-to text in the reader's language. The code inside it stays as it is."""
    return GUIDE_ZH if (lang or i18n.current()) == "zh" else GUIDE


# The hook file only *defines* `handle`; this is what runs it. Kept as a generated file rather
# than a `if __name__ == "__main__"` block in every hook, so a hook stays a plain function and
# cannot get the protocol wrong. The answer comes back on one marked line, so a hook that prints
# its own debugging text cannot be mistaken for an answer.
RUNNER = '''"""Generated by Team Agent; do not edit. Runs one hook with the event on stdin."""
import importlib.util
import json
import sys
import traceback

MARK = "__team_agent_hook__:"


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        print("stdin was not JSON", file=sys.stderr)
        return 2
    event = str(payload.pop("event", ""))
    spec = importlib.util.spec_from_file_location("team_agent_hook", sys.argv[1])
    if spec is None or spec.loader is None:
        print(f"cannot load {sys.argv[1]}", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        traceback.print_exc()
        return 3
    fn = getattr(module, "handle", None)
    if not callable(fn):
        print("the hook has no handle(event, payload) function", file=sys.stderr)
        return 3
    try:
        answer = fn(event, payload)
    except Exception:
        traceback.print_exc()          # the parent keeps stderr out of the log, the exit code says "failed"
        return 4
    if answer is not None:
        print(MARK + json.dumps(answer, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
RUNNER_MARK = "__team_agent_hook__:"


@dataclass
class HookInfo:
    id: str
    path: Path
    name: str = ""
    description: str = ""
    events: list[str] = field(default_factory=list)
    timeout_ms: int = 1500
    on_error: str = "auto"                 # auto | open | closed
    enabled: bool = False
    groups: list[str] = field(default_factory=list)   # empty = every group
    error: str = ""
    last: dict = field(default_factory=dict)          # the last run, for the list

    @property
    def kind(self) -> str:
        return "gate" if any(EVENTS.get(e, ("", 0))[0] == "gate" for e in self.events) else "observe"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "events": self.events, "kind": self.kind, "timeout_ms": self.timeout_ms,
                "on_error": self.on_error, "enabled": self.enabled, "groups": self.groups,
                "error": self.error, "last": self.last}


# A run's own note is written to the log, which outlives the request that produced it — so it is
# stored in one language and rendered in the reader's, the same rule the model diagnostics follow.
# A hook's own wording is not in here: that is its answer, and it is passed through untouched.
_NOTE_NUM = re.compile(r"^did not answer within (\d+) ms$|^exited with code (\d+)$")
_START_FAILS = "could not be started: "


def localize_note(note: str, lang: str | None = None) -> str:
    """The note as the reader should see it. Text we did not write is returned as it is."""
    if not note or (lang or i18n.current()) != "zh":
        return note
    m = _NOTE_NUM.match(note)
    if m:
        return f"超过 {m.group(1)} 毫秒没有回应" if m.group(1) else f"退出码 {m.group(2)}"
    if note.startswith(_START_FAILS):
        return "启动失败:" + note[len(_START_FAILS):]
    if note == "its answer was not a JSON object":
        return "它返回的不是一个 JSON 对象"
    return note


def _read_spec(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class HookManager:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "hooks"
        self.runner = self.dir / ".runner.py"
        self.log_path = Path(data_dir) / "hook-log.jsonl"
        self.hooks: dict[str, HookInfo] = {}
        self.errors: list[str] = []
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        """(Re)scan the hooks directory.

        A hook whose HOOK.json is broken stays in the list with its error instead of vanishing:
        "it is not installed" and "it is installed but wrong" are different problems, and only
        the second one is the user's own doing.
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.runner.write_text(RUNNER, encoding="utf-8")
        except OSError:
            pass                              # a read-only data directory: every run below fails loudly
        found: dict[str, HookInfo] = {}
        errors: list[str] = []
        if self.dir.is_dir():
            for sub in sorted(self.dir.iterdir()):
                if not sub.is_dir() or sub.name.startswith("."):
                    continue
                spec_file, code_file = sub / "HOOK.json", sub / "hook.py"
                if not spec_file.is_file() or not code_file.is_file():
                    errors.append(f"{sub.name}: both HOOK.json and hook.py are required")
                    continue
                info = HookInfo(id=sub.name, path=code_file)
                spec = _read_spec(spec_file)
                if not spec:
                    info.error = "HOOK.json is missing or is not readable JSON"
                else:
                    info.name = str(spec.get("name") or sub.name)
                    info.description = str(spec.get("description") or "")
                    raw = [str(x) for x in (spec.get("events") if isinstance(spec.get("events"), list) else [])]
                    info.events = [e for e in raw if e in EVENTS]
                    if not info.events:
                        info.error = "no event it knows about is listed in \"events\""
                    try:
                        want = int(spec.get("timeout_ms") or EVENTS[info.events[0]][1] if info.events else 1500)
                    except (TypeError, ValueError):
                        want = 1500
                    info.timeout_ms = max(100, min(MAX_TIMEOUT_MS, want))
                    info.on_error = str(spec.get("on_error") or "auto").lower()
                    if info.on_error not in ("auto", "open", "closed"):
                        info.on_error = "auto"
                    info.enabled = bool(spec.get("enabled"))
                    groups = spec.get("groups")
                    info.groups = [str(g) for g in groups] if isinstance(groups, list) else []
                found[sub.name] = info
        self.hooks = found
        self.errors = errors

    def write_spec(self, hid: str, *, enabled: bool | None = None, groups: list[str] | None = None) -> HookInfo:
        """Change these two fields in place and leave the rest of HOOK.json to its author."""
        info = self.hooks.get(hid)
        if info is None:
            raise KeyError(hid)
        spec_file = info.path.parent / "HOOK.json"
        spec = _read_spec(spec_file)
        if enabled is not None:
            spec["enabled"] = bool(enabled)
            info.enabled = bool(enabled)
        if groups is not None:
            spec["groups"] = sorted(set(groups))
            info.groups = list(spec["groups"])
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return info

    def source(self, hid: str) -> str:
        info = self.hooks.get(hid)
        if info is None:
            raise KeyError(hid)
        return info.path.read_text(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------ selection
    def _for(self, event: str, gid: str) -> list[HookInfo]:
        """The hooks that want this event in this group, in id order — so the sequence is
        predictable, and so two hooks never race: they run one after the other."""
        return [h for h in sorted(self.hooks.values(), key=lambda x: x.id)
                if h.enabled and not h.error and event in h.events
                and (not h.groups or gid in h.groups)]

    def any_enabled(self) -> bool:
        return any(h.enabled and not h.error for h in self.hooks.values())

    # ------------------------------------------------------------------ running
    async def _run(self, hook: HookInfo, event: str, payload: dict) -> tuple[dict | None, str]:
        """Run one hook: JSON on stdin, JSON (or nothing) on stdout.

        Returns `(answer, note)`; `answer` is None when the hook produced nothing usable, and
        `note` says what happened so the log and the panel can show it. Never raises.
        """
        tmp = self.dir / ".tmp"
        try:
            tmp.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        body = json.dumps({"event": event, "hook": hook.id, "timeout_ms": hook.timeout_ms, **payload},
                          ensure_ascii=False, default=str)
        try:
            proc = await asyncio.create_subprocess_exec(
                *INTERPRETER["python"], str(self.runner), str(hook.path),
                cwd=str(hook.path.parent),
                env=child_env(tmp),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,        # own process group: the timeout can kill the tree
            )
        except (OSError, ValueError) as e:
            return None, f"could not be started: {e}"
        pgid = proc.pid                       # the group leader; read it before it exits
        try:
            try:
                out, _ = await asyncio.wait_for(proc.communicate(body.encode("utf-8")),
                                                hook.timeout_ms / 1000 + 0.2)
            except asyncio.TimeoutError:
                out = b""
                timed_out = True
            else:
                timed_out = False
        finally:
            kill_group(pgid)                  # nothing may outlive the call
        await proc.wait()
        if timed_out:
            return None, f"did not answer within {hook.timeout_ms} ms"   # stored: see HOOK_NOTES
        if proc.returncode != 0:
            return None, f"exited with code {proc.returncode}"
        text = (out or b"")[:MAX_OUTPUT].decode("utf-8", errors="replace")
        # Split on "\n" rather than `splitlines()`: the latter also breaks on control characters a
        # hook might print, which would cut the marked line in half (that is how this got found).
        for line in reversed(text.split("\n")):
            line = line.strip()
            if line.startswith(RUNNER_MARK):
                try:
                    answer = json.loads(line[len(RUNNER_MARK):])
                except ValueError:
                    continue
                if isinstance(answer, dict):
                    return answer, ""
                return None, "its answer was not a JSON object"
        return {}, ""                     # said nothing: for a gate that means "no objection"

    # ------------------------------------------------------------------ observing
    def notify(self, event: str, gid: str, payload: dict) -> None:
        """Tell the observers. Never blocks the round and never raises: a hook that is slow or
        broken must not turn into "the group is slow"."""
        for hook in self._for(event, gid):
            try:
                task = asyncio.ensure_future(self._observe(hook, event, gid, payload))
            except RuntimeError:      # no running loop (a tool call made from a plain thread)
                return
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _observe(self, hook: HookInfo, event: str, gid: str, payload: dict) -> None:
        t0 = time.time()
        answer, note = await self._run(hook, event, {**payload, "group_id": gid})
        hook.last = {"ok": not note, "note": note, "ms": int((time.time() - t0) * 1000), "at": time.time()}
        self.log(hook.id, event, gid, note, answer)

    async def drain(self) -> None:
        """Wait for the observers to finish. Used on shutdown and by the tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ gating
    async def gate_tool(self, gid: str, spec: dict, args: dict) -> tuple[str, dict]:
        """Ask the gates about one tool call. `(reason_to_block, args_after_the_hooks)`.

        Runs after the user's own permission rules, so all a hook can do is object or change the
        arguments — never grant. A hook returns only the fields it wants different; a later hook
        sees what an earlier one rewrote (the order is the hook ids, ascending).
        """
        for hook in self._for("pre_tool_use", gid):
            payload = {"group_id": gid, "tool": spec.get("name"), "source": spec.get("source"),
                       "risk": risk_of(spec), "args": scrub_args(args)}
            answer, note = await self._run(hook, "pre_tool_use", payload)
            hook.last = {"ok": not note, "note": note, "at": time.time()}
            self.log(hook.id, "pre_tool_use", gid, note, answer, tool=spec.get("name"))
            if answer is None:
                if self._fails_closed(hook, risk_of(spec)):
                    return i18n.pick_now(
                        f"Hook \"{hook.id}\" could not judge this call ({note}), and it is set to block when that happens.",
                        f"钩子「{hook.id}」无法判断这次调用({note}),而它设成这种情况下要拦截。"), args
                continue
            if answer.get("block"):
                return _blocked("pre_tool_use", hook, answer, "拦截了这次调用", "blocked this call"), args
            patch = answer.get("args")
            if isinstance(patch, dict):
                # A hook may change anything except a credential-named field: it was not shown
                # those, so a hook that echoes the whole payload back must not be able to turn a
                # real key into the placeholder it saw.
                keep = {k: v for k, v in args.items() if SECRET_ARG.search(str(k))}
                args = {**args, **patch, **keep}
        return "", args

    def _fails_closed(self, hook: HookInfo, risk: str) -> bool:
        if hook.on_error == "closed":
            return True
        if hook.on_error == "open":
            return False
        return risk != "read"                 # auto: a broken gate may not let a write through

    async def gate_outgoing(self, gid: str, text: str) -> tuple[str, str]:
        """Ask the gates about text that is about to leave this machine. `(reason, text)`.

        Failure means "hold it back" whatever `on_error` says, unless it is explicitly `open`:
        a message that went out cannot be recalled.
        """
        for hook in self._for("before_send", gid):
            answer, note = await self._run(hook, "before_send", {"group_id": gid, "text": text})
            hook.last = {"ok": not note, "note": note, "at": time.time()}
            self.log(hook.id, "before_send", gid, note, answer)
            if answer is None:
                if hook.on_error != "open":
                    return i18n.pick_now(
                        f"Hook \"{hook.id}\" could not check this message ({note}), and it is set to hold it back when that happens.",
                        f"钩子「{hook.id}」没能检查这条消息({note}),而它设成这种情况下要拦住。"), text
                continue
            if answer.get("block"):
                return _blocked("before_send", hook, answer, "没有让这条消息发出去", "did not let this message out"), text
            if isinstance(answer.get("text"), str):
                text = answer["text"]
        return "", text

    # ------------------------------------------------------------------ log
    def log(self, hook_id: str, event: str, gid: str, note: str, answer: dict | None, **extra: Any) -> None:
        row: dict[str, Any] = {"at": round(time.time(), 3), "hook": hook_id, "event": event,
                               "group_id": gid, "ok": not note, "note": note, **extra}
        if isinstance(answer, dict) and answer:
            row["answer"] = {k: v for k, v in answer.items() if k in ("block", "reason")}
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str)[:2000] + "\n")
            self._trim_log()
        except OSError:
            pass                              # a log that cannot be written is not worth failing over

    def _trim_log(self, keep: int = 1000, limit: int = 1_000_000) -> None:
        """Keep the log from growing for ever. Only when it is already big, so the usual write
        costs one `stat`."""
        try:
            if self.log_path.stat().st_size <= limit:
                return
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-keep:]
            self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def recent(self, limit: int = 50) -> list[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, limit):]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                row["note"] = localize_note(row.get("note", ""))
                out.append(row)
        return list(reversed(out))

    # ------------------------------------------------------------------ one-off run
    async def test(self, hid: str, event: str, payload: dict) -> dict:
        """Run one hook once from the panel, with a sample payload.

        "It is installed" and "it works" are different claims, and only the second one is worth
        showing the user.
        """
        hook = self.hooks.get(hid)
        if hook is None:
            raise KeyError(hid)
        answer, note = await self._run(hook, event, payload)
        self.log(hid, event, str(payload.get("group_id") or ""), note, answer, tool="(test)")
        return {"ok": not note, "note": note, "answer": answer}


def _blocked(event: str, hook: HookInfo, answer: dict, zh: str, en: str) -> str:
    reason = str(answer.get("reason") or "")[:300]
    head = i18n.pick_now(f"Hook \"{hook.id}\" {en}.", f"钩子「{hook.id}」{zh}。")
    return f"{head} {reason}" if reason else head


def scrub_args(args: dict) -> dict:
    """The arguments a hook is shown.

    Credential-named fields are replaced and the rest is passed through, redacted and cut at 2000
    characters: a policy hook has to be able to read the program it is judging, but a hook that
    writes its payload to a file (or gets copied to a colleague) must not be carrying a key
    around. A gate can neither read nor change those fields — see `gate_tool`.
    """
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if SECRET_ARG.search(str(k)):
            out[str(k)] = "…"
        elif isinstance(v, str):
            out[str(k)] = redact(v[:2000])
        else:
            out[str(k)] = v
    return out


def scrub_text(text: str, limit: int = 400) -> str:
    """A string safe to hand to a hook: secrets redacted, length capped."""
    return redact((text or "")[:limit])


EXAMPLE_HOOK = '''"""Example hook: one line per finished round, into a file of your own.

Off until you switch it on (Settings → Hooks). Nothing leaves this machine.
"""

import json
import pathlib


def handle(event, payload):
    if event != "round.end":
        return None
    path = pathlib.Path.home() / "team-agent-rounds.log"
    line = {
        "at": payload.get("at"),
        "group": payload.get("group_name") or payload.get("group_id"),
        "entries": payload.get("entries"),
        "seconds": payload.get("seconds"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\\n")
    return None
'''

EXAMPLE_SPEC = {
    "name": "Note every finished round",
    "description": "Appends one line per finished round to ~/team-agent-rounds.log.",
    "events": ["round.end"],
    "enabled": False,
    "timeout_ms": 1500,
    "on_error": "auto",
    "groups": [],
}


def ensure_example_hooks(data_dir: Path) -> None:
    """Write the example hook once so the panel has something real to show. Never overwrites."""
    folder = Path(data_dir) / "hooks" / "example-round-log"
    if folder.exists():
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hook.py").write_text(EXAMPLE_HOOK, encoding="utf-8")
        (folder / "HOOK.json").write_text(json.dumps(EXAMPLE_SPEC, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    except OSError:
        pass
