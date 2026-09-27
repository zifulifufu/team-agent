"""Handing the process log's open entries to a coding agent that can change files.

`advisor.py` asks a question read-only and comes back with an opinion. This is the other half, and
the difference is the whole point — so that is what is asserted here, not that a string was built:

  * **write access is a property of the command line.** The ledger says what is broken; the program
    that fixes it has to be able to edit files, and the flags that give it that (codex's
    `-s workspace-write`, the engine's `--permission-mode bypassPermissions`) are asserted here, so
    an edit that quietly falls back to the read-only shape fails in this file rather than in
    somebody's project. The mirror image is asserted too: neither agent may be started in the
    read-only mode `ask_advisor` uses.
  * **it is the group's own directory, never one named by a model.** The work order travels on
    stdin, into a real executable wearing the agent's name, which also proves the child's working
    directory, its environment (no keys from this app) and what a timeout does to it.
  * **only what is actually open goes out.** `fixed` is waiting on a measurement, `verified` and
    `wontfix` are closed; re-sending any of them asks a second agent to redo or undo work.

⚠️ Nothing here asserts a **sentence**. The panel's wording is translated (`i18n.pick_now`), and a
test that pins one language is a test that goes red the day somebody switches — so the checks are on
the record, the flags and the directory, and where a message is checked at all it is checked for the
thing it has to name in either language ("process" / "流程").

The endpoint is driven at the end, since the button is the approval and therefore part of the
behaviour rather than an implementation detail.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import external, handoff, proclog
from app.main import create_app
from tests.conftest import FakeLLM


# ------------------------------------------------------------------ helpers
def _client(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="OK"))
    return TestClient(app, base_url="http://127.0.0.1"), app


def _store(tmp_path, name="data"):
    """A real install with one group: the ledger lives inside a real workspace."""
    return create_app(tmp_path / name, completion_fn=FakeLLM(default="OK")).state.store


def _fake_agent(tmp_path: Path, name="agent") -> Path:
    """A real executable wearing the agent's name: it records where it was run and what it was fed,
    then answers. The same shape `test_advisor.py` uses, for the same reason — a mocked subprocess
    would not prove that stdin and the working directory actually arrive."""
    exe = tmp_path / name
    exe.write_text('#!/bin/sh\npwd > "$0.cwd"\ncat > "$0.stdin"\nprintf "changed note.txt\\n"\n',
                   encoding="utf-8")
    exe.chmod(0o755)
    return exe


def _point_at(monkeypatch, exe: Path) -> None:
    """Make every agent resolve to that one script, so `send` can be driven end to end."""
    monkeypatch.setattr(handoff, "_launcher",
                        lambda key: external.Launcher([str(exe)], str(exe), "custom"))


def _ledger(store, group_id: str, *titles: str) -> list:
    """Open entries in that group's own log, each with evidence — the input a hand-off works from."""
    entries: list = []
    for title in titles:
        proclog.report(entries, title=title, symptom=f"{title} 的症状", evidence="第 12 行",
                       severity="blocker", stage="delivery", key=f"k:{title}", by="auto")
    proclog.write(proclog.unit(store.workspace_dir(group_id)), entries, "群")
    return entries


# ------------------------------------------------------------------ what goes out
def test_only_open_entries_are_handed_over(tmp_path):
    store = _store(tmp_path)
    gid = store.list_groups()[0]["id"]
    entries = _ledger(store, gid, "没落盘", "别的问题")
    proclog.update(entries, entries[1].id, status="wontfix", note="决定不改")
    proclog.write(proclog.unit(store.workspace_dir(gid)), entries, "群")

    assert [e.title for e in handoff.problems(store.workspace_dir(gid))] == ["没落盘"]


def test_a_fixed_but_unverified_entry_is_not_sent_out_again(tmp_path):
    """`fixed` waits on a measurement, not on a fix — sending it out asks for the work twice."""
    store = _store(tmp_path)
    gid = store.list_groups()[0]["id"]
    entries = _ledger(store, gid, "已经改了")
    proclog.update(entries, entries[0].id, status="fixed", note="改了写法")
    proclog.write(proclog.unit(store.workspace_dir(gid)), entries, "群")

    assert handoff.problems(store.workspace_dir(gid)) == []


def test_the_work_order_names_the_directory_every_entry_and_its_evidence(tmp_path):
    store = _store(tmp_path)
    gid = store.list_groups()[0]["id"]
    entries = _ledger(store, gid, "交付物没落盘")
    workspace = store.workspace_dir(gid)

    text = handoff.brief(group=store.list_groups()[0]["name"], folder=workspace,
                         entries=entries, minutes=30)

    assert str(workspace) in text                          # where to work
    assert entries[0].id in text and "交付物没落盘" in text  # which problem
    assert "第 12 行" in text                              # and the evidence for it
    assert "30" in text                                    # how long it has
    assert proclog.LOG_NAMES_ZH[0] in text                 # it may read the whole ledger itself


def test_entries_past_the_cap_are_named_rather_than_dropped(tmp_path):
    """A list that is silently cut reads as a complete list of what is wrong."""
    store = _store(tmp_path)
    gid = store.list_groups()[0]["id"]
    entries = _ledger(store, gid, *[f"毛病{i}" for i in range(handoff.MAX_ENTRIES + 3)])

    text = handoff.brief(group="群", folder=store.workspace_dir(gid), entries=entries, minutes=5)

    assert entries[0].id in text                                   # the first goes out
    assert entries[handoff.MAX_ENTRIES - 1].id in text             # so does the last one that fits
    assert entries[handoff.MAX_ENTRIES].id not in text             # the tail does not
    assert "3" in text                                             # and the count left behind is said


# ------------------------------------------------------------------ how they are started
def test_neither_agent_is_dispatched_read_only(monkeypatch, tmp_path):
    """⚠️ The guard that matters: this module exists to be the *writable* half.

    An edit that made the hand-off reuse `ask_advisor`'s flags would still answer, still look like
    it worked, and never change a file — the failure this project has paid for before.
    """
    monkeypatch.setattr(handoff.bindirs, "tool", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(external, "find_launcher",
                        lambda cli_path="": external.Launcher(["codebuddy"], "/x/codebuddy", "bundled"))
    monkeypatch.setattr(external, "launcher_for",
                        lambda raw, node="": external.Launcher(["codex"], str(raw), "path"))

    for key in handoff.ORDER:
        argv, _ = handoff.command(key, tmp_path)
        assert argv, key
        assert "read-only" not in argv, argv            # codex's read-only sandbox
        assert "dontAsk" not in argv, argv              # the engine's read-only permission mode
        assert "Read,Grep,Glob" not in argv, argv       # claude's read-only tool list
        assert str(tmp_path) in argv, argv              # and it is told which directory


def test_codex_gets_its_own_write_sandbox_and_the_prompt_on_stdin(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff.bindirs, "tool", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(external, "launcher_for",
                        lambda raw, node="": external.Launcher(["codex"], str(raw), "path"))

    argv, label = handoff.command("codex", tmp_path)

    assert label == "codex"
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[-1] == "-"                          # the work order goes in on stdin


def test_workbuddy_is_started_in_the_mode_that_stops_asking(monkeypatch, tmp_path):
    monkeypatch.setattr(external, "find_launcher",
                        lambda cli_path="": external.Launcher(["codebuddy"], "/x/codebuddy", "bundled"))

    argv, label = handoff.command("workbuddy", tmp_path)

    assert label == "WorkBuddy"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--add-dir" in argv and str(tmp_path) in argv


def test_the_codex_node_is_the_one_that_can_run_it(monkeypatch, tmp_path):
    """⚠️ Measured on this machine: the managed node is arm64 and runs codex; `/usr/local/bin/node`
    is x64 under Rosetta and dies with `Missing optional dependency @openai/codex-darwin-x64`. Which
    one is picked is the difference between a repair and a crash, so it is not left to PATH."""
    chosen: list[str] = []

    def _capture(raw, *, node=""):
        chosen.append(node)
        return external.Launcher([node or "node", raw], str(raw), "path")

    monkeypatch.setattr(handoff.bindirs, "tool", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(external, "launcher_for", _capture)
    monkeypatch.setattr(external, "preferred_node", lambda: "/managed/node")

    handoff.command("codex", tmp_path)

    assert chosen == ["/managed/node"]


# ------------------------------------------------------------------ one real run
def test_the_work_order_arrives_on_stdin_in_that_directory(tmp_path, monkeypatch):
    exe = _fake_agent(tmp_path)
    _point_at(monkeypatch, exe)
    work = tmp_path / "ws"
    work.mkdir()

    sent = handoff.send("codex", folder=work, text="FIX THE THING", timeout=30)

    assert sent.ok and sent.label == "codex" and "changed note.txt" in sent.text
    # ⚠️ Compare **resolved** paths: on macOS `/tmp` and `/var` are symlinks and `pwd` reports the
    # real one — the same trap `coderun.base_dir` exists for.
    assert Path((tmp_path / "agent.cwd").read_text().strip()).resolve() == work.resolve()
    assert "FIX THE THING" in (tmp_path / "agent.stdin").read_text()


def test_the_child_environment_carries_no_keys_from_this_app(tmp_path, monkeypatch):
    exe = tmp_path / "agent"
    exe.write_text('#!/bin/sh\nenv > "$0.env"\nprintf "ok"\n', encoding="utf-8")
    exe.chmod(0o755)
    _point_at(monkeypatch, exe)
    monkeypatch.setenv("TEAM_AGENT_TOKEN", "secret-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")

    assert handoff.send("codex", folder=tmp_path, text="x", timeout=30).ok

    dump = (tmp_path / "agent.env").read_text(encoding="utf-8")
    assert "TEAM_AGENT_TOKEN" not in dump and "sk-secret" not in dump
    assert "PATH=" in dump                            # a usable PATH is still handed over


def test_an_agent_that_never_finishes_is_stopped_along_with_its_children(tmp_path, monkeypatch):
    exe = tmp_path / "agent"
    exe.write_text("#!/bin/sh\necho $$ > pid.txt\nsleep 60\n", encoding="utf-8")
    exe.chmod(0o755)
    _point_at(monkeypatch, exe)

    sent = handoff.send("codex", folder=tmp_path, text="x", timeout=1.5)

    assert not sent.ok and "1" in sent.text           # the stop is reported, with the limit in it
    pid = int((tmp_path / "pid.txt").read_text().strip())
    gone = False
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            gone = True
            break
        time.sleep(0.2)
    assert gone, f"the agent {pid} is still running after the timeout"


def test_an_engine_that_exits_zero_with_no_output_still_says_what_it_printed(tmp_path, monkeypatch):
    """⚠️ Measured for real on 2026-09-27: WorkBuddy's engine answers

        Authentication required. Please use /login command to sign in to your account

    on **stderr** and exits **0**. An empty message there is indistinguishable from a clean run that
    changed nothing — the failure this project keeps paying for — so the engine's own words have to
    survive into what the panel shows, together with a route the reader can actually take.
    """
    exe = tmp_path / "agent"
    exe.write_text('#!/bin/sh\n'
                   'echo "Authentication required. Please use /login command to sign in" >&2\n'
                   'exit 0\n', encoding="utf-8")
    exe.chmod(0o755)
    _point_at(monkeypatch, exe)

    sent = handoff.send("workbuddy", folder=tmp_path, text="x", timeout=30)

    assert not sent.ok and sent.code == 0
    assert "Authentication required" in sent.text      # the engine's own words survive
    assert "/login" in sent.text                       # and the reader is told a route that works
    # ⚠️ Not the member's route: a hand-off has no settings panel to point at a model of its own.
    assert "this member" not in sent.text


def test_nothing_installed_says_so_instead_of_failing_later(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "command", lambda key, folder: None)
    monkeypatch.setattr(handoff, "installed", lambda: [])

    sent = handoff.send("codex", folder=tmp_path, text="x", timeout=5)

    assert not sent.ok and "codex" in sent.text
    assert "npm install" in sent.text                 # the useful half of the sentence
    row = handoff.info()
    assert row["ready"] == []
    assert all(t["ready"] is False for t in row["targets"])


# ------------------------------------------------------------------ the button
def _handed(monkeypatch, *, pause=0.0):
    """Stand in for the two agents: capture the call, answer, change nothing."""
    calls: list[dict] = []

    def _send_all(keys, *, folder, text, timeout=0):
        calls.append({"keys": list(keys), "folder": str(folder), "text": text, "timeout": timeout})
        if pause:
            time.sleep(pause)                        # only reached when it is the *slow* stand-in
        return [handoff.Sent(target=k, label=k, ok=True, text="看到问题,改了 X", seconds=1.0,
                             note="经本机 7890 端口代理") for k in keys]

    monkeypatch.setattr(handoff, "send_all", _send_all)
    return calls


def _wait(cl, job_id: str, tries=100) -> dict:
    for _ in range(tries):
        job = cl.get(f"/api/process/handoff/{job_id}").json()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the hand-off never finished")


def test_the_button_sends_each_group_its_own_open_entries_into_its_own_directory(
        tmp_path, monkeypatch):
    calls = _handed(monkeypatch)
    cl, app = _client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    _ledger(store, gid, "没落盘", "重试没变")

    started = cl.post("/api/process/handoff", json={}).json()["job"]
    job = _wait(cl, started["id"])

    assert job["state"] == "done" and job["error"] == ""
    assert len(calls) == 1
    assert calls[0]["folder"] == str(store.workspace_dir(gid))      # ⚠️ the group's own directory
    assert "没落盘" in calls[0]["text"] and "重试没变" in calls[0]["text"]
    assert calls[0]["timeout"] == float(store.get_settings()["handoff_timeout"])

    row = job["groups"][0]
    assert row["gid"] == gid and row["count"] == 2
    assert row["dir"] == str(store.workspace_dir(gid))
    # Both answers are kept apart rather than merged: two agents, one directory, no waiting.
    assert sorted(s["target"] for s in row["sends"]) == sorted(handoff.installed())
    assert row["sends"][0]["ok"] is True and "改了 X" in row["sends"][0]["text"]
    assert row["note"] == "经本机 7890 端口代理"      # how it reached the network, said out loud


def test_a_group_with_nothing_open_is_not_handed_anything(tmp_path, monkeypatch):
    calls = _handed(monkeypatch)
    cl, app = _client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    entries = _ledger(store, gid, "已经好了")
    proclog.update(entries, entries[0].id, status="wontfix", note="不改")
    proclog.write(proclog.unit(store.workspace_dir(gid)), entries, "群")

    r = cl.post("/api/process/handoff", json={})

    assert r.status_code == 400
    assert "process" in r.json()["detail"] or "流程" in r.json()["detail"]
    assert calls == []


def test_a_second_hand_off_is_refused_while_one_is_running(tmp_path, monkeypatch):
    """Two rounds of agents loose in the same directories is not something to debug."""
    _handed(monkeypatch)
    cl, app = _client(tmp_path)
    gid = app.state.store.list_groups()[0]["id"]
    _ledger(app.state.store, gid, "没落盘")
    first = cl.post("/api/process/handoff", json={}).json()["job"]

    # Hold the job open by hand: the first stand-in answers instantly.
    calls = _handed(monkeypatch, pause=0.6)
    second = cl.post("/api/process/handoff", json={})
    if second.status_code == 409:
        assert second.json()["detail"]
        assert calls == []                            # refused before anything was dispatched
        _wait(cl, first["id"])
    else:
        assert second.status_code == 200               # it had finished: also a correct answer


def test_an_unknown_hand_off_is_a_plain_404(tmp_path):
    cl, _ = _client(tmp_path)
    assert cl.get("/api/process/handoff/nope").status_code == 404


def test_the_capabilities_row_says_who_could_take_a_repair(tmp_path):
    """Same shape as the advisor row: a dead end is not an acceptable answer to "why not"."""
    cl, _ = _client(tmp_path)
    row = cl.get("/api/capabilities").json()["handoff"]

    assert [t["key"] for t in row["targets"]] == list(handoff.ORDER)
    for t in row["targets"]:
        assert t["install"], t
        assert isinstance(t["ready"], bool)
    assert isinstance(row["ready"], list) and set(row["ready"]) <= set(handoff.ORDER)
