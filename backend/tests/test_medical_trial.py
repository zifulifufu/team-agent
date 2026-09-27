"""Regressions from the three real self-selected medical production groups."""
from types import SimpleNamespace

import httpx
import pytest

from app import localcmd, teamrec, scoring
from app import attachments, planner, docwrite, voices, assemble, comfyui
from app.literature import LiteratureSearch
from app.orchestrator import RunState
from app.planner import PlanTask
from app.router import AllRoutesFailed
from tests.conftest import chunk
from app.toolcall import parse_tool_calls
from tests.conftest import FakeLLM
from tests.test_collab import setup
from tests.test_orchestrator import Collector


def test_document_call_preserves_literal_newlines_without_guessing_broken_json():
    text = '<tool_call>{"name":"write_document","arguments":{"path":"paper.md","body":"# Title\n\nActual text"}}</tool_call>'
    visible, calls = parse_tool_calls(text, known={"write_document"})
    assert not visible and len(calls) == 1 and not calls[0].error
    assert calls[0].arguments["body"] == "# Title\n\nActual text"
    _, bad = parse_tool_calls('<tool_call>{"name":"write_document","arguments":{"path" "paper.md"}}</tool_call>')
    assert bad[0].error


@pytest.mark.parametrize("tagged", [True, False])
def test_closed_document_body_with_missing_outer_brace(tagged):
    body = '{"name":"write_document","arguments":{"path":"paper.md","body":"Actual complete text"}'
    raw = '<tool_call>' + body + '</tool_call>' if tagged else body
    _, calls = parse_tool_calls(raw, known={"write_document"})
    assert len(calls) == 1 and calls[0].name == "write_document" and not calls[0].error
    assert calls[0].arguments["body"] == "Actual complete text"
    _, cut = parse_tool_calls('<tool_call>'+body[:-2]+'</tool_call>', known={"write_document"})
    assert not cut or cut[0].error


async def test_pubmed_returns_actual_identifiers_and_bounds_records():
    routes = []
    def response(request):
        routes.append(request)
        assert request.url.host == "eutils.ncbi.nlm.nih.gov"
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["retmax"] == "8"
            return httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": ["123"]}})
        if request.url.path.endswith("esummary.fcgi"):
            return httpx.Response(200, json={"result": {"123": {"title": "Evidence", "articleids": [
                {"idtype": "doi", "value": "10.1000/example"}]}}})
        return httpx.Response(200, text="Study abstract")
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        found = await LiteratureSearch().search("aneurysm", 99, client=client)
    assert found["records"][0]["doi"] == "10.1000/example"
    assert found["records"][0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/123/"
    assert found["abstracts"] == "Study abstract" and len(routes) == 3


async def test_pmid_with_explanatory_keywords_is_an_identifier_lookup():
    def respond(request):
        assert request.url.params["term"] == "3082145[uid]"
        return httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await LiteratureSearch().search("PMID 3082145 Smoker criteria", client=client)


async def test_pubmed_network_error_cannot_be_reported_as_evidence(monkeypatch):
    async def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")
    search = LiteratureSearch()
    monkeypatch.setattr(search, "search", fail)
    text, ok, files = await search.run({"query": "aneurysm"})
    assert not ok and not files and "Do not invent" in text


async def test_outbound_switch_is_checked_at_listing_and_execution(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), external_calls_enabled=True)
    host = store.get_agent(group["host_agent_id"])
    ctx = await orch.toolhub.context(group, host, read_only=True)
    assert "search_literature" in ctx.tools
    store.update_settings({"external_calls_enabled": False})
    assert "search_literature" not in (await orch.toolhub.context(group, host)).tools
    _, ok = await orch.toolhub._builtin(ctx, "search_literature", {"query": "aneurysm"})
    assert not ok


async def test_unexecuted_last_round_call_is_visible_failure(store, make_router):
    fake = FakeLLM(default='<tool_call>{"name":"current_time","arguments":{}}</tool_call>')
    orch, group = setup(store, make_router, fake, tool_rounds=1)
    host = store.get_agent(group["host_agent_id"])
    out = await orch._agent_turn(group, host, [host], Collector(), RunState(group["id"], "time"))
    trace = out.message["meta"]["tools"]
    assert [t["status"] for t in trace] == ["ok", "failed"]
    assert "not executed" in out.text or "未执行" in out.text
    assert any("Last tool round" in m["content"] or "最后一轮工具" in m["content"] for m in fake.calls[0][1])


def test_realistic_atlas_requires_image_capability_and_coordinator(store):
    store.update_provider("deepseek", {"api_key": "isolated-key"})
    store.update_settings({"image_enabled": True})
    model = store.add_model("deepseek", "gpt-image-1", "Image")
    advice = teamrec.suggest(store, "绘制前交通动脉瘤逼真模拟图及变异图谱，医学核验")
    assert model["id"] in [m["id"] for m in advice["models"]]
    assert "biostat" not in [e["key"] for e in advice["experts"]]
    assert "med-english" not in [e["key"] for e in advice["experts"]]
    assert advice["host_ref"].startswith("agent:")


def test_reviewer_skips_recently_failed_model(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    store.update_provider("ollama", {"enabled": False})
    first = store.add_model("deepseek", "review-broken", "Broken reviewer")
    second = store.add_model("deepseek", "review-ready", "Ready reviewer")
    store.set_health(first["id"], "bad", "model not found")
    store.set_health(second["id"], "ok")
    assert scoring.pick_judge(store, orch.router, group) == second["id"]


async def test_native_streamed_tool_calls_are_executable_not_lost(store, make_router):
    async def native(**kwargs):
        async def chunks():
            for name, arguments in [("write_document", '{"path":"report.md",'),
                                    (None, '"body":"Complete body"}')]:
                call = SimpleNamespace(index=0, function=SimpleNamespace(name=name, arguments=arguments))
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))])
        return chunks()
    router = make_router(native)
    reply = await router.complete([{"role": "user", "content": "Write document"}])
    _, calls = parse_tool_calls(reply.text, known={"write_document"})
    assert len(calls) == 1 and not calls[0].error
    assert calls[0].arguments == {"path": "report.md", "body": "Complete body"}


async def test_native_schema_is_sent_and_removed_after_tool_budget(store, make_router):
    captured = []
    fake = FakeLLM(default='<tool_call>{"name":"current_time","arguments":{}}</tool_call>')
    async def record(**kwargs):
        captured.append(kwargs)
        return await fake(**kwargs)
    orch, group = setup(store, make_router, record, tool_rounds=1)
    host = store.get_agent(group["host_agent_id"])
    await orch._agent_turn(group, host, [host], Collector(), RunState(group["id"], "time"))
    assert any(t["function"]["name"] == "current_time" for t in captured[0]["tools"])
    assert "tools" not in captured[-1]


async def test_unsupported_native_tools_retry_text_protocol_only_once(store, make_router):
    calls = []
    async def unsupported(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            raise ValueError("This model does not support tools")
        async def chunks():
            yield chunk("text protocol answer")
        return chunks()
    router = make_router(unsupported)
    for _ in range(2):
        reply = await router.complete([{"role": "user", "content": "test"}], tools=[{"type": "function"}], tool_choice="auto")
        assert reply.text == "text protocol answer"
    assert len(calls) == 3 and "tools" not in calls[-1] and "tool_choice" not in calls[-1]


async def test_truncated_response_never_becomes_executable_delivery(store, make_router):
    async def truncated(**kwargs):
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", delta=SimpleNamespace(content='<tool_call>{"name":"write_document","arguments":{"body":"unfinished'))])
        return chunks()
    with pytest.raises(AllRoutesFailed, match="cut off|截断"):
        await make_router(truncated).complete([{"role": "user", "content": "test"}])


def test_malformed_native_arguments_are_not_replaced_with_empty_object():
    _, calls = parse_tool_calls('<tool_call>{"name":"current_time","arguments":"broken json"}</tool_call>')
    assert calls[0].error


def test_small_image_mime_matches_actual_bytes_even_when_suffix_is_wrong():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, format="JPEG")
    data = buf.getvalue()
    assert attachments.shrink_image(data, "image/png", 8 * 1024 * 1024) == ("image/jpeg", data)


def test_long_voice_text_is_not_silently_truncated():
    with pytest.raises(localcmd.LocalToolError, match="1000"):
        localcmd.argv_for("qwen3tts", out="speech.wav", instruction="字" * 1001)


def test_generated_file_does_not_bypass_failed_required_review(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    workspace = store.workspace_dir(group["id"])
    (workspace / "picture.png").write_bytes(b"actual image bytes")
    task = PlanTask("t1", "artist", "artist", "image", "draw and check", tools=["generate_image", "review_picture"], deliverable="picture.png")
    message = {"meta": {"tools": [
        {"name": "generate_image", "status": "ok", "files": [{"name": "picture.png"}]},
        {"name": "review_picture", "status": "failed"}]}}
    assert "review_picture" in orch._task_shortfall(task, message, workspace)
    message["meta"]["tools"][-1]["status"] = "ok"
    assert not orch._task_shortfall(task, message, workspace)


def test_extra_nonexistent_review_typo_does_not_block_verified_deliverables(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    root = store.workspace_dir(group["id"])
    (root / "image").mkdir()
    (root / "image" / "output.png").write_bytes(b"synthetic")
    task = PlanTask("t", "artist", "artist", "Synthetic", "inspect", tools=["review_picture"], deliverable="output.png")
    calls = [
        {"name": "generate_image", "status": "ok", "files": [{"path": "image/output.png"}]},
        {"name": "review_picture", "status": "ok", "args": {"path": "image/output.png"}},
        {"name": "review_picture", "status": "failed", "args": {"path": "image/outpt.png"}},
    ]
    message = {"meta": {"tools": calls}}
    assert not orch._task_shortfall(task, message, root)
    calls[1]["args"]["path"] = "image/reference.png"
    assert "review_picture" in orch._task_shortfall(task, message, root)
    calls[1]["args"]["path"] = "image/output.png"
    calls[2]["status"] = "denied"
    assert "review_picture" in orch._task_shortfall(task, message, root)
    calls[2]["status"] = "failed"
    (root / "image" / "outpt.png").write_bytes(b"real unverified input")
    assert "review_picture" in orch._task_shortfall(task, message, root)


async def test_missing_review_retries_inspection_without_regenerating_delivered_media(store, make_router, monkeypatch):
    from app.toolhub import builtin_specs

    replies = [
        '<tool_call>{"name":"generate_image","arguments":{"prompt":"synthetic fixture"}}</tool_call>',
        "Synthetic image was saved to picture.png. Review remains pending.",
        '<tool_call>{"name":"review_picture","arguments":{"path":"picture.png","question":"synthetic check"}}</tool_call>',
        "Synthetic image inspected; use the existing picture.png.",
        "Synthetic handoff complete.",
    ]
    offered = []
    class RecordingLLM(FakeLLM):
        async def __call__(self, **kw):
            offered.append({t["function"]["name"] for t in kw.get("tools", [])})
            return await super().__call__(**kw)
    fake = RecordingLLM(default=lambda _: replies.pop(0))
    orch, group = setup(store, make_router, fake)
    host = store.get_agent(group["host_agent_id"])
    original_context = orch.toolhub.context
    async def context(*args, **kw):
        ctx = await original_context(*args, **kw)
        for name in ("generate_image", "review_picture"):
            ctx.tools[name] = {**builtin_specs()[name], "name": name, "source": "builtin"}
        return ctx
    monkeypatch.setattr(orch.toolhub, "context", context)
    generated, reviewed = [], []
    async def generate(ctx, args):
        generated.append(args)
        (store.workspace_dir(group["id"]) / "picture.png").write_bytes(b"synthetic fixture")
        return "Synthetic image saved", True, [{"kind": "image", "name": "picture.png", "bytes": 17}]
    async def review(ctx, args):
        reviewed.append(args)
        return "Synthetic inspection receipt", True, []
    monkeypatch.setattr(orch.toolhub, "_generate_image", generate)
    monkeypatch.setattr(orch.toolhub, "_review_picture", review)
    task = PlanTask("produce", host["name"], host["id"], "Synthetic image", "Make and inspect a fixture",
                    tools=["generate_image", "review_picture"], deliverable="picture.png")
    plan = planner.Plan("Synthetic review recovery", "", [task])
    await orch._execute_plan(group, [host], host, plan, RunState(group["id"], "synthetic"), Collector())
    assert len(generated) == len(reviewed) == 1 and task.status == "done"
    assert "review_picture" in offered[2] and "generate_image" not in offered[2]
    assert not {"assemble_video", "synthesize_speech"} & offered[2]
    message = store.get_message(task.message_id)
    assert message["meta"]["retry"] == "verification" and task.files == ["picture.png"]


def test_chat_member_cannot_be_given_a_different_members_speech_executable():
    members = [{"id": "reader", "name": "Reader"}]
    with pytest.raises(planner.PlanError, match="local:qwen3tts"):
        planner.build_plan({"tasks": [{"owner": "Reader", "instruction": "speak", "tools": ["local:qwen3tts"], "deliverable": "sample.wav"}]}, members, member_tools={"reader": {"library_read"}})


def test_production_deliverable_must_name_a_real_file():
    with pytest.raises(planner.PlanError, match="deliverable"):
        planner.build_plan({"tasks": [{"owner": "Writer", "instruction": "write", "tools": ["write_document"], "deliverable": "completed report"}]}, [{"id": "w", "name": "Writer"}], member_tools={"w": {"write_document"}})


def test_write_document_is_not_offered_a_format_it_cannot_write():
    """`.html` is a file name and still a task that cannot run — 2026-09-26, two dead rounds.

    The video group planned `t6`: an .html subtitle track for the HyperFrames chain, written with
    `write_document`. The validator's extension list did not carry `html`, so the task failed the
    "name your output file" rule — and the notice said exactly that. The model had named a file, so
    the notice was self-contradictory: it re-sent the plan, was rejected again, and the round ended
    with nobody assigned. Twice.

    What was actually true is that `write_document` writes four formats and `.html` is not one of
    them. That is what the message has to say, and it may only point at `run_code` when the member
    really has it (`code_enabled` is a switch, and this machine has it off).
    """
    task = {"id": "t6", "owner": "Writer", "instruction": "write the subtitle track",
            "tools": ["write_document"], "deliverable": "video/字幕轨.html"}
    with pytest.raises(planner.PlanError, match=r"cannot produce \.html"):
        planner.build_plan({"tasks": [task]}, [{"id": "w", "name": "Writer"}],
                           member_tools={"w": {"write_document"}})
    # No run_code in the roster → the notice must not send the host to a tool it cannot use.
    with pytest.raises(planner.PlanError, match="No tool available here"):
        planner.build_plan({"tasks": [task]}, [{"id": "w", "name": "Writer"}],
                           member_tools={"w": {"write_document"}})
    # With run_code available, the same rejection names the way out.
    with pytest.raises(planner.PlanError, match="run_code"):
        planner.build_plan({"tasks": [task]}, [{"id": "w", "name": "Writer"}],
                           member_tools={"w": {"write_document", "run_code"}})
    # And a version number is not a suffix: this one is "no file name", not "wrong format".
    with pytest.raises(planner.PlanError, match="is not a file name"):
        planner.build_plan({"tasks": [{**task, "deliverable": "v2.3 装配表"}]},
                           [{"id": "w", "name": "Writer"}], member_tools={"w": {"write_document"}})


async def test_missing_visual_service_blocks_review_not_independent_production(store, make_router, monkeypatch):
    from app.orchestrator import RunState, TurnOut
    from tests.test_collab import Collector

    orch, g = setup(store, make_router, FakeLLM(default="OK"))
    group = store.get_group(g["id"])
    host = store.get_agent(group["host_agent_id"])
    members = [host]
    plan = planner.build_plan({"tasks": [
        {"id": "produce", "owner": host["name"], "instruction": "Produce local pages",
         "tools": ["write_document"], "deliverable": "pages.md"},
        {"id": "review", "owner": host["name"], "instruction": "Review pages", "needs": ["produce"],
         "tools": ["review_picture"], "deliverable": "review opinion"},
        {"id": "release", "owner": host["name"], "instruction": "Release after review", "needs": ["review"]},
    ]}, members, known_tools={"write_document"}, member_tools={host["id"]: {"write_document"}})
    calls = []

    async def turn(group, agent, members, emit, run, **kw):
        tid = kw["extra_meta"]["task_id"]
        calls.append(tid)
        tool_calls = []
        if tid == "produce":
            (store.workspace_dir(group["id"]) / "pages.md").write_text("synthetic test output")
            tool_calls = [{"name": "write_document", "status": "ok", "files": [{"name": "pages.md"}]}]
        message = store.add_message(group["id"], "agent", agent["id"], agent["name"], "done",
                                    meta={"tools": tool_calls})
        return TurnOut("done", "done", message)
    monkeypatch.setattr(orch, "_agent_turn", turn)
    await orch._execute_plan(group, members, host, plan, RunState(gid=group["id"], user_text="test independent production"), Collector())
    assert calls == ["produce", "final"]
    assert [t.status for t in plan.tasks] == ["done", "skipped", "skipped"]
    assert "review_picture" in plan.tasks[1].error and plan.status == "failed"


async def test_model_failure_after_write_preserves_receipt_without_releasing_downstream(store, make_router):
    requests = []

    def respond(messages):
        requests.append(messages)
        if len(requests) == 1:
            return '<tool_call>{"name":"write_document","arguments":{"path":"partial.md","body":"Synthetic retained output"}}</tool_call>'
        raise RuntimeError("insufficient balance")

    orch, group = setup(store, make_router, FakeLLM(default=respond))
    host = store.get_agent(group["host_agent_id"])
    task = PlanTask("produce", host["name"], host["id"], "Produce", "Write a synthetic file",
                    tools=["write_document"], deliverable="partial.md")
    release = PlanTask("release", host["name"], host["id"], "Release", "Release result", needs=["produce"])
    plan = planner.Plan(goal="Retain partial evidence", conventions="", tasks=[task, release])
    collector = Collector()
    await orch._execute_plan(group, [host], host, plan, RunState(group["id"], "test receipts"), collector)

    assert task.status == "failed" and release.status == "skipped" and plan.status == "failed"
    assert "insufficient balance" in task.error and task.message_id
    message = next(m for m in store.list_messages(group["id"]) if m["id"] == task.message_id)
    assert message["meta"]["interrupted"] and message["meta"]["tools"][0]["status"] == "ok"
    assert message["meta"]["tools"][0]["files"] and task.files == ["partial.md"]
    assert (store.workspace_dir(group["id"]) / "partial.md").read_text().strip() == "Synthetic retained output"
    assert not any((m.get("meta") or {}).get("retry") == "delivery" for m in store.list_messages(group["id"]))
    assert not any(e["type"] == "message_discard" and e["message_id"] == task.message_id for e in collector.events)


def test_audio_deliverable_cannot_be_satisfied_by_successful_library_read(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    task = PlanTask("t", "reader", "r", "voice", "speak", deliverable="配音/旁白试制-段1.wav（Qwen3-TTS 生成）")
    message = {"meta": {"tools": [{"name": "library_read", "status": "ok"}, {"name": "Qwen3-TTS", "status": "failed"}]}}
    assert orch._task_shortfall(task, message, store.workspace_dir(group["id"]))


def test_chinese_word_count_excludes_headings_and_reference_section():
    assert docwrite.text_metrics("# 标题\n\n正文 ABC\n## 参考文献\n这不是正文") == {"han_chars": 2, "nonspace_chars": 5}


async def test_speech_tool_delivers_exact_text_and_rechecks_execution_switch(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), external_agents_enabled=True)
    monkeypatch.setattr(voices, "names", lambda: ["test"])
    monkeypatch.setattr(voices, "parse", lambda value: ("clone", {"name": "test"}))
    heard = []
    async def speak(text, dest, voice, timeout):
        heard.append((text, voice))
        dest.write_bytes(b"RIFFaudio")
        return 2.5
    monkeypatch.setattr(assemble, "speak", speak)
    ctx = await orch.toolhub.context(group, store.get_agent(group["host_agent_id"]), connect=False)
    result = await orch.toolhub.call(ctx, "synthesize_speech", {"text": "完整旁白。", "voice": "voice:test"})
    assert result.ok and heard == [("完整旁白。", "voice:test")]
    assert result.files[0]["kind"] == "audio" and result.files[0]["seconds"] == 2.5
    store.update_settings({"external_agents_enabled": False})
    assert not (await orch.toolhub.call(ctx, "synthesize_speech", {"text": "x", "voice": "voice:test"})).ok
    assert len(heard) == 1


async def test_chat_video_model_override_requires_configured_enabled_workflow(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), video_enabled=True)
    provider = store.add_provider_from_preset("comfyui")
    store.update_settings({"video_provider_id": provider["id"]})
    ctx = await orch.toolhub.context(group, store.get_agent(group["host_agent_id"]), connect=False)
    _, ok, _ = await orch.toolhub._generate_video(ctx, {"prompt": "person", "model": "unregistered"})
    assert not ok
    model = store.add_model(provider["id"], "wan2.2-custom-audio", "Custom")
    async def ready(*a, **kw): return True, ""
    monkeypatch.setattr(orch.toolhub.comfy_runtime, "ensure", ready)
    chosen = []
    def graph(name):
        chosen.append(name)
        return {"graph": {"1": {"inputs": {"audio": "{{audio}}"}}}}
    monkeypatch.setattr(comfyui, "workflow_of", graph)
    # Getting to file validation proves audio was accepted and the requested
    # workflow selected, without sending a render or reading outside the group.
    text, ok, _ = await orch.toolhub._generate_video(ctx, {"prompt": "person", "model": model["model_name"], "reference_audios": ["../outside.wav"]})
    assert not ok and chosen == [model["model_name"]]
    assert "outside" in text or "之外" in text


async def test_voice_reference_reaches_actual_command_and_cannot_escape_group(tmp_path):
    script = tmp_path / "tts"
    script.write_text("#!/usr/bin/env python3\nimport sys\nprint(repr(sys.argv[1:]))\n")
    script.chmod(0o755)
    ref = tmp_path / "reference.wav"
    ref.write_bytes(b"test")
    result = await localcmd.run("qwen3tts", workspace=tmp_path, instruction="Read exactly this.",
                               timeout=10, cli_path=str(script), ref_audio=ref.name, ref_text="reference words")
    assert result["ok"] and "--ref-audio" in result["command"]
    assert str(ref) in result["command"] and "reference words" in result["command"]
    with pytest.raises(localcmd.LocalToolError):
        await localcmd.run("qwen3tts", workspace=tmp_path, instruction="x", timeout=10,
                           cli_path=str(script), ref_audio="../outside.wav")

async def test_planning_cannot_overwrite_deliverables_before_task_execution(store, make_router):
    fake = FakeLLM(default='<tool_call>{"name":"write_document","arguments":{"path":"draft.md","body":"Overwritten"}}</tool_call>')
    orch, group = setup(store, make_router, fake, tool_rounds=1)
    host = store.get_agent(group["host_agent_id"])
    work = store.workspace_dir(group['id'])
    work.mkdir(parents=True, exist_ok=True)
    (work / 'draft.md').write_text('Already approved source')
    out = await orch._agent_turn(group, host, [host], Collector(), RunState(group['id'], 'plan'),
                                extra_meta={'phase': 'planning'})
    assert (work / 'draft.md').read_text() == 'Already approved source'
    assert not any(c.get('status') == 'ok' for c in out.message['meta'].get('tools', []))


async def test_planning_budget_finishes_with_a_plan_instead_of_a_missing_delivery_summary(store, make_router):
    prompts = []
    def response(messages):
        prompts.append(messages[-1]['content'])
        if len(prompts) == 1:
            return '<tool_call>{"name":"list_workspace_files","arguments":{"query":"source"}}</tool_call>'
        assert '<plan>' in prompts[-1]
        return '<plan>{"tasks":[{"owner":"Aide","instruction":"Use source.txt to produce a synthetic report"}]}</plan>'
    orch, group = setup(store, make_router, FakeLLM(default=response), tool_rounds=1)
    host = store.get_agent(group['host_agent_id'])
    work = store.workspace_dir(group['id'])
    work.mkdir(parents=True, exist_ok=True)
    (work / 'source.txt').write_text('Synthetic source')
    out = await orch._agent_turn(group, host, [host], Collector(), RunState(group['id'], 'plan'),
                                extra_meta={'phase': 'planning'})
    assert len(prompts) == 2 and all('<plan>' in p for p in prompts)
    assert planner.extract_plan_json(out.raw)['tasks']
    assert out.message['meta']['tools'][0]['status'] == 'ok'
    assert list(work.iterdir()) == [work / 'source.txt']


async def test_workspace_read_honors_the_advertised_character_offset(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    ctx = await orch.toolhub.context(group, store.get_agent(group['host_agent_id']))
    work = store.workspace_dir(group['id'])
    work.mkdir(parents=True, exist_ok=True)
    (work / 'long.md').write_text('甲' * 3100 + 'REFERENCE-SENTINEL' + '乙' * 200)
    first, ok = await orch.toolhub._builtin(ctx, 'library_read', {'doc': 'long.md'})
    assert ok and 'REFERENCE-SENTINEL' not in first and 'start=3000' in first
    page, ok = await orch.toolhub._builtin(ctx, 'library_read', {'doc': 'long.md', 'start': 3100, 'limit': 30})
    assert ok and 'REFERENCE-SENTINEL' in page and '甲' not in page and 'start=3130' in page


async def test_document_conversion_preserves_source_and_blocks_escape(store, make_router, tmp_path):
    from docx import Document
    orch, group = setup(store, make_router, FakeLLM())
    ctx = await orch.toolhub.context(group, store.get_agent(group['host_agent_id']))
    work = store.workspace_dir(group['id'])
    work.mkdir(parents=True, exist_ok=True)
    body = '# Source title\n\n## 摘要\n\n' + '完整正文' * 1000 + '\n\n## 参考文献\n\nSource 1.'
    (work / 'draft.md').write_text(body)
    text, ok, files = await orch.toolhub._write_document(ctx, {'path': 'final.docx', 'source_path': 'draft.md'})
    assert ok and files and '4000' in text
    paragraphs = [p.text for p in Document(work / 'final.docx').paragraphs]
    assert paragraphs.count('Source title') == 1
    assert '完整正文' * 1000 in paragraphs
    outside = tmp_path / 'outside.md'
    outside.write_text('Not a group source')
    (work / 'escape.md').symlink_to(outside)
    _, ok, _ = await orch.toolhub._write_document(ctx, {'path': 'escape.docx', 'source_path': 'escape.md'})
    assert not ok and not (work / 'escape.docx').exists()
    _, ok, _ = await orch.toolhub._write_document(ctx, {'path': 'mixed.md', 'source_path': 'draft.md', 'body': 'Ambiguous'})
    assert not ok


def test_reviewing_reference_does_not_clear_failed_generated_audio(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    task = PlanTask(id='t1', owner='Aide', owner_id=group['host_agent_id'], title='Review', instruction='Review generated audio', tools=['review_audio'])
    calls = [{'name': 'review_audio', 'args': {'path': 'generated.wav'}, 'status': 'failed'},
             {'name': 'review_audio', 'args': {'path': 'reference.wav'}, 'status': 'ok'}]
    message = {'meta': {'tools': calls}}
    assert orch._task_shortfall(task, message, store.workspace_dir(group['id']))
    calls.append({'name': 'review_audio', 'args': {'path': 'generated.wav'}, 'status': 'ok'})
    assert not orch._task_shortfall(task, message, store.workspace_dir(group['id']))

async def test_explicit_media_prompt_does_not_trigger_unrelated_library_rewrite(store, make_router, monkeypatch):
    from app import media
    from app.toolhub import ToolContext, ToolOutcome
    orch, group = setup(store, make_router, FakeLLM(), video_enabled=True, external_calls_enabled=True)
    host = store.get_agent(group['host_agent_id'])
    agent = dict(host, origin='media', name='Video')
    target = {'use': 'video', 'provider': {'id': 'comfyui', 'kind': 'comfyui', 'is_local': True},
              'model': {'model_name': comfyui.DEFAULT_WORKFLOW}}
    monkeypatch.setattr(media, 'member_target', lambda *args: target)
    async def no_rewrite(*args, **kwargs):
        pytest.fail('Explicit generation prompt must not be rewritten or searched')
    monkeypatch.setattr(orch, '_media_prompt', no_rewrite)
    async def context(*args, **kwargs):
        return ToolContext(group, agent)
    monkeypatch.setattr(orch.toolhub, 'context', context)
    seen = []
    async def call(ctx, name, args, approve):
        seen.append(args)
        return ToolOutcome(text='Generated clip', ok=True)
    monkeypatch.setattr(orch.toolhub, 'call', call)
    out = await orch._agent_turn(group, agent, [agent], Collector(), RunState(group['id'], 'make clip'),
        extra_meta={'arguments': {'prompt': 'Keep the reference person unchanged', 'duration_seconds': 2}})
    assert out and seen[0]['prompt'] == 'Keep the reference person unchanged'
