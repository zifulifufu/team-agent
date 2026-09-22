"""Permissions and control: tool approval (ask / allow / deny / timeout / always
allow / forbidden), risk tiers, endpoint validation.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.approvals import policy_for, risk_of
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_collab import Collector, call, last_user, setup, write_plugin


def script(messages):
    if "<tool_result" in last_user(messages):
        return "收到结果"
    return "我来处理。" + call("shout", text="hi")


async def wait_for(pred, timeout=3.0):
    t0 = asyncio.get_running_loop().time()
    while not pred():
        assert asyncio.get_running_loop().time() - t0 < timeout, "等待超时"
        await asyncio.sleep(0.01)


def pending(c):
    return [e for e in c.events if e["type"] == "approval"]


def statuses(c):
    return [e["call"]["status"] for e in c.events if e["type"] == "tool"]


def prep(store, make_router, **cfg):
    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake, perm_mode="ask_risky", **cfg)
    write_plugin(store, orch)
    store.update_group(g["id"], {"ext": {"plugins": ["demo"]}})
    return orch, g, fake


async def test_plugin_call_waits_for_approval_then_runs(store, make_router):
    orch, g, fake = prep(store, make_router)
    c = Collector()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "@Copywriter 大写 hi", c))
    await wait_for(lambda: pending(c))
    ap = pending(c)[0]["approval"]
    assert ap["tool"] == "shout" and ap["risk"] == "exec" and ap["agent"] == "Copywriter" and ap["args"] == {"text": "hi"}
    assert orch.approvals.list(g["id"])[0]["id"] == ap["id"]
    assert len(fake.calls) == 1                                    # not approved yet: the tool never ran and the model got no result
    assert orch.approvals.resolve(ap["id"], True)
    await task
    msg = c.ends()[0]
    assert msg["meta"]["tools"][0]["status"] == "ok" and msg["meta"]["tools"][0]["preview"] == "HI"
    assert statuses(c) == ["running", "waiting", "running", "ok"]
    assert [e["decision"] for e in c.events if e["type"] == "approval_done"] == ["allow"]
    assert orch.approvals.list() == []
    assert not orch.approvals.resolve(ap["id"], True)              # a settled request cannot be answered twice


async def test_denied_call_is_not_executed_and_model_is_told(store, make_router):
    orch, g, fake = prep(store, make_router)
    c = Collector()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "@Copywriter 大写 hi", c))
    await wait_for(lambda: pending(c))
    orch.approvals.resolve(pending(c)[0]["approval"]["id"], False)
    await task
    tr = c.ends()[0]["meta"]["tools"][0]
    assert tr["status"] == "denied" and "HI" not in tr["preview"]
    assert "did not approve this call" in last_user(fake.calls[1][1]) and 'ok="false"' in last_user(fake.calls[1][1])


async def test_timeout_counts_as_deny(store, make_router):
    orch, g, _ = prep(store, make_router, perm_timeout=0.05)
    c = Collector()
    await orch.handle_user_message(g["id"], "@Copywriter 大写 hi", c)
    assert c.ends()[0]["meta"]["tools"][0]["status"] == "denied"
    assert [e["decision"] for e in c.events if e["type"] == "approval_done"] == ["timeout"]
    assert orch.approvals.list() == []


async def test_allow_always_remembers_and_deny_list_wins(store, make_router):
    orch, g, _ = prep(store, make_router)
    c = Collector()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "@Copywriter 大写 hi", c))
    await wait_for(lambda: pending(c))
    orch.approvals.resolve(pending(c)[0]["approval"]["id"], True, remember=True)
    await task
    assert store.get_settings()["perm_allow"] == ["shout"]
    c2 = Collector()
    await orch.handle_user_message(g["id"], "@Copywriter 再大写一次", c2)     # no prompt this time
    assert not pending(c2) and c2.ends()[0]["meta"]["tools"][0]["status"] == "ok"
    store.update_settings({"perm_deny": ["shout"], "perm_mode": "allow_all"})
    c3 = Collector()
    await orch.handle_user_message(g["id"], "@Copywriter 又一次", c3)          # the deny list beats everything
    assert not pending(c3) and c3.ends()[0]["meta"]["tools"][0]["status"] == "denied"


async def test_stop_while_waiting_cleans_up(store, make_router):
    orch, g, _ = prep(store, make_router)
    c = Collector()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "@Copywriter 大写 hi", c))
    await wait_for(lambda: pending(c))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert orch.approvals.list() == []
    assert [e["decision"] for e in c.events if e["type"] == "approval_done"] == ["cancelled"]


async def test_allow_all_mode_and_read_tools_do_not_ask(store, make_router):
    orch, g, _ = prep(store, make_router)
    store.update_settings({"perm_mode": "allow_all"})
    c = Collector()
    await orch.handle_user_message(g["id"], "@Copywriter 大写 hi", c)
    assert not pending(c) and c.ends()[0]["meta"]["tools"][0]["status"] == "ok"


def test_policy_matrix():
    base = {"perm_mode": "ask_risky", "perm_allow": [], "perm_deny": []}
    plug = {"name": "p", "source": "plugin"}
    mcp_ro = {"name": "m1", "source": "mcp", "read_only": True}
    mcp_rw = {"name": "m2", "source": "mcp", "read_only": False}
    # Built-ins carry their tier with them now, so the specs here look like the real ones
    mem = {"name": "memory_save", "source": "builtin", "risk": "write"}
    lib = {"name": "library_search", "source": "builtin", "risk": "read"}
    now = {"name": "current_time", "source": "builtin", "risk": "read"}
    assert [risk_of(s) for s in (plug, mcp_ro, mcp_rw, mem, lib)] == ["exec", "exec", "exec", "write", "read"]
    # …and a spec that somehow arrives without one counts as exec, never as read
    assert risk_of({"name": "memory_save", "source": "builtin"}) == "exec"
    # An MCP tool is `exec` whatever its server says about it: `readOnlyHint` is the server's own
    # annotation, and this is the decision that decides whether anything is shown to the user.
    assert [policy_for(base, s) for s in (plug, mcp_ro, mcp_rw, mem, lib, now)] == ["ask", "ask", "ask", "allow", "allow", "allow"]
    # The way to stop being asked about a read-only server is to allow that tool, once, by hand
    assert policy_for({**base, "perm_allow": ["m1"]}, mcp_ro) == "allow"
    ask_all = {**base, "perm_mode": "ask_all"}
    assert [policy_for(ask_all, s) for s in (plug, mcp_ro, mem, lib, now)] == ["ask", "ask", "ask", "ask", "allow"]
    assert policy_for({**ask_all, "perm_allow": ["p"]}, plug) == "allow"
    assert policy_for({**base, "perm_mode": "allow_all", "perm_deny": ["p"]}, plug) == "deny"


def test_builtin_risk_comes_from_the_spec_not_the_name():
    """A built-in that runs something must not be advertised as read-only.

    Two consumers read the risk: dispatch (through the per-call spec) and the Permissions
    page. They have to agree, so the value lives in BUILTIN_SPECS and both read it from
    there. The name-based fallback files anything unknown under "read", which is how
    `run_code` would have been shown as safe while actually asking (or, once "always
    allowed", running without asking).
    """
    from app.toolhub import builtin_specs

    specs = builtin_specs()
    assert risk_of({**specs["run_code"], "name": "run_code", "source": "builtin"}) == "exec"
    assert risk_of({**specs["memory_save"], "name": "memory_save", "source": "builtin"}) == "write"
    assert risk_of({**specs["library_search"], "name": "library_search", "source": "builtin"}) == "read"
    # A spec built without the field (MCP, plugin, anything hand-made) fails *closed*: guessing
    # "read" would mean a tool that runs something gets allowed without asking.
    assert risk_of({"name": "something_new", "source": "builtin"}) == "exec"
    assert risk_of({"name": "run_code", "source": "plugin"}) == "exec"


def test_an_unknown_risk_word_is_treated_as_exec():
    """`policy_for` asks for approval by comparing the tier against "exec".

    So a tier spelled any other way — a typo, a plugin declaring `"execute"`, `"safe"` or
    `"medium"` — used to fall through to "allow" and run without asking, in the default mode,
    while the docstring claimed the opposite. Anything unrecognised must mean exec.
    """
    cfg = {"perm_mode": "ask_risky", "perm_allow": [], "perm_deny": []}
    for declared in ("execute", "EXEC", "safe", "medium", "ex", 1, True):
        spec = {"name": "p", "source": "plugin", "risk": declared}
        assert risk_of(spec) == "exec", declared
        assert policy_for(cfg, spec) == "ask", declared
    # …and the three real tiers keep working
    for declared, want in (("read", "allow"), ("write", "allow"), ("exec", "ask")):
        spec = {"name": "p", "source": "plugin", "risk": declared}
        assert policy_for(cfg, spec) == want, declared


def test_every_builtin_declares_its_own_risk_level():
    """The tier must never be inferred from a tool's name.

    It used to be "everything is read-only except memory_save", so adding a built-in that runs
    a program silently gave it the read-only tier — allowed without asking under the default
    permission mode. Every built-in now states its own tier, and this fails when one is added
    without it (which is the moment to decide, not later).
    """
    from app.toolhub import BUILTIN_SPECS

    missing = [name for name, spec in BUILTIN_SPECS.items() if spec.get("risk") not in ("read", "write", "exec")]
    assert missing == [], f"built-in tools without an explicit risk tier: {missing}"


def test_every_setting_can_actually_be_written(tmp_path):
    """A setting the API refuses to store is a control that silently does nothing.

    PUT /api/settings falls back to `isinstance(value, str)` for anything it does not know
    about, so adding an int or bool to DEFAULT_SETTINGS without also listing it in RANGES (or
    ENUMS) makes it read-only on the wire while the UI happily offers to change it.
    """
    from app.presets import DEFAULT_SETTINGS
    from tests.conftest import FakeLLM

    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    c = TestClient(app, base_url="http://127.0.0.1")
    # Values that cannot be written back by design: derived from the request language, or
    # write-only secrets, or set through a dedicated endpoint.
    skip = {"obsidian_dir", "obsidian_auto", "github_token", "app_repo", "catalog_url",
            "system_prompt", "route_chain", "perm_allow", "perm_deny"}
    refused = []
    for key, value in DEFAULT_SETTINGS.items():
        if key in skip or isinstance(value, str):
            continue
        r = c.put("/api/settings", json={key: value})
        if r.status_code != 200:
            refused.append((key, type(value).__name__, r.status_code))
    assert refused == [], f"settings the API will not store: {refused}"


def test_api_permissions_and_validation(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    c = TestClient(app, base_url="http://127.0.0.1")
    p = c.get("/api/permissions").json()
    assert p["mode"] == "ask_risky" and p["timeout"] == 120 and p["allow"] == [] and p["deny"] == []
    names = {t["name"]: t for t in p["tools"]}
    assert names["current_time"]["policy"] == "allow" and names["memory_save"]["risk"] == "write"
    # What the page reports has to equal what the dispatcher does, else the user is told a
    # tool is read-only while it asks (or runs) like an exec tool.
    assert names["run_code"]["risk"] == "exec" and names["run_code"]["policy"] == "ask"
    assert p["access"]["external_calls"] is True and p["access"]["data_dir"]
    assert c.put("/api/settings", json={"perm_mode": "yolo"}).status_code == 400
    assert c.put("/api/settings", json={"perm_timeout": 5}).status_code == 400
    assert c.put("/api/settings", json={"perm_allow": "shout"}).status_code == 400
    assert c.put("/api/settings", json={"perm_allow": ["a", "a", "b"], "perm_mode": "ask_all"}).json()["perm_allow"] == ["a", "b"]
    assert c.post("/api/approvals/nope", json={"decision": "allow"}).status_code == 404
    assert c.post("/api/approvals/nope", json={"decision": "maybe"}).status_code == 400
    assert c.get("/api/approvals").json() == []


# ------------------------------------------------ MCP import / context management
def test_mcp_json_import_parse_and_add(tmp_path):
    import json

    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    c = TestClient(app, base_url="http://127.0.0.1")
    text = json.dumps({"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], "env": {"TOKEN": "s3cret"}},
        "remote": {"type": "streamable-http", "url": "https://x.example/mcp", "headers": {"Authorization": "Bearer S3CRET"}},
        "off": {"command": "uvx", "args": ["mcp-server-time"], "disabled": True},
        "bad1": {"args": ["x"]}, "bad2": "str", "bad3": {"url": "ftp://x"},
    }})
    p = c.post("/api/mcp/import/parse", json={"text": text}).json()
    assert [s["name"] for s in p["servers"]] == ["fs", "remote", "off"] and len(p["warnings"]) == 3
    assert "s3cret" not in json.dumps(p) and "S3CRET" not in json.dumps(p)          # no secrets in the preview
    assert p["servers"][1]["transport"] == "http" and p["servers"][2]["enabled"] is False
    r = c.post("/api/mcp/import", json={"text": text, "names": ["fs", "remote"]}).json()
    assert [m["name"] for m in r["added"]] == ["fs", "remote"] and r["skipped"] == []
    from app.store import Store

    real = {m["name"]: m for m in Store(tmp_path / "data").list_mcp()}
    assert real["fs"]["env"] == {"TOKEN": "s3cret"} and real["remote"]["headers"]["Authorization"] == "Bearer S3CRET"
    r2 = c.post("/api/mcp/import", json={"text": text}).json()                        # names already present are skipped, not duplicated
    assert r2["skipped"] == ["fs", "remote"] and [m["name"] for m in r2["added"]] == ["off"] and r2["added"][0]["enabled"] is False
    for bad in ("nope", "[1]", "{}", '{"mcpServers": {}}'):
        assert c.post("/api/mcp/import/parse", json={"text": bad}).status_code == 400
    # three shorthand spellings
    assert len(c.post("/api/mcp/import/parse", json={"text": '{"a": {"command": "x"}}'}).json()["servers"]) == 1
    assert c.post("/api/mcp/import/parse", json={"text": '{"command": "x"}'}).json()["servers"][0]["name"] == "x"


def test_history_clip_and_tool_output_limit(store, make_router):
    from app.orchestrator import Orchestrator, clip_middle

    assert clip_middle("a" * 100, 200) == "a" * 100
    out = clip_middle("头" * 50 + "中" * 400 + "尾" * 50, 100)
    assert out.startswith("头" * 50) and out.endswith("尾" * 30) and "400 characters omitted in the middle" in out
    orch = Orchestrator(store, make_router(FakeLLM(default="ok")))
    g = store.list_groups()[0]
    agent = store.list_agents()[0]
    # the label on a user line follows the interface language (the orchestrator calls
    # i18n.pick_now("me", <zh>))
    store.add_message(g["id"], "user", None, "me", "开头" + "很长" * 3000 + "结尾")
    store.add_message(g["id"], "user", None, "me", "最新一条:" + "全文" * 800)
    store.update_settings({"history_clip": 500})
    msgs = orch.build_messages(g, agent, [agent])
    body = msgs[-1]["content"]
    assert "omitted in the middle" in body and body.count("全文") == 800           # older messages get clipped, the newest stays whole
    assert body.startswith("[me] 开头")


def test_a_servers_own_read_only_claim_does_not_skip_the_approval():
    """`readOnlyHint` is an annotation the MCP *server* writes about its own tool, and this is the
    decision that decides whether anything is put in front of the user at all. Trusting it meant a
    server could label something that writes or sends as read-only and have it run unattended."""
    base = {"perm_mode": "ask_risky", "perm_allow": [], "perm_deny": []}
    claiming = {"name": "mcp__notes__delete_all", "source": "mcp", "read_only": True,
                "server_id": "srv1", "tool": "delete_all"}
    assert risk_of(claiming) == "exec"
    assert policy_for(base, claiming) == "ask"
    # "always allow" is the user's decision, and it is what makes the prompt stop
    assert policy_for({**base, "perm_allow": ["mcp__notes__delete_all"]}, claiming) == "allow"


def test_deleting_an_mcp_server_takes_its_always_allow_entries_with_it(tmp_path):
    """An allow entry names a tool, and an MCP tool's name comes from the server's display name.
    Left behind, the entries would be inherited by whatever server is added under that name next
    — a permission the user granted to a tool they read, silently applied to another one."""
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    with TestClient(app, base_url="http://127.0.0.1") as cl:
        srv = cl.post("/api/mcp", json={"name": "notes", "command": "npx", "args": ["-y", "notes"]}).json()
        cl.put("/api/settings", json={"perm_allow": ["mcp__notes__read", "library_search"]})
        cl.delete(f"/api/mcp/{srv['id']}")
        left = cl.get("/api/settings").json()["perm_allow"]
    assert left == ["library_search"], "the deleted server's permission outlived it"


@pytest.mark.parametrize("host,want", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
    ("0.0.0.0", False), ("192.168.1.212", False), ("example.com", False), ("", False),
])
def test_only_a_loopback_bind_counts_as_this_machine_only(host, want):
    """The warning that fires when the API is put on the network without a token rests on this
    predicate — and the obvious one to reuse, `net.is_local_url`, answers the proxy question
    instead and calls `0.0.0.0` and the LAN ranges local."""
    from app.__main__ import loopback_only
    assert loopback_only(host) is want
