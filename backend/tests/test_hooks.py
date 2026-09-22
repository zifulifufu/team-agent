"""Hooks: the six events, the gates that can only tighten, the subprocess runner, and what the
panel shows. The point of these tests is the *contract*, not the plumbing: a gate must never be
able to grant what the user forbade, a broken hook must not take the round down with it, and a
hook that is off must cost nothing.
"""

import asyncio
import json
import time

import pytest

from app import hooks as hooks_lib
from app import i18n
from app.hooks import HookManager, ensure_example_hooks
from app.orchestrator import Orchestrator
from tests.conftest import FakeLLM

# Observers write to a file of their own. The path is baked into the generated source rather than
# read from the environment: a hook runs with the trimmed environment `coderun` gives a member's
# program, so it cannot see the app's variables (`HOOK_TEST_FILE` included) — which is the point.
OBSERVER = '''
import json

def handle(event, payload):
    with open(%(path)r, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": event, "payload": payload}, ensure_ascii=False) + "\\n")
    return None
'''

GATE = '''
def handle(event, payload):
    if event != "pre_tool_use":
        return None
    if payload["tool"] == "current_time":
        return {"block": True, "reason": "not during a review"}
    if payload["tool"] == "library_search":
        return {"block": False, "args": {"group_id": "rewritten"}}
    return {"block": False}
'''

NOISY = '''
def handle(event, payload):
    print("this is not an answer, just noise")
    return {"block": True, "reason": "read the log, not my stdout"}
'''

FRIENDLY = '''
def handle(event, payload):
    return {"block": False}          # "I have no objection" — not "run it anyway"
'''

CRASHER = '''
import sys

def handle(event, payload):
    sys.exit(3)
'''

SLEEPER = '''
import time

def handle(event, payload):
    time.sleep(5)
    return None
'''

ECHOER = '''
def handle(event, payload):
    return {"block": False, "args": dict(payload.get("args") or {})}
'''

OUTBOUND = '''
def handle(event, payload):
    if "secret" in payload["text"]:
        return {"block": True, "reason": "that looks like a credential"}
    return {"block": False, "text": payload["text"] + " (checked)"}
'''


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_hook(data, hid, code, *, events=("round.end",), enabled=True, timeout_ms=1500,
              on_error="auto", groups=None):
    folder = data / "hooks" / hid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "hook.py").write_text(code, encoding="utf-8")
    (folder / "HOOK.json").write_text(json.dumps({
        "name": hid, "events": list(events), "enabled": enabled,
        "timeout_ms": timeout_ms, "on_error": on_error, "groups": list(groups or []),
    }), encoding="utf-8")
    return folder


def observer(data, hid="watcher", **kw):
    """An observer that records every event it is handed."""
    return make_hook(data, hid, OBSERVER % {"path": str(data / "seen.jsonl")}, **kw)


def seen(data):
    path = data / "seen.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def manager(data):
    m = HookManager(data)
    m.load()
    return m


class Collector:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, ev):
        self.events.append(ev)


def run_round(orch, gid, text="在吗"):
    """One round and its observers, in a single loop: `notify` schedules tasks on the running
    loop, so draining has to happen before that loop closes."""
    got = Collector()

    async def main():
        await orch.handle_user_message(gid, text, got)
        await orch.hooks.drain()

    asyncio.run(main())
    return got


def orchestrator(store, make_router, data, text="好的。", hooks=True):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    m = manager(data) if hooks else None
    return Orchestrator(store, make_router(FakeLLM(default=text)), hooks=m), m


# ------------------------------------------------------------------ the example
def test_the_example_hook_is_written_once_and_starts_off(data):
    """It exists so the panel has something to show — and it is off, because a hook is code
    running on this machine."""
    ensure_example_hooks(data)
    m = manager(data)
    hook = m.hooks["example-round-log"]
    assert hook.events == ["round.end"] and hook.enabled is False
    assert "def handle(event, payload)" in hook.path.read_text(encoding="utf-8")

    hook.path.write_text("# mine now\n", encoding="utf-8")
    ensure_example_hooks(data)                                     # must not overwrite an edit
    assert "mine now" in hook.path.read_text(encoding="utf-8")


def test_a_hook_that_is_off_costs_nothing(data, store, make_router):
    observer(data, enabled=False)
    orch, m = orchestrator(store, make_router, data)
    assert m.any_enabled() is False
    assert m._for("round.end", "g1") == []

    run_round(orch, store.list_groups()[0]["id"])
    assert seen(data) == []


# ------------------------------------------------------------------ observers
def test_round_start_and_round_end_reach_an_observer(data, store, make_router):
    observer(data, events=("round.start", "round.end"))
    orch, m = orchestrator(store, make_router, data)
    gid = store.list_groups()[0]["id"]

    run_round(orch, gid)

    assert [row["event"] for row in seen(data)] == ["round.start", "round.end"], seen(data)
    start, end = seen(data)[0]["payload"], seen(data)[1]["payload"]
    assert start["group_id"] == gid and start["chars"] == 2
    assert end["entries"] >= 1 and end["seconds"] >= 0 and end["answer_chars"] > 0


def test_a_member_reply_reaches_an_observer_with_who_and_what(data, store, make_router):
    observer(data, events=("agent.reply",))
    orch, _ = orchestrator(store, make_router, data, text="收到。")

    run_round(orch, store.list_groups()[0]["id"])

    rows = [row["payload"] for row in seen(data) if row["event"] == "agent.reply"]
    assert rows, "no agent.reply event"
    assert rows[0]["agent"] and rows[0]["model"] and rows[0]["chars"] > 0


def test_a_hook_is_only_handed_the_event_it_asked_for(data, store, make_router):
    observer(data, events=("agent.reply",))
    orch, _ = orchestrator(store, make_router, data)

    run_round(orch, store.list_groups()[0]["id"])
    assert {row["event"] for row in seen(data)} == {"agent.reply"}


# ------------------------------------------------------------------ gates
def test_a_gate_can_block_a_tool_call(store, data, make_router):
    """A gate says `block` and the call does not happen — and the member is told why, so it does
    not simply retry the same thing."""
    make_hook(data, "gate", GATE, events=("pre_tool_use",))
    orch, _ = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))
    assert "current_time" in ctx.tools

    out = asyncio.run(orch.toolhub.call(ctx, "current_time", {}))
    assert out.ok is False and out.denied is True
    assert "gate" in out.text and "not during a review" in out.text


def test_a_gate_can_rewrite_the_arguments(store, data, make_router):
    make_hook(data, "gate", GATE, events=("pre_tool_use",))
    orch, _ = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))
    ctx.tools["library_search"] = {"name": "library_search", "description": "", "risk": "read",
                                   "parameters": {}, "source": "builtin"}
    seen_args: list[dict] = []

    async def fake_dispatch(ctx_, spec, args, timeout):
        seen_args.append(dict(args))
        return "done", True, []

    orch.toolhub._dispatch = fake_dispatch            # type: ignore[assignment]
    out = asyncio.run(orch.toolhub.call(ctx, "library_search", {"query": "x"}))
    assert out.ok is True
    assert seen_args == [{"query": "x", "group_id": "rewritten"}]


def test_a_gate_cannot_hand_out_what_the_user_forbade(store, data, make_router):
    """The security property of the whole feature: the user's own rules are consulted first, and
    a hook has no vocabulary for granting anything. Other people's hook files get copied around."""
    make_hook(data, "friendly", FRIENDLY, events=("pre_tool_use",))
    store.update_settings({"perm_deny": ["current_time"]})
    orch, _ = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))

    out = asyncio.run(orch.toolhub.call(ctx, "current_time", {}))
    assert out.ok is False and out.denied is True
    assert "Permissions" in out.text or "权限" in out.text, out.text


def test_a_broken_gate_lets_reads_through_and_holds_writes(store, data, make_router):
    """`on_error: auto` — the same instinct as `perm_mode`: a gate that cannot judge stops what
    writes, and does not stop what only reads."""
    make_hook(data, "broken", CRASHER, events=("pre_tool_use",))
    orch, m = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))
    ctx.tools["writer"] = {"name": "writer", "description": "", "risk": "write",
                           "parameters": {}, "source": "plugin"}

    read = asyncio.run(orch.toolhub.call(ctx, "current_time", {}))
    assert read.ok is True, read.text                      # read: a broken gate does not block it
    write = asyncio.run(orch.toolhub.call(ctx, "writer", {}))
    assert write.ok is False and "broken" in write.text
    assert m.hooks["broken"].last["ok"] is False and "3" in m.hooks["broken"].last["note"]


def test_on_error_can_be_told_to_fail_closed_even_for_reads(store, data, make_router):
    make_hook(data, "strict", NOISY, events=("pre_tool_use",), on_error="closed")
    orch, _ = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))

    # The hook's own stdout noise is not the answer: it blocked, so the call is refused.
    out = asyncio.run(orch.toolhub.call(ctx, "current_time", {}))
    assert out.ok is False and "strict" in out.text and "read the log" in out.text


def test_a_gate_never_sees_a_credential(store, data, make_router):
    """A gate is shown the real arguments (it has to be able to read the program it judges) but
    never a credential-named field, so a copied hook cannot harvest one."""
    make_hook(data, "echo", ECHOER, events=("pre_tool_use",))
    orch, _ = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))
    ctx.tools["probe"] = {"name": "probe", "description": "", "risk": "read",
                          "parameters": {}, "source": "plugin"}
    got: list[dict] = []

    async def fake_dispatch(ctx_, spec, args, timeout):
        got.append(dict(args))
        return "done", True, []

    orch.toolhub._dispatch = fake_dispatch            # type: ignore[assignment]
    asyncio.run(orch.toolhub.call(ctx, "probe", {"api_key": "sk-live-1234567890", "code": "print(1)"}))

    # The call itself is untouched (the hook only echoed what it was shown).
    assert got[0]["api_key"] == "sk-live-1234567890"
    assert got[0]["code"] == "print(1)"


def test_a_gate_is_shown_a_credential_free_view_of_the_arguments(store, data, make_router):
    make_hook(data, "spy", ('''
import json

def handle(event, payload):
    with open(%(path)r, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload["args"]) + "\\n")
    return {"block": False}
''') % {"path": str(data / "args.jsonl")}, events=("pre_tool_use",))
    orch, _ = orchestrator(store, make_router, data)
    group = store.list_groups()[0]
    ctx = asyncio.run(orch.toolhub.context(group, store.list_agents()[0]))
    ctx.tools["probe2"] = {"name": "probe2", "description": "", "risk": "read",
                           "parameters": {}, "source": "plugin"}

    asyncio.run(orch.toolhub.call(ctx, "probe2", {"token": "abc123", "command": "ls -la"}))

    shown = json.loads((data / "args.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert shown["token"] == "…" and shown["command"] == "ls -la"


def test_outgoing_text_can_be_held_back_or_rewritten(store, data):
    make_hook(data, "outbound", OUTBOUND, events=("before_send",))
    m = manager(data)

    blocked, text = asyncio.run(m.gate_outgoing("g1", "here is a secret"))
    assert blocked and "credential" in blocked and text == "here is a secret"
    reason, rewritten = asyncio.run(m.gate_outgoing("g1", "all clear"))
    assert reason == "" and rewritten == "all clear (checked)"


def test_a_message_a_hook_holds_back_is_not_sent(store, data, monkeypatch):
    """The channel layer asks once, before anything leaves the machine: a hook that objects or
    rewrites decides what actually goes out, and a held-back message does not go at all."""
    import app.api_channels as api_channels

    gid = store.list_groups()[0]["id"]
    store.update_settings({"wecom_enabled": True, "wecom_group_id": gid, "wecom_on_answer": True,
                           "wecom_webhook_url": "https://example.com/robot"})
    sent: list[tuple] = []

    async def fake_send(cid, cfg, to, body, **kw):
        sent.append((cid, to, body))
        return True, "sent"

    monkeypatch.setattr(api_channels.channels, "send", fake_send)
    chan = api_channels.Channels(store, orch=None, hub=None)

    # Without the hook, the text goes out as it is.
    asyncio.run(chan.push_answer(gid, "plain message"))
    assert sent and sent[0][2] == "plain message", sent

    make_hook(data, "outbound", OUTBOUND, events=("before_send",))
    chan.hooks = manager(data)
    sent.clear()

    asyncio.run(chan.push_answer(gid, "here is a secret"))
    assert sent == [], "the hook said no, so nothing may go out"

    asyncio.run(chan.push_answer(gid, "all clear"))
    assert sent and sent[0][2].endswith("(checked)"), sent


def test_a_hook_that_hangs_is_killed_and_the_round_goes_on(data, store, make_router):
    """A hook that never returns is cut off at its own timeout and recorded — somebody's script
    must not hold the group hostage."""
    observer(data, "slow", enabled=True)
    make_hook(data, "slow", SLEEPER, events=("round.end",), timeout_ms=300)
    orch, m = orchestrator(store, make_router, data)

    t0 = time.time()
    run_round(orch, store.list_groups()[0]["id"])
    assert time.time() - t0 < 4.0, "the hook's timeout did not cut it short"

    hook = m.hooks["slow"]
    assert hook.last["ok"] is False and "300" in hook.last["note"], hook.last


def test_a_hook_can_be_limited_to_one_group(data, store, make_router):
    other = store.create_group("另一个群")
    here = store.list_groups()[0]["id"]
    observer(data, groups=[here])
    m = manager(data)
    assert [h.id for h in m._for("round.end", here)] == ["watcher"]
    assert m._for("round.end", other["id"]) == []


def test_two_hooks_run_in_a_predictable_order(store, data):
    observer(data, "b-second", events=("round.end",))
    observer(data, "a-first", events=("round.end",))
    m = manager(data)
    assert [h.id for h in m._for("round.end", "g1")] == ["a-first", "b-second"]


# ------------------------------------------------------------------ bookkeeping
def test_a_hook_that_asks_for_an_event_nobody_has_is_reported_not_silently_dropped(data):
    observer(data, "typo", events=("round.finished",))
    m = manager(data)
    hook = m.hooks["typo"]
    assert hook.error and "no event it knows about" in hook.error
    assert m._for("round.end", "g1") == []            # and it is never run


def test_a_folder_without_both_files_is_listed_as_an_error(data):
    (data / "hooks" / "half").mkdir(parents=True, exist_ok=True)
    (data / "hooks" / "half" / "hook.py").write_text("def handle(e, p): return None\n", encoding="utf-8")
    m = manager(data)
    assert m.hooks == {} and any("half" in e for e in m.errors)


def test_the_log_says_what_happened(data):
    observer(data)
    m = manager(data)
    asyncio.run(m._observe(m.hooks["watcher"], "round.end", "g1", {"entries": 2}))

    rows = m.recent(10)
    assert rows and rows[0]["hook"] == "watcher" and rows[0]["event"] == "round.end" and rows[0]["ok"] is True
    assert seen(data)[0]["event"] == "round.end"


def test_the_panel_can_run_one_hook_on_demand(data):
    """'It is installed' and 'it works' are different claims; the panel must be able to show the
    second one."""
    observer(data, enabled=False)
    m = manager(data)
    result = asyncio.run(m.test("watcher", "round.end", {"group_id": "g1", "entries": 1}))
    assert result["ok"] is True
    assert m.recent(5)[0]["tool"] == "(test)"


def test_the_timeout_a_hook_asks_for_is_capped(data):
    observer(data, timeout_ms=600_000)
    assert manager(data).hooks["watcher"].timeout_ms == hooks_lib.MAX_TIMEOUT_MS


# ------------------------------------------------------------------ the API
def test_the_endpoints_list_switch_reload_and_show_source(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"), background=False)
    c = TestClient(app, base_url="http://127.0.0.1")

    listing = c.get("/api/hooks").json()
    assert [h["id"] for h in listing["hooks"]] == ["example-round-log"] and listing["guide"]

    turned_on = c.patch("/api/hooks/example-round-log", json={"enabled": True}).json()["hook"]
    assert turned_on["enabled"] is True
    spec = json.loads((tmp_path / "data" / "hooks" / "example-round-log" / "HOOK.json").read_text())
    assert spec["enabled"] is True

    assert c.get("/api/hooks/nope/source").status_code == 404
    assert c.patch("/api/hooks/nope", json={"enabled": True}).status_code == 404
    assert "def handle" in c.get("/api/hooks/example-round-log/source").json()["content"]

    # a new folder needs no restart
    observer(tmp_path / "data", "added-by-hand")
    assert c.post("/api/hooks/reload").json()["hooks"]
    assert {h["id"] for h in c.get("/api/hooks").json()["hooks"]} == {"example-round-log", "added-by-hand"}

    assert c.post("/api/hooks/example-round-log/test", json={"event": "round.end"}).json()["ok"] is True
    entries = c.get("/api/hooks/log?limit=5").json()["entries"]
    assert entries and entries[0]["hook"] == "example-round-log"


def test_the_api_refuses_an_unknown_event_and_an_unknown_group(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"), background=False)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.post("/api/hooks/example-round-log/test", json={"event": "nope"}).status_code == 422
    assert c.patch("/api/hooks/example-round-log", json={"groups": ["ghost"]}).status_code == 422


def test_a_run_note_is_stored_in_one_language_and_shown_in_the_readers(data):
    """The log outlives the request that wrote it, so the note is stored in one language; the
    panel renders it in whatever language the reader is using. A hook's own wording is not ours
    to translate and is passed through."""
    from app.hooks import localize_note

    assert localize_note("did not answer within 300 ms", "zh") == "超过 300 毫秒没有回应"
    assert localize_note("exited with code 4", "zh") == "退出码 4"
    assert localize_note("could not be started: [Errno 8] Exec format error", "zh").startswith("启动失败:")
    assert localize_note("did not answer within 300 ms", "en") == "did not answer within 300 ms"
    assert localize_note("smoke test said no", "zh") == "smoke test said no"

    observer(data)
    m = manager(data)
    asyncio.run(m._observe(m.hooks["watcher"], "round.end", "g1", {"entries": 1}))
    m.log("watcher", "tool.called", "g1", "exited with code 3", None)
    with i18n.pinned("zh"):                                     # the log is read in Chinese here
        assert m.recent(1)[0]["note"] == "退出码 3"
    assert m.recent(1)[0]["note"] == "exited with code 3"       # and in English by default
    raw = (data / "hook-log.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert "exited with code 3" in raw                          # stored canonically
