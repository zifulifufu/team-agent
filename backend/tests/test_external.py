"""External agent members (WorkBuddy joining the group chat through the CodeBuddy CLI
engine): config validation, CLI arguments, stream parsing, subprocess handling,
orchestration and endpoints.

The real codebuddy is never started in these tests - tests/codebuddy_fake.py emits the
stream-json shape from the documentation instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import external
from app.external import ExternalError, ExternalRunner, StreamParser, build_args, build_env, clean_cfg, find_launcher
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_collab import Collector, setup

FAKE = str(Path(__file__).parent / "codebuddy_fake.py")


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_AGENT_CODEBUDDY", FAKE)
    log = tmp_path / "fake.log"
    monkeypatch.setenv("CODEBUDDY_FAKE_LOG", str(log))
    monkeypatch.setenv("CODEBUDDY_FAKE_MODE", "ok")
    return log


def make_agent(store, **cfg):
    return store.create_agent("WorkBuddy", "🧰", "外部智能体", "p", None, [], ["tool-use"], engine="workbuddy",
                              engine_cfg=clean_cfg(cfg))


# ---------------------------------------------------------- config validation
def test_clean_cfg_defaults_and_validation(tmp_path):
    c = clean_cfg({})
    assert c["level"] == "read" and c["web"] is False and c["handoff"] is True and c["risk_ack"] is False
    with pytest.raises(ValueError):
        clean_cfg({"level": "root"})
    with pytest.raises(ValueError, match="risk has to be acknowledged"):
        clean_cfg({"level": "full"})
    assert clean_cfg({"level": "full", "risk_ack": True, "cwd": str(tmp_path)})["risk_ack"] is True
    # dropping back from full to another level clears the acknowledgement; going back
    # to full requires confirming again
    back = clean_cfg({"level": "read"}, clean_cfg({"level": "full", "risk_ack": True}))
    assert back["risk_ack"] is False
    with pytest.raises(ValueError):
        clean_cfg({"level": "full"}, back)
    for bad in ({"cwd": "relative/dir"}, {"cwd": str(tmp_path / "nope")}, {"timeout": 5}, {"timeout": True},
                {"max_turns": 0}, {"model": "a b;rm -rf"}, {"web": "yes"}, {"add_dirs": [str(tmp_path)] * 6 + ["x"] * 3},
                {"cli_path": str(tmp_path / "missing")}):
        with pytest.raises(ValueError):
            clean_cfg(bad)
    other = tmp_path / "evil.sh"
    other.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="codebuddy"):
        clean_cfg({"cli_path": str(other)})


def test_edit_and_full_refuse_root_or_home_as_workdir():
    with pytest.raises(ValueError, match="home directory"):
        clean_cfg({"level": "edit", "cwd": str(Path.home())})
    with pytest.raises(ValueError):
        clean_cfg({"level": "edit", "cwd": "/"})
    assert clean_cfg({"level": "read", "cwd": str(Path.home())})["cwd"]      # allowed at the read-only level


# ----------------------------------------------- CLI arguments are the permissions
def opt(args, name):
    return args[args.index(name) + 1]


def test_permission_args_per_level():
    read = build_args(clean_cfg({}), "SYS")
    assert opt(read, "--permission-mode") == "dontAsk"
    assert opt(read, "--allowedTools") == "Read,Grep,Glob"
    dis = opt(read, "--disallowedTools").split(",")
    assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch"} <= set(dis)
    assert "--strict-mcp-config" in read and "-p" in read and "-y" not in read
    assert opt(read, "--append-system-prompt") == "SYS"

    edit = build_args(clean_cfg({"level": "edit"}))
    assert opt(edit, "--permission-mode") == "acceptEdits"
    assert "Edit" in opt(edit, "--allowedTools").split(",")
    assert "Bash" in opt(edit, "--disallowedTools").split(",")
    assert "Edit" not in opt(edit, "--disallowedTools").split(",")

    web = build_args(clean_cfg({"web": True}))
    assert "WebSearch" in opt(web, "--allowedTools").split(",")
    assert "WebSearch" not in opt(web, "--disallowedTools").split(",")

    full = build_args(clean_cfg({"level": "full", "risk_ack": True}))
    assert opt(full, "--permission-mode") == "bypassPermissions" and "--allowedTools" not in full


def test_model_and_dirs_args(tmp_path):
    a = build_args(clean_cfg({"model": "glm-5", "max_turns": 7, "add_dirs": [str(tmp_path)]}))
    assert opt(a, "--model") == "glm-5" and opt(a, "--max-turns") == "7" and opt(a, "--add-dir") == str(tmp_path.resolve())


# ------------------------------------------------- environment variable allowlist
def test_env_is_whitelisted(monkeypatch):
    monkeypatch.setenv("TEAM_AGENT_TOKEN", "secret-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret2")
    monkeypatch.setenv("CODEBUDDY_API_KEY", "user-set-key")
    monkeypatch.setenv("CODEBUDDY_COMPUTER_USE_ENABLED", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    env = build_env(clean_cfg({}))
    assert "TEAM_AGENT_TOKEN" not in env and "DEEPSEEK_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["CODEBUDDY_API_KEY"] == "user-set-key"           # credentials the user configured for the engine are passed through
    assert "CODEBUDDY_COMPUTER_USE_ENABLED" not in env          # desktop control is never passed through
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert env["CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_find_launcher_uses_custom_then_none(monkeypatch, tmp_path):
    monkeypatch.delenv("TEAM_AGENT_CODEBUDDY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(external, "BUNDLED_MAC", ())
    monkeypatch.setattr(external, "_extra_bins", lambda: [])
    assert find_launcher() is None
    lc = find_launcher(FAKE)
    assert lc and lc.argv == [sys.executable, FAKE] and lc.via == "custom"


# -------------------------------------------------------------- stream parsing
async def feed_all(lines, deltas=None, tools=None):
    async def d(t):
        (deltas if deltas is not None else []).append(t)

    async def tl(i, e):
        (tools if tools is not None else []).append((i, e))

    p = StreamParser(d, tl)
    for ln in lines:
        await p.feed(ln)
    return p


def j(o):
    return json.dumps(o, ensure_ascii=False)


def test_parser_full_message_flow_and_subagent_filter():
    deltas, tools = [], []
    lines = [
        j({"type": "system", "subtype": "init", "session_id": "s", "model": "m1"}),
        j({"type": "assistant", "message": {"content": [{"type": "text", "text": "先看看"},
                                                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a"}}]}}),
        j({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "内容"}]}}),
        j({"type": "assistant", "parent_tool_use_id": "x", "message": {"content": [{"type": "text", "text": "子智能体"}]}}),
        j({"type": "assistant", "message": {"content": [{"type": "text", "text": "结论"}]}}),
        j({"type": "result", "subtype": "success", "result": "最终结论", "total_cost_usd": 0.5, "num_turns": 2,
           "permission_denials": [{"tool_name": "Bash"}]}),
    ]
    p = asyncio.run(feed_all(lines, deltas, tools))
    out = p.outcome()
    assert out.text == "最终结论" and out.session_id == "s" and out.model == "m1"
    assert out.cost_usd == 0.5 and out.num_turns == 2 and out.denials == ["Bash"]
    assert "".join(deltas) == "先看看\n\n结论" and "子智能体" not in "".join(deltas)
    assert [(i, e["status"]) for i, e in tools] == [(0, "running"), (0, "ok")] and tools[1][1]["preview"] == "内容"
    assert not p.error


def test_parser_partial_deltas_not_duplicated_by_final_assistant_message():
    deltas = []
    lines = [
        j({"type": "stream_event", "event": {"type": "message_start"}}),
        j({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你好"}}}),
        j({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "世界"}}}),
        j({"type": "assistant", "message": {"content": [{"type": "text", "text": "你好世界"}]}}),
        j({"type": "result", "subtype": "success", "result": "你好世界"}),
    ]
    p = asyncio.run(feed_all(lines, deltas))
    assert "".join(deltas) == "你好世界" and p.outcome().text == "你好世界"


def test_parser_error_result_unknown_events_and_non_json_fallback():
    p = asyncio.run(feed_all([j({"type": "weird"}), j([1, 2]), "not json", "",
                              j({"type": "result", "subtype": "error_max_turns", "is_error": True, "errors": ["太多轮了"]})]))
    assert "太多轮了" in p.error
    q = asyncio.run(feed_all(["普通文字第一行", "第二行"]))
    assert q.outcome().text == "普通文字第一行\n第二行" and not q.error


def test_explain_failure_adds_login_hint():
    assert "codebuddy" in external.explain_failure(1, "Error: not logged in", "")
    assert "not signed in" not in external.explain_failure(2, "segfault", "")
    assert "exit code 2" in external.explain_failure(2, "segfault", "")


# -------------------------------------------------- running it (against the fake CLI)
def run(store, agent, prompt="hi", system="SYS", **kw):
    runner = ExternalRunner(store.data_dir)
    return asyncio.run(runner.run(agent, system=system, prompt=prompt, **kw))


def test_run_success_sends_prompt_on_stdin_in_workspace(store, fake_env):
    a = make_agent(store)
    res = run(store, a, prompt="群聊记录XYZ", system="系统提示ABC")
    assert res.text.startswith("读完了") and res.session_id == "sess-1" and res.cost_usd == 0.01
    log = json.loads(fake_env.read_text())
    assert log["stdin"] == "群聊记录XYZ"
    assert opt(log["argv"], "--append-system-prompt") == "系统提示ABC"
    assert Path(log["cwd"]).resolve() == (store.data_dir / "external" / a["id"] / "workspace").resolve()
    assert "TEAM_AGENT_TOKEN" not in log["env"] and "CODEBUDDY_FAKE_MODE" in log["env"]


def test_run_streams_deltas_and_tools(store, fake_env, monkeypatch):
    monkeypatch.setenv("CODEBUDDY_FAKE_MODE", "delta")
    got = []

    async def d(t):
        got.append(t)

    res = asyncio.run(ExternalRunner(store.data_dir).run(make_agent(store), system="", prompt="x", on_delta=d))
    assert "".join(got) == "你好,我是假 WorkBuddy" and res.text == "你好,我是假 WorkBuddy" and res.duration_ms == 321


def test_run_errors_are_explained(store, fake_env, monkeypatch):
    a = make_agent(store)
    for mode, needle in (("crash", "boom"), ("auth", "codebuddy"), ("error", "unauthorized")):
        monkeypatch.setenv("CODEBUDDY_FAKE_MODE", mode)
        with pytest.raises(ExternalError, match=needle):
            run(store, a)
    monkeypatch.setenv("CODEBUDDY_FAKE_MODE", "raw")
    assert "纯文本" in run(store, a).text        # when the engine does not emit stream-json, fall back to its text output


def test_run_timeout_kills_process(store, fake_env, monkeypatch):
    monkeypatch.setenv("CODEBUDDY_FAKE_MODE", "slow")
    a = make_agent(store)
    a["engine_cfg"]["timeout"] = 1
    t0 = time.time()
    with pytest.raises(ExternalError, match="did not finish within 1"):
        run(store, a)
    assert time.time() - t0 < 20


def test_run_cancel_kills_process(store, fake_env, monkeypatch):
    monkeypatch.setenv("CODEBUDDY_FAKE_MODE", "slow")
    a = make_agent(store)

    async def go():
        t = asyncio.ensure_future(ExternalRunner(store.data_dir).run(a, system="", prompt="x"))
        await asyncio.sleep(1.5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(go())
    time.sleep(0.5)
    ps = os.popen("ps -eo args | grep codebuddy_fake | grep -v grep").read()
    assert ps.strip() == ""


def test_run_without_cli_gives_actionable_error(store, monkeypatch):
    monkeypatch.delenv("TEAM_AGENT_CODEBUDDY", raising=False)
    monkeypatch.setattr(external, "BUNDLED_MAC", ())
    monkeypatch.setattr(external, "_extra_bins", lambda: [])
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ExternalError, match="WorkBuddy"):
        run(store, make_agent(store))


def test_probe_reports_version_and_live(store, fake_env):
    r = ExternalRunner(store.data_dir)
    p = asyncio.run(r.probe({}))
    assert p["found"] and p["version"] == "9.9.9-fake" and p["live"] is None
    p = asyncio.run(r.probe({}, live=True))
    assert p["live"]["ok"] and "读完了" in p["live"]["reply"]
    assert "--max-turns" in json.loads(fake_env.read_text())["argv"]


# ------------------------------------- external agents inside the group chat
def enable(store, **extra):
    store.update_settings({"external_agents_enabled": True, **extra})


def group_with_external(store, make_router, fake, **cfg):
    orch, g = setup(store, make_router, fake)
    a = make_agent(store, **cfg)
    store.add_member(g["id"], a["id"])
    return orch, g, a


def test_external_turn_replies_in_group_and_hands_off(store, make_router, fake_env):
    enable(store)
    fake = FakeLLM(default="收到,我来确认。")
    orch, g, a = group_with_external(store, make_router, fake)
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 帮我读一下文件", col))
    ends = col.ends()
    wb = [m for m in ends if m["sender_name"] == "WorkBuddy"][0]
    assert wb["content"].startswith("读完了") and wb["model_id"] == "ext:workbuddy"
    assert wb["meta"]["engine"] == "workbuddy" and wb["meta"]["level"] == "read"
    assert wb["meta"]["tools"][0]["name"] == "Read" and wb["meta"]["tools"][0]["status"] == "ok"
    assert wb["meta"]["denied"] == ["Bash"] and wb["meta"]["external"]["cost_usd"] == 0.01
    # it @-ed the copywriter, and the member it named speaks next (off a real model)
    assert [m["sender_name"] for m in ends][-1] == "Copywriter" and fake.calls
    log = json.loads(fake_env.read_text())
    assert "帮我读一下文件" in log["stdin"] and "[Chat transcript]" in log["stdin"]
    sysprompt = opt(log["argv"], "--append-system-prompt")
    # the notes block is the external agent own prompt; the model column of the roster is
    # what follows the interface language
    assert "[Notes for external agents]" in sysprompt and "Read-only" in sysprompt
    assert "external agent (WorkBuddy)" in sysprompt
    assert "<tool_call>" not in log["stdin"]                     # our own text tool protocol is not pushed into it
    deltas = "".join(e["text"] for e in col.events if e["type"] == "delta" and e["message_id"] == wb["id"])
    assert "先看看" not in deltas and "读完了" in deltas or "我先看看文件" in deltas


def test_handoff_off_keeps_mentions_as_plain_text(store, make_router, fake_env):
    enable(store)
    fake = FakeLLM(default="不该被调用")
    orch, g, a = group_with_external(store, make_router, fake, handoff=False)
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 读文件", col))
    assert [m["sender_name"] for m in col.ends()] == ["WorkBuddy"] and not fake.calls


def test_external_disabled_or_offline_is_skipped_with_notice(store, make_router, fake_env):
    fake = FakeLLM(default="x")
    orch, g, a = group_with_external(store, make_router, fake)
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 你好", col))
    assert not col.ends() and any(e["type"] == "message_discard" for e in col.events)
    sys_msgs = [e["message"]["content"] for e in col.events if e["type"] == "message" and e["message"]["sender_type"] == "system"]
    assert any("master switch" in t for t in sys_msgs)
    assert not fake_env.exists()                                  # the CLI was never launched
    enable(store, external_calls_enabled=False)
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 你好", col))
    assert any("outbound calls are switched off" in e["message"]["content"] for e in col.events if e["type"] == "message" and e["message"]["sender_type"] == "system")
    assert not fake_env.exists()


def test_external_failure_shows_system_notice_and_no_bubble(store, make_router, fake_env, monkeypatch):
    enable(store)
    monkeypatch.setenv("CODEBUDDY_FAKE_MODE", "auth")
    orch, g, a = group_with_external(store, make_router, FakeLLM(default="x"))
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 你好", col))
    assert not col.ends() and any(e["type"] == "message_discard" for e in col.events)
    notes = [e["message"]["content"] for e in col.events if e["type"] == "message" and e["message"]["sender_type"] == "system"]
    assert any("WorkBuddy" in t and "could not reply" in t and "not signed in" in t for t in notes)


def test_tampered_settings_are_rejected_at_run_time(store, make_router, fake_env):
    enable(store)
    orch, g, a = group_with_external(store, make_router, FakeLLM(default="x"))
    store.update_agent(a["id"], {"engine_cfg": {**a["engine_cfg"], "level": "full", "risk_ack": False}})   # tampering with the database: full permissions, never acknowledged
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 你好", col))
    assert not col.ends() and not fake_env.exists()
    assert any("are not valid" in e["message"]["content"] for e in col.events if e["type"] == "message" and e["message"]["sender_type"] == "system")


def test_external_agent_is_never_the_host_and_plan_still_works(store, make_router, fake_env):
    enable(store)
    fake = FakeLLM(default="群主直接回答")
    orch, g, a = group_with_external(store, make_router, fake)
    store.update_group(g["id"], {"host_agent_id": a["id"]})       # forced past the API: orchestration must still pick a model member as host
    members = store.group_members(g["id"])
    host = orch._pick_host(store.get_group(g["id"]), members)
    assert not host.get("engine")
    col = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "大家好", col))    # nobody is @-ed: the host, a model member, answers, not the external agent
    assert all(m["sender_name"] != "WorkBuddy" for m in col.ends()) and not fake_env.exists()


def test_roster_and_variables_do_not_route_external_member(store, make_router, fake_env):
    orch, g, a = group_with_external(store, make_router, FakeLLM(default="x"))
    members = store.group_members(g["id"])
    text = orch.prompts.roster_text(members)
    # a member role comes straight from the data (here one this test created itself);
    # only the model column is localized
    assert "WorkBuddy(外部智能体)" in text and "model: external agent (WorkBuddy)" in text
    vals = orch.prompts.values(store.get_group(g["id"]), a, members)
    assert vals["model_name"] == "WorkBuddy"


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def client(tmp_path, fake_env):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="你好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def test_api_requires_master_switch(client):
    o = client.get("/api/external").json()
    assert o["enabled"] is False and o["engines"][0]["id"] == "workbuddy" and o["engines"][0]["found"] is True
    assert [x["id"] for x in o["levels"]] == ["read", "edit", "full"] and o["members"] == []
    assert client.post("/api/external/agents", json={}).status_code == 403
    assert client.post("/api/external/test", json={}).status_code == 403
    assert not os.path.exists(os.environ["CODEBUDDY_FAKE_LOG"])


def test_api_create_patch_and_group_rules(client, tmp_path):
    assert client.put("/api/settings", json={"external_agents_enabled": True}).json()["external_agents_enabled"] is True
    g = client.get("/api/groups").json()[0]
    a = client.post("/api/external/agents", json={"group_id": g["id"], "cfg": {"level": "edit", "cwd": str(tmp_path)}}).json()
    assert a["engine"] == "workbuddy" and a["name"] == "WorkBuddy" and a["model_id"] is None
    assert a["engine_cfg"]["level"] == "edit" and set(a["tags"]) == {"tool-use", "coding"}
    assert a["id"] in client.get("/api/groups").json()[0]["member_ids"]
    b = client.post("/api/external/agents", json={}).json()          # avoids the name clash on its own
    assert b["name"] == "WorkBuddy2" and b["engine_cfg"]["level"] == "read"
    assert client.post("/api/external/agents", json={"name": "WorkBuddy"}).status_code == 409
    assert client.post("/api/external/agents", json={"name": "a b"}).status_code == 400
    assert client.post("/api/external/agents", json={"engine": "nope"}).status_code == 400
    assert client.post("/api/external/agents", json={"cfg": {"level": "full"}}).status_code == 400
    ok = client.patch(f"/api/external/agents/{a['id']}", json={"cfg": {"level": "full", "risk_ack": True, "cwd": str(tmp_path)}})
    assert ok.status_code == 200 and ok.json()["engine_cfg"]["level"] == "full"
    assert client.patch(f"/api/external/agents/{b['id']}", json={"cfg": {"timeout": 1}}).status_code == 400
    assert client.patch("/api/external/agents/nope", json={"cfg": {}}).status_code == 404
    # an external agent can be neither host nor pinned to a model
    assert client.patch(f"/api/groups/{g['id']}", json={"host_agent_id": a["id"]}).status_code == 400
    assert client.post("/api/groups", json={"name": "x", "member_ids": [a["id"]], "host_agent_id": a["id"]}).status_code == 400
    assert client.patch(f"/api/agents/{a['id']}", json={"model_id": "deepseek/deepseek-flash"}).status_code == 400
    ov = client.get("/api/external").json()
    assert {m["name"] for m in ov["members"]} == {"WorkBuddy", "WorkBuddy2"} and ov["members"][0]["workspace"] is not None


def test_api_test_endpoint_and_offline_rule(client):
    client.put("/api/settings", json={"external_agents_enabled": True})
    r = client.post("/api/external/test", json={}).json()
    assert r["found"] and r["version"] == "9.9.9-fake" and r["live"] is None
    r = client.post("/api/external/test", json={"live": True}).json()
    assert r["live"]["ok"] is True
    client.put("/api/settings", json={"external_calls_enabled": False})
    assert client.post("/api/external/test", json={"live": True}).status_code == 403
    assert client.post("/api/external/test", json={}).status_code == 200      # reading the version number makes no request
    assert client.post("/api/external/test", json={"agent_id": "nope"}).status_code == 404


def test_stats_label_for_external_model(store, make_router, fake_env):
    from app.stats import compute_stats
    enable(store)
    orch, g, a = group_with_external(store, make_router, FakeLLM(default="x"), handoff=False)
    asyncio.run(orch.handle_user_message(g["id"], "@WorkBuddy 读文件", Collector()))
    rows = compute_stats(store)["by_model"]
    assert {"model_id": "ext:workbuddy", "label": "Workbuddy (external)"}.items() <= next(r for r in rows if r["model_id"] == "ext:workbuddy").items()


def test_parser_on_real_codebuddy_2_137_1_capture():
    """stream-json captured on a real machine (codebuddy 2.137.1 bundled with WorkBuddy
    5.5.6): thinking deltas first, then text deltas, then two complete assistant messages
    (thinking first, then text) and a result. Text must be emitted once and thinking must
    never reach the group.
    """
    lines = (Path(__file__).parent / "fixtures" / "codebuddy_2.137.1_stream.jsonl").read_text().splitlines()
    deltas: list[str] = []
    p = asyncio.run(feed_all(lines, deltas))
    out = p.outcome()
    assert "".join(deltas) == "OK" and out.text == "OK" and not p.error
    assert out.model == "auto" and out.num_turns == 3 and out.cost_usd == 0 and out.session_id


def test_parser_marks_permission_denied_tool_results_even_without_is_error():
    """Seen on a real machine: Bash/Write blocked at the read-only level come back as a
    plain "Error: Permission to use X has been denied..." tool_result, with no is_error and
    an empty permission_denials in the result. Recognising it from the text is what makes
    the group show "denied" instead of "ok".
    """
    msg = "Error: Permission to use Bash has been denied because this tool requires approval but permission prompts are not available"
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x/hello.txt"}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "echo pwned > pwned.txt"}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "   1→秘密暗号:蓝莓-7"},
            {"type": "tool_result", "tool_use_id": "t2", "content": msg}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "Bash 被拒绝", "permission_denials": []}),
    ]
    p = asyncio.run(feed_all(lines, []))
    out = p.outcome()
    assert [t["status"] for t in out.tools] == ["ok", "denied"]
    assert out.denials == ["Bash"] and out.text == "Bash 被拒绝"
