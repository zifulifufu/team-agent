"""Executable media assignments, real deliveries and local runtime recovery."""
import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import comfyui, planner
from app.comfy_runtime import LocalRuntime
from app.main import create_app
from app.orchestrator import Orchestrator, RunState
from tests.conftest import FakeLLM
from tests.test_collab import setup
from tests.test_comfyui import FakeComfy, PROV, object_info
from tests.test_delivery_contract import task, msg


@pytest.mark.parametrize("tools,deliverable", [
    (["generate_image"], "frame.png"), (["generate_video"], "frame.png"),
    (["assemble_video"], "film.mp4"), (["write_document"], "script.md"),
])
def test_video_member_cannot_be_assigned_other_jobs(tools, deliverable):
    members = [{"id": "wan", "name": "Wan", "origin": "media"}]
    with pytest.raises(planner.PlanError):
        planner.build_plan({"tasks": [{"owner": "Wan", "instruction": "work", "tools": tools,
                                        "deliverable": deliverable}]}, members,
                           member_tools={"wan": {"generate_video"}})


def test_plan_keeps_all_tools_and_structured_generator_parameters():
    p = planner.build_plan({"tasks": [{"owner": "host", "instruction": "make film",
        "tools": ["library_read", "write_document", "generate_image", "generate_video", "assemble_video"],
        "arguments": {"duration_seconds": 3, "aspect_ratio": "9:16"}}]}, [{"id": "h", "name": "host"}])
    assert p.tasks[0].tools[-1] == "assemble_video"
    assert p.to_meta()["tasks"][0]["arguments"]["duration_seconds"] == 3
    prompt = planner.task_prompt(p, p.tasks[0], {}, 1)
    assert '"duration_seconds": 3' in prompt and '"aspect_ratio": "9:16"' in prompt


def test_dependency_on_dropped_task_is_not_misreported_as_cycle():
    p = planner.build_plan({"tasks": [
        {"id": "bad", "owner": "missing", "instruction": "x"},
        {"id": "next", "owner": "host", "instruction": "x", "needs": ["bad"]},
    ]}, [{"id": "h", "name": "host"}])
    assert p.dropped and p.tasks[0].needs == ["bad"]


@pytest.mark.parametrize("files", [[], [{"name": "missing.mp4"}], [{"name": "notes.md"}]])
def test_search_or_wrong_file_cannot_complete_video_task(tmp_path, files):
    (tmp_path / "notes.md").write_text("storyboard")
    orch = Orchestrator.__new__(Orchestrator)
    assert orch._task_shortfall(task("final.mp4"), msg([{"name": "library_search", "status": "ok", "files": files}]), tmp_path)


def test_empty_or_outside_workspace_artifact_is_not_delivery(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "empty.mp4").touch()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"clip")
    orch = Orchestrator.__new__(Orchestrator)
    for path in ["empty.mp4", str(outside)]:
        assert orch._task_shortfall(task("final.mp4"), msg([{"name": "generate_video", "status": "ok", "files": [{"path": path}]}]), root)


async def test_plan_repair_receives_original_request_and_plan(store, make_router):
    fake = FakeLLM(default='<plan>{"tasks":[]}</plan>')
    orch, group = setup(store, make_router, fake)
    host = store.get_agent(group["host_agent_id"])
    await orch._repair_plan(host, store.group_members(group["id"]), "wrong owner",
                           original="draft plan with frame.png", request="make a 12 second film",
                           capabilities="Wan: generate_video")
    prompt = fake.calls[-1][1][-1]["content"]
    assert all(s in prompt for s in ["frame.png", "12 second film", "Wan: generate_video"])


def test_media_handoff_carries_duration_ratio_and_public_image_url(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    target = {"use": "video", "provider": {"kind": "metachat_media"}, "model": {"model_name": "mj-video-v1"}}
    args = orch._media_arguments(group, RunState(group["id"], "make video"), target, "make it",
        {"arguments": {"duration_seconds": 5, "aspect_ratio": "9:16", "first_frame": "images/frame.png"},
         "input_files": [{"kind": "image", "name": "frame.png", "path": "images/frame.png",
                          "source_url": "https://assets.example/frame.png"}]})
    assert args == {"duration_seconds": 5, "aspect_ratio": "9:16", "first_frame": "https://assets.example/frame.png"}


def test_text_to_video_does_not_receive_ark_only_reference_arrays(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    target = {"use": "video", "provider": {"kind": "comfyui"}, "model": {"model_name": comfyui.DEFAULT_WORKFLOW}}
    args = orch._media_arguments(group, RunState(group["id"], "制作 2 秒 1:1 视频"), target,
                                "制作 2 秒 1:1 视频", {})
    assert args == {"duration_seconds": 2, "aspect_ratio": "1:1"}


async def test_probe_checks_save_node_not_just_model_loaders():
    async with FakeComfy(info=object_info(without=("SaveVideo",))).client() as client:
        ok, detail = await comfyui.probe(PROV, client=client)
    assert not ok and "SaveVideo" in detail


async def test_unreadable_node_catalog_is_not_ready():
    def respond(request):
        return httpx.Response(200, json={"system": {"comfyui_version": "1"}}) if request.url.path == "/system_stats" else httpx.Response(403, json={"error": "forbidden"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (await comfyui.probe(PROV, client=client))[0] is False


async def test_runtime_autostart_reuses_one_process_for_concurrent_requests(tmp_path):
    root = tmp_path / "comfy"
    root.mkdir()
    (root / "main.py").write_text('''import json,sys
from http.server import HTTPServer,BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"system":{"comfyui_version":"test"}}')
HTTPServer(('127.0.0.1',int(sys.argv[sys.argv.index('--port')+1])),Handler).serve_forever()
''')
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    manager = LocalRuntime(tmp_path)
    prov = {"kind": "comfyui", "is_local": True, "base_url": f"http://127.0.0.1:{port}"}
    cfg = {"comfyui_auto_start": True, "comfyui_dir": str(root), "comfyui_python": sys.executable}
    try:
        results = await asyncio.gather(manager.ensure(prov, cfg, timeout=8), manager.ensure(prov, cfg, timeout=8))
        assert all(r[0] for r in results) and len(manager._children) == 1
        pid = manager._children[prov["base_url"]].pid
        assert (await manager.ensure(prov, cfg))[0]
        assert manager._children[prov["base_url"]].pid == pid
    finally:
        for process in manager._children.values():
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)


async def test_autostart_never_launches_remote_or_unconfigured_installation(tmp_path):
    manager = LocalRuntime(tmp_path)
    assert not (await manager.ensure({"kind": "comfyui", "is_local": False, "base_url": "http://example.com"}, {"comfyui_auto_start": True}))[0]
    assert not manager._children


def test_media_member_shows_its_model_and_cannot_be_changed_to_chat(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    store.add_provider_from_preset("comfyui")
    member = store.ensure_model_agent("comfyui/wan2.2-ti2v-5b")
    group = store.list_groups()[0]
    store.add_member(group["id"], member["id"])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        caps = client.get(f"/api/groups/{group['id']}/capabilities").json()
        shown = next(m for m in caps["members"] if m["agent_id"] == member["id"])
        assert shown["model"]["id"] == member["model_id"] and shown["model"]["is_local"]
        assert client.patch(f"/api/agents/{member['id']}", json={"model_id": None}).status_code == 400


def test_generators_wait_for_explicit_assignment_instead_of_joining_all_chatter():
    assert Orchestrator._quiet_members([{"id": "wan", "origin": "media"}, {"id": "host"}]) == {"wan"}


@pytest.mark.parametrize('bar', ['||', '｜｜'])
def test_native_dsml_call_runs_without_leaking_markup(bar):
    from app.toolcall import parse_tool_calls, strip_hidden, TagFilter
    raw = (f'<{bar}DSML{bar} calls><{bar}DSML{bar} invoke name="generate_video">'
           f'<{bar}DSML{bar} parameter name="prompt" string="true">a boat &amp; water</{bar}DSML{bar} parameter>'
           f'<{bar}DSML{bar} parameter name="duration_seconds" string="false">1</{bar}DSML{bar} parameter>'
           f'</{bar}DSML{bar} invoke></{bar}DSML{bar} calls>')
    visible, calls = parse_tool_calls('before' + raw + 'after')
    assert visible == 'beforeafter'
    assert len(calls) == 1 and not calls[0].error
    assert calls[0].name == 'generate_video'
    assert calls[0].arguments == {'prompt': 'a boat & water', 'duration_seconds': 1}
    assert strip_hidden(raw) == ''
    for step in [1, 5, 17]:
        f = TagFilter()
        assert ''.join(f.feed(raw[i:i+step]) for i in range(0,len(raw),step)) + f.flush() == ''


def test_incomplete_native_dsml_call_requests_repair_and_stays_hidden():
    from app.toolcall import parse_tool_calls, strip_hidden, TagFilter
    raw = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="write_document"><｜｜DSML｜｜ parameter name="body">partial'
    visible, calls = parse_tool_calls(raw)
    assert visible == strip_hidden(raw) == ''
    assert len(calls) == 1 and calls[0].error
    f = TagFilter()
    assert f.feed(raw) + f.flush() == ''


async def test_native_dsml_writes_file_through_group_tool_loop(store, make_router):
    from tests.test_collab import Collector
    raw = ('<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="write_document">'
           '<｜｜DSML｜｜ parameter name="path" string="true">native.md</｜｜DSML｜｜ parameter>'
           '<｜｜DSML｜｜ parameter name="body" string="true"># Native tool result</｜｜DSML｜｜ parameter>'
           '</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>')
    replies = iter([raw, 'Written native.md.'])
    fake = FakeLLM(default=lambda messages: next(replies, 'Done.'))
    orch, group = setup(store, make_router, fake, plan_mode='off', perm_mode='allow_all')
    events = Collector()
    await orch.handle_user_message(group['id'], 'Write native.md', events)
    await orch.drain()
    file = store.workspace_dir(group['id']) / 'native.md'
    assert file.is_file() and 'Native tool result' in file.read_text()
    assert all('DSML' not in m['content'] for m in events.ends())
    assert any(t['name']=='write_document' and t['status']=='ok'
               for m in events.ends() for t in m.get('meta',{}).get('tools',[]))


def test_shipped_wan_workflow_avoids_unipc_mps_corruption():
    payload = comfyui.payload_for(comfyui.DEFAULT_WORKFLOW, prompt='a red boat', ratio='16:9',
                                 seconds=1, short_edge=480, seed=42)
    assert payload['frames'] == 25
    assert payload['graph']['3']['inputs']['sampler_name'] == 'euler'
    assert payload['graph']['3']['inputs']['cfg'] == 5
