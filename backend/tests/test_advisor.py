"""Asking an outside model — codex or Claude Code — for a second opinion, read-only.

Three claims are defended here, and none of them is "a string was produced":

  * **read-only is a property of the command, not a promise in a prompt.** The built-in commands put
    `--tools Read,Grep,Glob` (claude) or `-s read-only` (codex) on the command line, and the test
    asserts that, so a later edit that drops the flag fails here rather than in somebody's repository.
  * **the question travels on stdin, and the answer comes back** — proved against a real executable
    (`/bin/sh` wearing the CLI's name), which also proves the child's working directory, its
    environment (no keys from this app) and the exit code are handled as described.
  * **the friction is real**: it is an `exec`-risk tool, so `policy_for` asks the user every time.

The last section drives the tool through the hub, which is what a member actually calls.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time

import pytest

from app import advisor
from app.approvals import policy_for


# ------------------------------------------------------------------ the built-in commands
def test_the_built_in_commands_are_read_only_by_construction(monkeypatch):
    monkeypatch.setattr(advisor, "found", lambda: ["claude", "codex"])

    claude, _ = advisor.command({}, folder="/tmp/ws")
    assert "--tools" in claude and "Read,Grep,Glob" in claude
    for forbidden in ("Write", "Edit", "Bash", "WebFetch"):
        assert forbidden not in claude, claude          # not in the session at all
    assert "--strict-mcp-config" in claude               # and no third-party MCP tools either

    codex, _ = advisor.command({"advisor_cmd": ""}, folder="/tmp/ws")
    assert codex == claude                               # claude is preferred when both are installed

    monkeypatch.setattr(advisor, "found", lambda: ["codex"])
    codex, label = advisor.command({}, folder="/tmp/ws")
    assert label == "codex" and "-s" in codex and codex[codex.index("-s") + 1] == "read-only"
    assert "/tmp/ws" in codex                            # the folder it works in
    assert codex[-1] == "-"                              # and "read the prompt from stdin"


def test_the_question_is_never_placed_on_the_command_line_by_default(monkeypatch):
    monkeypatch.setattr(advisor, "found", lambda: ["claude", "codex"])
    for name in ("claude", "codex"):
        spec = advisor.CANDIDATES[name]
        assert not any("{prompt}" in a for a in spec["args"]), name


def test_nothing_installed_says_so_and_names_the_command(monkeypatch):
    monkeypatch.setattr(advisor, "found", lambda: [])
    ok, why = advisor.available({}, folder="/tmp/ws")

    assert not ok
    assert "claude" in why and "codex" in why
    assert "npm install" in why                          # the useful half of the sentence
    info = advisor.info({}, folder="/tmp/ws")
    assert info["ready"] is False and info["installed"] == [] and info["label"] == ""


# ------------------------------------------------------------------ a command of the user's own
def test_a_hand_written_command_wins_and_its_placeholders_are_filled(monkeypatch):
    monkeypatch.setattr(advisor, "found", lambda: ["claude"])
    settings = {"advisor_cmd": "env HTTP_PROXY=http://127.0.0.1:7890 codex exec -s read-only -C {dir} -"}

    argv, label = advisor.command(settings, folder="/tmp/ws")
    assert argv[:2] == ["env", "HTTP_PROXY=http://127.0.0.1:7890"]
    assert "/tmp/ws" in argv and "{dir}" not in argv
    assert label == "env"                                # the program the user named


def test_a_program_that_is_not_there_is_named_rather_than_guessed(monkeypatch):
    ok, why = advisor.available({"advisor_cmd": "no-such-thing --watch"}, folder="/tmp/ws")
    assert not ok and "no-such-thing" in why
    assert "PATH" in why                                 # says *why* it may be missing


# ------------------------------------------------------------------ running it for real
def _fake_cli(tmp_path: pathlib.Path, script: str) -> pathlib.Path:
    """An executable that wears a CLI's name, so the whole path can be exercised for real."""
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _point_at(monkeypatch, exe: pathlib.Path) -> None:
    class Bins:
        def tool(self, name: str):
            return str(exe) if name == "claude" else None

        def search_path(self, current: str | None = None) -> str:
            return current or "/usr/bin:/bin"

    monkeypatch.setattr(advisor, "found", lambda: ["claude"])
    monkeypatch.setattr(advisor, "bindirs", Bins())


def test_the_question_arrives_on_stdin_and_the_answer_comes_back(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    exe = _fake_cli(tmp_path, 'cat > received.txt\nprintf "FINDING 1: note.txt is there\\n"\n')
    _point_at(monkeypatch, exe)

    answer = advisor.ask({"advisor_timeout": 30}, question="看一下 note.txt 在不在", folder=workspace,
                         group="测试群", timeout=30, extra="扫描结果第一行")

    assert answer.ok and answer.label == "claude" and answer.code == 0
    assert "FINDING 1" in answer.text
    received = (workspace / "received.txt").read_text(encoding="utf-8")
    assert "看一下 note.txt 在不在" in received            # the question itself
    assert "read-only" in received and "测试群" in received  # the framing it goes out with
    assert "扫描结果第一行" in received                      # and what the caller added
    assert answer.seconds >= 0


def test_a_command_that_asks_for_the_prompt_gets_it_as_an_argument(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    exe = _fake_cli(tmp_path, 'printf "ARGV:%s:" "$1"\n')
    _point_at(monkeypatch, exe)
    monkeypatch.setattr(advisor, "command",
                        lambda settings=None, folder="": ([str(exe), "{prompt}"], "claude"))

    answer = advisor.ask({}, question="只有这一句", folder=workspace, timeout=30)
    assert answer.ok and "ARGV:只有这一句:" in answer.text


def test_the_child_environment_carries_no_keys_from_this_app(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    exe = _fake_cli(tmp_path, 'env > child-env.txt\nprintf "done"\n')
    _point_at(monkeypatch, exe)
    monkeypatch.setenv("TEAM_AGENT_TOKEN", "secret-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")

    assert advisor.ask({}, question="x", folder=workspace, timeout=30).ok
    dump = (workspace / "child-env.txt").read_text(encoding="utf-8")
    assert "TEAM_AGENT_TOKEN" not in dump and "DEEPSEEK_API_KEY" not in dump
    assert "PATH=" in dump and "/usr/bin" in dump          # a usable PATH is still handed over


def test_a_failure_with_no_output_reports_the_exit_code_and_the_last_lines(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    exe = _fake_cli(tmp_path, 'echo "boom: not signed in" >&2\nexit 3\n')
    _point_at(monkeypatch, exe)

    answer = advisor.ask({}, question="x", folder=workspace, timeout=30)
    assert not answer.ok and answer.code == 3
    assert "3" in answer.text and "not signed in" in answer.text


def test_a_question_that_never_finishes_is_stopped_and_the_group_is_killed(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # The child writes its own pid, then hangs: after the timeout nothing of it may be left running,
    # because a CLI that has already started reasoning would keep going (and keep billing).
    exe = _fake_cli(tmp_path, 'echo $$ > pid.txt\nsleep 60\n')
    _point_at(monkeypatch, exe)

    answer = advisor.ask({}, question="x", folder=workspace, timeout=1.5)
    assert not answer.ok
    assert "1" in answer.text or "did not answer" in answer.text

    pid = int((workspace / "pid.txt").read_text().strip())
    gone = False
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            gone = True
            break
        time.sleep(0.2)
    assert gone, f"the child {pid} is still running after the timeout"


# ------------------------------------------------------------------ through the tool
def _hub(store, make_router):
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    return orch, group, agent, asyncio.run(orch.toolhub.context(group, agent, connect=False))


def test_the_tool_is_offered_only_when_something_can_answer(store, make_router, monkeypatch):
    monkeypatch.setattr(advisor, "available", lambda settings=None, folder="": (True, "ready"))
    orch, _, _, ctx = _hub(store, make_router)
    assert "ask_advisor" in ctx.tools, list(ctx.tools)
    # It starts a program, so the user is asked — every time, unless they allow it explicitly.
    # (The test fixture runs with `perm_mode: allow_all` for speed, so the default mode is set here.)
    assert ctx.tools["ask_advisor"]["risk"] == "exec"
    store.update_settings({"perm_mode": "ask_risky"})
    assert policy_for(store.get_settings(), ctx.tools["ask_advisor"]) == "ask"
    assert policy_for({"perm_allow": ["ask_advisor"], "perm_mode": "ask_risky",
                       "perm_deny": []}, ctx.tools["ask_advisor"]) == "allow"
    # ⚠️ and it is *not* announced through `problems`, which is posted into the chat on every turn.
    assert not any("ask_advisor" in p for p in ctx.problems), ctx.problems

    monkeypatch.setattr(advisor, "available", lambda settings=None, folder="": (False, "没装"))
    _, _, _, bare = _hub(store, make_router)
    assert "ask_advisor" not in bare.tools
    assert not any("没装" in p for p in bare.problems), bare.problems


def test_the_tool_hands_the_outside_answer_over_with_its_source(store, make_router, tmp_path, monkeypatch):
    exe = _fake_cli(tmp_path, 'cat > /dev/null\nprintf "FINDING 1: the plan has no owner\\n"\n')
    _point_at(monkeypatch, exe)
    orch, _, _, ctx = _hub(store, make_router)

    out = asyncio.run(orch.toolhub.call(ctx, "ask_advisor", {"question": "为什么没人被指派?", "evidence": "scan 第 3 行"}))
    assert out.ok, out.text
    assert "FINDING 1: the plan has no owner" in out.text
    assert "claude" in out.text                          # named, not presented as the app's own view
    assert "hypothesis" in out.text or "假设" in out.text

    empty = asyncio.run(orch.toolhub.call(ctx, "ask_advisor", {"question": "   "}))
    assert not empty.ok and "question" in empty.text
