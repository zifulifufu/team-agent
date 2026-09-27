"""Invitation, concrete tool handoffs and the return to the assigning member."""

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cooperation, localcmd, planner
from app.main import create_app
from app.orchestrator import RunState, TurnOut, _tool_instruction
from tests.conftest import FakeLLM
from tests.test_collab import setup, Collector


def test_comfyui_invitation_is_named_and_reused(tmp_path):
    app = create_app(tmp_path)
    st = app.state.store
    st.add_provider_from_preset("comfyui")
    gid = st.list_groups()[0]["id"]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for _ in range(2):
            response = client.post(f"/api/groups/{gid}/members/from-model", json={"model_id": "comfyui/wan2.2-ti2v-5b"})
            assert response.status_code == 200
        tools = [a for a in client.get("/api/agents").json() if a.get("is_tool")]
        assert len(tools) == 1 and tools[0]["name"] == "ComfyUI"
        assert response.json()["member_ids"].count(tools[0]["id"]) == 1
        caps = client.get(f"/api/groups/{gid}/capabilities").json()
        worker = next(m for m in caps["members"] if m["agent_id"] == tools[0]["id"])
        assert worker["participation"]["kind"] == "generator"
        assert worker["participation"]["tools"] == ["generate_video"]


@pytest.mark.parametrize("failed", [False, True])
async def test_tool_returns_to_assigner_even_on_failure(store, make_router, monkeypatch, failed):
    orch, group = setup(store, make_router, FakeLLM(), plan_mode="off", external_agents_enabled=True)
    host = store.get_agent(group["host_agent_id"])
    tool = store.create_agent("Renderer", "R", "render", "", None, [], [], engine="hyperframes")
    store.add_member(group["id"], tool["id"])
    turns = []

    async def turn(group, agent, members, emit, run, **kw):
        turns.append((agent["id"], kw.get("extra_user")))
        if agent["id"] == host["id"]:
            text = "@Renderer render the prepared composition" if len(turns) == 1 else "Delivery checked."
            return TurnOut(text, text, {"meta": {}})
        assert kw["extra_user"] == "render the prepared composition"
        if failed:
            run.last_error = "index.html is missing"
            return None
        return TurnOut("Rendered", "Rendered", {"meta": {"tools": [{"name": "local:hyperframes", "status": "ok", "files": [{"name": "clip.mp4", "path": "hyperframes/clip.mp4"}]}]}})

    monkeypatch.setattr(orch, "_agent_turn", turn)
    await orch._run_turns(group, "make a film", Collector(), RunState(group["id"], "make a film"))
    assert [t[0] for t in turns] == [host["id"], tool["id"], host["id"]]
    assert ("index.html is missing" if failed else "hyperframes/clip.mp4") in turns[-1][1]


async def test_identical_handoff_is_not_executed_twice(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), plan_mode="off", external_agents_enabled=True)
    tool = store.create_agent("Renderer", "R", "render", "", None, [], [], engine="hyperframes")
    store.add_member(group["id"], tool["id"])
    seen = []
    async def turn(group, agent, members, emit, run, **kw):
        seen.append(agent["id"])
        text = "ok" if agent["id"] == tool["id"] else "@Renderer render now"
        return TurnOut(text, text, {"meta": {}})
    monkeypatch.setattr(orch, "_agent_turn", turn)
    await orch._run_turns(group, "make film", Collector(), RunState(group["id"], "make film"))
    assert seen.count(tool["id"]) == 1 and len(seen) == 3


def test_multiple_tool_instructions_are_separated():
    members = [{"id": "a", "name": "ComfyUI"}, {"id": "b", "name": "VoiceStudio"}]
    text = "@ComfyUI a red boat\n@VoiceStudio Welcome aboard."
    assert _tool_instruction(text, members, members[0]) == "a red boat"
    assert _tool_instruction(text, members, members[1]) == "Welcome aboard."


async def test_voice_plan_reads_exact_text_not_plan_wrapper(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), external_agents_enabled=True)
    tool = store.create_agent("Voice", "V", "voice", "", None, [], [], engine="qwen3tts")
    captured = []
    async def execute(engine, **kw):
        captured.append(kw["instruction"])
        return {"ok": True, "text": "done", "seconds": 0, "exit": 0, "command": ["tts"], "files": []}
    monkeypatch.setattr(localcmd, "run", execute)
    await orch._agent_turn(group, tool, [tool], Collector(), RunState(group["id"], "Create a movie"),
                           extra_user="Plan metadata, upstream files, assignment rules",
                           extra_meta={"arguments": {"text": "Hello\nworld."}, "task_instruction": "Read the narration"})
    assert captured == ["Hello\nworld."]
    await orch._agent_turn(group, tool, [tool], Collector(), RunState(group["id"], "read", read_only=True))
    assert len(captured) == 1


def test_local_tool_contract_prevents_planning_an_authoring_task(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    tool = store.create_agent("Renderer", "R", "render", "", None, [], [], engine="hyperframes")
    info = cooperation.contract(store, tool)
    assert "index.html" in info["preparation"] and "hyperframes/" in info["preparation"]
    with pytest.raises(planner.PlanError):
        planner.build_plan({"tasks": [{"owner": "Renderer", "instruction": "write project", "tools": ["write_document"]}]},
                           [tool], member_tools={tool["id"]: {"local:hyperframes"}})


def test_wan_roster_does_not_require_an_image(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    store.add_provider_from_preset("comfyui")
    tool = store.ensure_model_agent("comfyui/wan2.2-ti2v-5b")
    prompt = orch.prompts.roster_text([tool])
    assert "arguments.prompt" in prompt and "duration_seconds" in prompt
    assert "must be given a reference image" not in prompt
    assert "它必须拿到一张" not in prompt


async def test_invitation_probes_do_not_freeze_other_requests(tmp_path, monkeypatch):
    from app.external import ExternalRunner
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_probe(self, engine):
        loop.call_soon_threadsafe(started.set)
        time.sleep(0.15)
        return {"found": False, "path": "", "hint": "not installed"}

    monkeypatch.setattr(ExternalRunner, "describe", slow_probe)
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        pending = asyncio.create_task(client.get("/api/external"))
        await started.wait()
        assert (await client.get("/api/health")).status_code == 200
        assert not pending.done(), "catalog probes must yield so chat and status requests stay responsive"
        assert (await pending).status_code == 200
