"""Regression checks for dependency, lifecycle and trust boundaries in team work."""
import asyncio
import json

import pytest
import httpx
from fastapi.testclient import TestClient

from app import planner, scoring
from app.main import create_app
from tests.conftest import FakeLLM, TASK_HEAD, has
from tests.test_collab import Collector, last_user, plan_script, role, setup


MEMBERS = [{"id": "a", "name": "WriterOne", "role": "Writer"},
           {"id": "b", "name": "WriterTwo", "role": "Writer"}]


@pytest.mark.parametrize("tasks,limit", [
    ([{"id": "a", "needs": ["missing"]}], 8),
    ([{"id": "a", "needs": ["a"]}], 8),
    ([{"id": "a"}, {"id": "a"}], 8),
    ([{"id": "a"}, {"id": "b"}], 1),
])
def test_invalid_dependencies_are_rejected_not_rewritten(tasks, limit):
    rows = [{"owner": "WriterOne", "instruction": "work", **t} for t in tasks]
    with pytest.raises(planner.PlanError):
        planner.build_plan({"tasks": rows}, MEMBERS, limit)


@pytest.mark.parametrize("owner", ["Writer", "writer", "WriterO or WriterT"])
def test_ambiguous_owner_is_not_assigned_to_first_member(owner):
    with pytest.raises(planner.PlanError):
        planner.build_plan({"tasks": [{"owner": owner, "instruction": "work"}]}, MEMBERS)


async def test_dependency_failure_skips_descendants_but_runs_independent_work(store, make_router):
    spec = json.dumps({"tasks": [
        {"id": "a", "owner": "Copywriter", "instruction": "draft"},
        {"id": "b", "owner": "Proofreader", "instruction": "review", "needs": ["a"]},
        {"id": "c", "owner": "Aide", "instruction": "independent checklist"},
        {"id": "d", "owner": "Proofreader", "instruction": "publish", "needs": ["b"]},
    ]})
    normal = plan_script(spec)

    def reply(messages):
        if role(messages) == "Copywriter" and has(last_user(messages), TASK_HEAD):
            raise RuntimeError("draft unavailable")
        return normal(messages)

    fake = FakeLLM(default=reply)
    orch, g = setup(store, make_router, fake)
    await orch.handle_user_message(g["id"], "prepare a notice", Collector())
    board = next(m["meta"] for m in store.list_messages(g["id"]) if m["sender_type"] == "plan")
    assert [t["status"] for t in board["tasks"]] == ["failed", "skipped", "done", "skipped"]
    assert board["status"] == "failed"
    assert not any(role(m) == "Proofreader" for _, m in fake.calls)
    assert board["tasks"][1]["error"] and board["tasks"][3]["error"]


async def test_repeated_plans_keep_deliveries_in_distinct_folders(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default=plan_script()))
    await orch.handle_user_message(g["id"], "first draft", Collector())
    await orch.handle_user_message(g["id"], "second draft", Collector())
    boards = [m["meta"] for m in store.list_messages(g["id"]) if m["sender_type"] == "plan"]
    paths = [t["dir"] for b in boards for t in b["tasks"]]
    assert len(paths) == 4 and len(set(paths)) == 4
    assert all((store.workspace_path(g["id"]) / p).is_dir() for p in paths)


def test_judge_excludes_assigned_member_models(store, make_router):
    orch, g = setup(store, make_router, FakeLLM())
    store.update_settings({"external_calls_enabled": False})
    store.update_agent(g["member_ids"][0], {"model_id": "ollama/qwen2.5:7b"})
    assert scoring.pick_judge(store, orch.router, g) == ""


def test_dev_api_blocks_untrusted_simple_requests(tmp_path):
    app = create_app(tmp_path / "api", token="")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.get("/api/groups").json()[0]["id"]
        assert c.post(f"/api/groups/{gid}/stop", headers={"Origin": "https://untrusted.example"}).status_code == 403
        assert c.post(f"/api/groups/{gid}/stop", headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_host_must_exist_and_belong_to_group(tmp_path):
    app = create_app(tmp_path / "api", token="")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        agents = c.get("/api/agents").json()
        gid = c.post("/api/groups", json={"name": "test", "member_ids": [agents[0]["id"]]}).json()["id"]
        for aid in ["missing", agents[1]["id"]]:
            assert c.patch(f"/api/groups/{gid}", json={"host_agent_id": aid}).status_code == 400


def test_media_generator_cannot_be_appointed_host(tmp_path):
    app = create_app(tmp_path / "api", token="")
    member = app.state.store.create_agent("Generator", origin="media")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.post("/api/groups", json={"name": "media", "member_ids": [member["id"]]}).json()["id"]
        assert c.patch(f"/api/groups/{gid}", json={"host_agent_id": member["id"]}).status_code == 400
        assert c.post("/api/groups", json={"name": "bad", "member_ids": [member["id"]],
                                          "host_agent_id": member["id"]}).status_code == 400


async def test_all_message_sources_share_busy_and_cancellation_tracking(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(), plan_mode="off")
    c = Collector()
    lock = orch._lock(g["id"])
    await lock.acquire()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "channel message", c, read_only=True))
    try:
        for _ in range(100):
            if c.events:
                break
            await asyncio.sleep(0)
        assert task in orch.tasks[g["id"]]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        lock.release()
    assert not orch.tasks.get(g["id"])


def test_restart_marks_abandoned_plan_stopped(tmp_path):
    app = create_app(tmp_path / "api", token="")
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    m = store.add_message(gid, "plan", None, "Task board", "old board", meta={
        "kind": "plan", "status": "running", "tasks": [
            {"id": "a", "status": "done"}, {"id": "b", "status": "running"},
            {"id": "c", "status": "pending"}]})
    restarted = create_app(tmp_path / "api", token="")
    meta = restarted.state.store.get_message(m["id"])["meta"]
    assert meta["status"] == "stopped"
    assert [t["status"] for t in meta["tasks"]] == ["done", "stopped", "skipped"]


async def test_queued_round_does_not_emit_idle_while_another_round_is_running(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(), plan_mode="off")
    c = Collector()
    lock = orch._lock(g["id"])
    await lock.acquire()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_turn(*args):
        entered.set()
        await release.wait()

    orch._run_turns = wait_turn
    a = asyncio.create_task(orch.handle_user_message(g["id"], "first", c))
    b = asyncio.create_task(orch.handle_user_message(g["id"], "second", c))
    try:
        while len([e for e in c.events if e["type"] == "message"]) < 2:
            await asyncio.sleep(0)
        lock.release()
        await asyncio.wait_for(entered.wait(), 2)
        await a
        assert not any(e["type"] == "idle" for e in c.events)
        release.set()
        await b
        assert sum(e["type"] == "idle" for e in c.events) == 1
    finally:
        a.cancel()
        b.cancel()
        await asyncio.gather(a, b, return_exceptions=True)


async def test_busy_project_cannot_be_mutated_and_stop_waits_for_cleanup(tmp_path):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(**kw):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app = create_app(tmp_path / "api", completion_fn=slow, token="")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            g = (await c.get("/api/groups")).json()[0]
            root = f"/api/groups/{g['id']}"
            await c.post(root + "/messages", json={"text": "@Copywriter wait"})
            await asyncio.wait_for(entered.wait(), 2)
            assert (await c.get(root + "/status")).json()["busy"] is True
            assert (await c.patch(root, json={"workspace": str(tmp_path)})).status_code == 409
            assert (await c.delete(root + "/messages")).status_code == 409
            assert (await c.delete(root + "/members/" + g["member_ids"][0])).status_code == 409
            assert (await c.delete(root)).status_code == 409
            assert (await c.post(root + "/stop")).json()["cancelled"] == 1
            assert cancelled.is_set()
            assert (await c.get(root + "/status")).json()["busy"] is False
            assert (await c.patch(root, json={"workspace": str(tmp_path)})).status_code == 200


async def test_shutdown_cancels_active_rounds(tmp_path):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(**kw):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app = create_app(tmp_path / "api", completion_fn=slow, token="")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            gid = (await c.get("/api/groups")).json()[0]["id"]
            await c.post(f"/api/groups/{gid}/messages", json={"text": "@Copywriter wait"})
            await asyncio.wait_for(entered.wait(), 2)
    assert cancelled.is_set()
    assert not app.state.orch.tasks.get(gid)


async def test_explicit_judge_cannot_grade_its_actual_fallback_output(store, make_router):
    from tests.test_scoring import make_plan, ENOUGH
    orch, g = setup(store, make_router, FakeLLM())
    store.update_settings({"scoring_enabled": True, "score_judge_model": "ollama/qwen2.5:7b"})
    card = await scoring.score_round(store, orch.router, g, make_plan(), {"t1": ENOUGH},
                                     [{"model": "ollama/qwen2.5:7b", "agent": "Copywriter"}])
    assert not card["judge"] and "participated" in card["judge_error"]


async def test_scoring_respects_memory_disabled(store, make_router):
    from tests.test_scoring import wire, reply, make_plan, ENOUGH
    orch, g, _ = wire(store, make_router, reply(t1={"delivered": 0, "usable": 0, "reason": "Missing output"}))
    store.update_settings({"memory_enabled": False})
    card = await scoring.score_round(store, orch.router, g, make_plan(), {"t1": ENOUGH}, [])
    assert card["judge"] and not card["lessons"]
    assert not store.list_memories("group", g["id"], "lesson")


async def test_active_round_does_not_read_a_later_queued_request(store, make_router):
    entered, release = asyncio.Event(), asyncio.Event()
    fake = FakeLLM(default=plan_script())
    orch, g = setup(store, make_router, fake)
    original = orch._execute_plan

    async def pause_plan(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    orch._execute_plan = pause_plan
    c = Collector()
    first = asyncio.create_task(orch.handle_user_message(g["id"], "FIRST_ROUND notice", c))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(orch.handle_user_message(g["id"], "SECOND_ROUND unrelated", c))
        while not any(e["type"] == "message" and e["message"]["content"] == "SECOND_ROUND unrelated" for e in c.events):
            await asyncio.sleep(0)
        release.set()
        await first
        workers = [m for _, m in fake.calls if role(m) == "Copywriter" and has(last_user(m), TASK_HEAD)]
        assert workers and "FIRST_ROUND" in json.dumps(workers[0])
        assert "SECOND_ROUND" not in json.dumps(workers[0])
        await second
    finally:
        first.cancel()
        if second:
            second.cancel()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


async def test_cancelled_channel_round_does_not_send_partial_reply(store, make_router, monkeypatch):
    from app import api_channels, channels
    from tests.test_channels import FakeHub
    orch, g = setup(store, make_router, FakeLLM())
    entered = asyncio.Event()
    sent = []

    async def partial(gid, text, emit, **kwargs):
        await emit({"type": "message_end", "message": {"sender_type": "agent", "content": "unfinished draft"}})
        entered.set()
        await asyncio.Event().wait()

    async def send(*args):
        sent.append(args)
        return True, "ok"

    monkeypatch.setattr(orch, "handle_user_message", partial)
    monkeypatch.setattr(channels, "send", send)
    channel = api_channels.build_channels(store, orch, FakeHub())
    item = channels.Inbound(sender="test", name="Test", text="draft", message_id="test-id")
    task = asyncio.create_task(channel._round("telegram", item, {"group_id": g["id"]}))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sent


@pytest.mark.parametrize("cancel", [False, True])
async def test_optional_scoring_cannot_undo_completed_work(store, make_router, monkeypatch, cancel):
    entered = asyncio.Event()

    async def fail_or_wait(*args):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("optional scoring unavailable")

    monkeypatch.setattr(scoring, "score_round", fail_or_wait)
    orch, g = setup(store, make_router, FakeLLM(default=plan_script()))
    task = asyncio.create_task(orch.handle_user_message(g["id"], "write a notice", Collector()))
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    board = next(m["meta"] for m in store.list_messages(g["id"]) if m["sender_type"] == "plan")
    assert board["status"] == "done"


async def test_channel_shutdown_waits_for_connection_cleanup(store, make_router):
    from app import api_channels
    from tests.test_channels import FakeHub
    orch, _ = setup(store, make_router, FakeLLM())
    channel = api_channels.build_channels(store, orch, FakeHub())
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def connection():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(connection())
    channel.tasks.add(task)
    await entered.wait()
    await channel.shutdown()
    assert task.done() and cleaned.is_set()


# ------------------------------------------------------- the roster's own order (2026-09-25)
def test_the_host_leads_the_roster(store):
    """`group_members.position` is the order every roster is built from — the queue a round walks,
    the member list in the dock, the planner's roster. A host sitting last still *is* the host (the
    code reads the column), but every list the user reads contradicts it; the live group that
    prompted this had its host in last place while the user addressed it by name three times.

    Both ways in are covered: the group being created with a host that is not the first id, and the
    host being changed afterwards.
    """
    ids = [a["id"] for a in store.list_agents()][:3]
    a, b, host = ids

    g = store.create_group("T-host-first", host_agent_id=host, member_ids=[a, b, host])
    assert store.member_ids(g["id"])[0] == host, "a new group's host does not lead it"

    store.update_group(g["id"], {"host_agent_id": b})
    order = store.member_ids(g["id"])
    assert order[0] == b, "changing the host does not move it to the front"
    assert sorted(order) == sorted([a, b, host]), "moving the host lost or duplicated a member"


def test_removing_a_member_leaves_no_hole_in_the_order(store):
    """`position` has to stay a real index. Measured: removing the second of four members left
    `[0, 2, 3]`, which reads as an order with a gap in it and is a trap for anything that treats the
    column as a position in a list."""
    ids = [a["id"] for a in store.list_agents()][:4]
    g = store.create_group("T-remove", host_agent_id=ids[0], member_ids=ids)

    rows = lambda: store._q(  # noqa: SLF001 — the column is the claim, so it is read directly
        "SELECT position FROM group_members WHERE group_id=? ORDER BY position", (g["id"],))

    store.remove_member(g["id"], ids[1])
    assert [r["position"] for r in rows()] == list(range(len(rows())))
    assert ids[1] not in store.member_ids(g["id"])

    # …and taking the host out clears the column in the same breath
    store.remove_member(g["id"], ids[0])
    assert store.get_group(g["id"])["host_agent_id"] is None
    assert [r["position"] for r in rows()] == list(range(len(rows())))
