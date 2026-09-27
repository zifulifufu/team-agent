"""Capability-based failover and host recruitment, with no real model or media calls."""
import json

import pytest

from app import localcmd, presets, recruitment, templates, vision
from app.orchestrator import RunState
from app.router import AllRoutesFailed
from app.tools import PluginInfo
from tests.conftest import FakeLLM
from tests.test_collab import Collector, setup


def model(store, provider, name, tags):
    if not store.get_provider(provider):
        store.add_provider(provider, "openai_compatible", "https://example.invalid/v1", "synthetic-key", pid=provider)
    row = store.add_model(provider, name)
    return store.update_model(row["id"], {"strengths": tags})


async def test_fallback_matches_current_task_instead_of_manual_chain_order(store, make_router):
    broken = model(store, "first", "broken", ["reasoning"])
    coder = model(store, "code", "coder", ["coding"])
    writer = model(store, "words", "writer", ["writing", "long-context"])
    disabled = model(store, "disabled", "great-writer", ["writing", "long-context", "chinese"])
    store.update_model(disabled["id"], {"enabled": False})
    store.update_settings({"route_auto_match": True, "route_chain": [coder["id"]]})
    fake = FakeLLM({"openai/broken": RuntimeError("connection refused")})
    result = await make_router(fake).complete([{"role": "user", "content": "write a review"}],
                                               preferred=broken["id"], tags=["writing", "long-context"])
    assert result.model_id == writer["id"] and result.fallback_from == broken["id"]
    assert [c[0] for c in fake.calls] == ["openai/broken", "openai/writer"]


def test_account_quota_survives_router_restart_and_explicit_success_recovers(store, make_router):
    first = model(store, "cloud", "one", ["writing"])
    second = model(store, "cloud", "two", ["writing"])
    store.update_settings({"route_auto_match": True})
    store.set_health(first["id"], "bad", "insufficient balance")
    router = make_router(FakeLLM())
    assert first["id"] in router.recent_problems() and second["id"] in router.recent_problems()
    store.set_health(second["id"], "ok", "")
    assert first["id"] not in router.recent_problems()


async def test_visual_fallback_never_sends_pictures_to_text_model(store, make_router):
    first = model(store, "eyes1", "vision-one", ["multimodal"])
    second = model(store, "eyes2", "vision-two", ["multimodal", "reasoning"])
    text = model(store, "text", "writer", ["writing", "tool-use"])
    store.update_settings({"route_auto_match": True, "vision_cloud": True,
                           "vision_model_id": first["id"], "route_chain": [text["id"]]})
    fake = FakeLLM({"openai/vision-one": RuntimeError("connection refused")}, default="Synthetic visual inspection")
    described = await vision.describe(store, make_router(fake), [("image/png", b"synthetic image")], "inspect")
    assert described.model_id == second["id"]
    assert [c[0] for c in fake.calls] == ["openai/vision-one", "openai/vision-two"]
    store.update_settings({"vision_cloud": False})
    fake.calls.clear()
    assert not await vision.describe(store, make_router(fake), [("image/png", b"x")], "inspect")
    assert not fake.calls


async def test_no_text_fallback_when_every_vision_model_fails(store, make_router):
    first = model(store, "eyes", "vision-one", ["multimodal"])
    text = model(store, "text", "writer", ["writing"])
    store.update_settings({"route_auto_match": True, "vision_cloud": True,
                           "vision_model_id": first["id"], "route_chain": [text["id"]]})
    fake = FakeLLM({"openai/vision-one": RuntimeError("insufficient balance")})
    with pytest.raises(AllRoutesFailed):
        await vision.describe(store, make_router(fake), [("image/png", b"x")], "inspect")
    assert [c[0] for c in fake.calls] == ["openai/vision-one"]


async def test_host_can_recruit_during_planning_and_assign_new_member_immediately(store, make_router):
    reviewer = store.create_agent("Synthetic Reviewer", tags=["writing"])
    n = 0
    def response(messages):
        nonlocal n
        n += 1
        if n == 1:
            return '<tool_call>{"name":"find_team_resources","arguments":{"query":"Synthetic Reviewer writing"}}</tool_call>'
        if n == 2:
            return '<tool_call>' + json.dumps({"name": "invite_team_resource", "arguments": {
                "ref": "agent:" + reviewer["id"], "reason": "Need an independent writer for this synthetic test"}}) + '</tool_call>'
        if n == 3:
            return '<plan>' + json.dumps({"goal": "Synthetic test", "tasks": [{
                "id": "write", "owner": reviewer["name"], "instruction": "Write a synthetic file",
                "tools": ["write_document"], "deliverable": "result.md"}]}) + '</plan>'
        if n == 4:
            return '<tool_call>{"name":"write_document","arguments":{"path":"result.md","body":"Synthetic result"}}</tool_call>'
        return "Synthetic result saved to result.md"
    orch, group = setup(store, make_router, FakeLLM(default=response), host_auto_recruit=True, tool_rounds=5)
    group = store.get_group(group["id"])
    host = store.get_agent(group["host_agent_id"])
    members = store.group_members(group["id"])
    assert reviewer["id"] not in [m["id"] for m in members]
    collector = Collector()
    result = await orch._planning_turn(group, members, host, "Synthetic writing", "on", collector,
                                       RunState(group["id"], "Synthetic writing"))
    assert result is True and reviewer["id"] in store.member_ids(group["id"])
    board = next(m for m in reversed(store.list_messages(group["id"])) if m["sender_type"] == "plan")
    assert board["meta"]["tasks"][0]["owner_id"] == reviewer["id"]
    assert board["meta"]["tasks"][0]["status"] == "done"
    assert (store.workspace_dir(group["id"]) / "result.md").is_file()
    assert any(e["type"] == "group_updated" for e in collector.events)


async def test_recruitment_rechecks_host_toggle_and_read_only_boundary(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), host_auto_recruit=True)
    group = store.get_group(group["id"])
    host = store.get_agent(group["host_agent_id"])
    extra = store.create_agent("Synthetic Extra", tags=["writing"])
    args = {"ref": "agent:" + extra["id"], "reason": "Test missing writer"}
    normal = next(m for m in store.group_members(group["id"]) if m["id"] != host["id"])
    ctx = await orch.toolhub.context(group, normal)
    assert "invite_team_resource" not in ctx.tools
    text, ok = await orch.toolhub._builtin(ctx, "invite_team_resource", args)
    assert not ok and "host" in text
    readonly = await orch.toolhub.context(group, host, read_only=True)
    assert "invite_team_resource" not in readonly.tools
    assert not (await orch.toolhub._builtin(readonly, "invite_team_resource", args))[1]
    ctx = await orch.toolhub.context(group, host)
    store.update_settings({"host_auto_recruit": False})
    assert not (await orch.toolhub.call(ctx, "invite_team_resource", args)).ok
    assert extra["id"] not in store.member_ids(group["id"])


def test_local_tool_invitation_is_idempotent_and_never_installs(store, make_router, monkeypatch, tmp_path):
    orch, group = setup(store, make_router, FakeLLM(), host_auto_recruit=True, external_agents_enabled=True)
    group = store.get_group(group["id"])
    engine = next(iter(localcmd.tools()))
    exe = tmp_path / "synthetic-cli"
    monkeypatch.setattr(localcmd, "exe_for", lambda *args: str(exe))
    with pytest.raises(ValueError, match="executable"):
        recruitment.invite(store, orch.router, orch.registry, group["id"], group["host_agent_id"], "tool:" + engine, "Need a renderer")
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    one = recruitment.invite(store, orch.router, orch.registry, group["id"], group["host_agent_id"], "tool:" + engine, "Need a renderer")
    two = recruitment.invite(store, orch.router, orch.registry, group["id"], group["host_agent_id"], "tool:" + engine, "Already joined")
    assert one["agent_id"] == two["agent_id"] and one["added"] and not two["added"]
    assert store.get_agent(one["agent_id"])["engine"] == engine


def test_host_attaches_only_registered_enabled_tools_without_changing_existing_bindings(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), host_auto_recruit=True)
    group = store.get_group(group["id"])
    original = group["ext"]
    server = store.add_mcp("Synthetic MCP", command="never-executed")
    store.update_mcp(server["id"], {"enabled": False})
    def invite(ref):
        return recruitment.invite(store, orch.router, orch.registry, group["id"], group["host_agent_id"], ref, "Need this configured capability")
    with pytest.raises(ValueError, match="disabled"):
        invite("mcp:" + server["id"])
    assert store.get_group(group["id"])["ext"] == original
    store.update_mcp(server["id"], {"enabled": True})
    assert invite("mcp:" + server["id"])["added"]
    orch.registry.plugins["synthetic"] = PluginInfo("synthetic", "unused.py", name="Synthetic plugin")
    assert invite("plugin:synthetic")["added"]
    ext = store.get_group(group["id"])["ext"]
    assert server["id"] in ext["mcp"] and "synthetic" in ext["plugins"]
    assert ext["skills"] == original["skills"] and ext["library"] == original["library"]
    assert store.get_group(group["id"])["host_agent_id"] == group["host_agent_id"]
    with pytest.raises(ValueError, match="missing"):
        invite("plugin:not-installed")


def test_joined_preset_status_and_duplicate_invites_at_capacity(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), host_auto_recruit=True)
    group = store.get_group(group["id"])
    key, preset = next((key, p) for key, p in presets.AGENT_PRESET_BY_KEY.items()
                       if key != "process" and not p.get("system"))
    member = templates.ensure_agent_from_key(store, key)
    store.add_member(group["id"], member["id"])
    mid = model(store, "synthetic", "member", ["writing"])["id"]
    member_model = store.ensure_model_agent(mid)
    store.add_member(group["id"], member_model["id"])
    while len(store.member_ids(group["id"])) < 64:
        extra = store.create_agent("Synthetic filler " + str(len(store.member_ids(group["id"]))))
        store.add_member(group["id"], extra["id"])
    rows = recruitment.catalog(store, orch.router, orch.registry, group, preset["name"], 24)
    assert next(r for r in rows if r["ref"] == "preset:" + key)["in_group"]
    for ref, ident in (("preset:" + key, member["id"]), ("model:" + mid, member_model["id"])):
        result = recruitment.invite(store, orch.router, orch.registry, group["id"], group["host_agent_id"],
                                    ref, "Reuse existing member")
        assert not result["added"] and result["agent_id"] == ident
    outsider = store.create_agent("Synthetic outsider")
    with pytest.raises(ValueError, match="64"):
        recruitment.invite(store, orch.router, orch.registry, group["id"], group["host_agent_id"],
                           "agent:" + outsider["id"], "New gap")


async def test_invalid_plan_repair_uses_another_capable_model_without_changing_host_preference(store, make_router):
    broken = model(store, "plans", "broken-planner", ["speed", "tool-use"])
    model(store, "plans", "capable-planner", ["reasoning", "tool-use"])
    draft = '<plan>{"tasks":[{"owner":"Writer","instruction":"Synthetic task"}]}</plan>'
    fake = FakeLLM({"openai/broken-planner": RuntimeError("must not retry this planner")}, default=draft)
    orch, group = setup(store, make_router, fake, route_auto_match=True)
    host = store.update_agent(group["host_agent_id"], {"model_id": broken["id"]})
    repaired = await orch._repair_plan(host, store.group_members(group["id"]), "Malformed JSON",
                                       original="<plan>{", request="Synthetic task", failed_model=broken["id"])
    assert repaired == draft and fake.calls[0][0] == "openai/capable-planner"
    assert store.get_agent(host["id"])["model_id"] == broken["id"]
