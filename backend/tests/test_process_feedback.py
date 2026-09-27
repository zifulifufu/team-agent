"""Exercise the hidden engineer's feedback/re-run loop against actual task delivery."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import proclog, scoring
from app.orchestrator import RunState
from app.toolhub import ToolOutcome
from tests.conftest import FakeLLM, PLAN_MODE, TASK_HEAD, has
from tests.test_collab import Collector, call, setup


async def test_failed_delivery_feedback_real_rerun_and_recurrence(store, make_router):
    mode = {"write": False, "title": "报告"}
    def script(messages):
        last = messages[-1]["content"]
        if has(last, PLAN_MODE):
            return "<plan>" + json.dumps({"goal": "交付报告", "tasks": [{
                "id": "t1", "owner": "Copywriter", "title": mode["title"],
                "instruction": "写报告", "deliverable": "报告.md"}]}, ensure_ascii=False) + "</plan>"
        if has(last, TASK_HEAD) and mode["write"]:
            return call("write_document", path="报告.md", body="# 已交付\n真实文件")
        return "完成"

    fake = FakeLLM(default=script)
    orch, group = setup(store, make_router, fake, process_review=False, scoring_enabled=False)
    gid = group["id"]
    path = proclog.unit(store.workspace_dir(gid))
    # An old file with the right name must not conceal a failed new delivery.
    (store.workspace_dir(gid) / "报告.md").write_text("old file")
    await orch.handle_user_message(gid, "交付报告", Collector())
    entries = proclog.read(path)
    target = next(e for e in entries if e.key.startswith("missing-file:"))
    assert target.status == "open"
    target.fix = "请先落盘,再把文件交给审稿成员"
    proclog.write(path, entries)

    # A different task, even with the same t1, owner and output name, cannot verify it.
    mode.update(write=True, title="不同任务")
    await orch.handle_user_message(gid, "另一个任务", Collector())
    entry = next(e for e in proclog.read(path) if e.id == target.id)
    assert entry.status == "open" and entry.advised == 1
    assert any("请先落盘,再把文件交给审稿成员" in m["content"]
               for _, messages in fake.calls for m in messages if m["role"] == "system")

    mode["title"] = "报告"
    await orch.handle_user_message(gid, "重跑原任务", Collector())
    entry = next(e for e in proclog.read(path) if e.id == target.id)
    assert entry.status == "verified", entry
    assert "message=" in entry.verify and "报告.md" in entry.verify
    assert entry.advised == 2

    mode["write"] = False
    await orch.handle_user_message(gid, "再次交付", Collector())
    entries = [e for e in proclog.read(path) if e.key == target.key]
    assert len(entries) == 1 and entries[0].status == "open" and entries[0].seen == 2
    assert not entries[0].verify
    assert any(h.startswith("reopened@") for h in entries[0].history)
    await orch.drain()


def test_verified_cannot_be_claimed_without_evidence_or_for_machine_checks():
    entries = []
    manual = proclog.report(entries, title="manual", symptom="s")
    automatic = proclog.report(entries, title="auto", symptom="s", check={"kind": "task", "scope": "s"})
    with pytest.raises(ValueError):
        proclog.update(entries, manual.id, status="verified")
    with pytest.raises(ValueError):
        proclog.update(entries, automatic.id, status="verified", verify="I say it works")
    assert all(e.status == "open" for e in entries)


async def test_review_merges_current_ledger_and_restricts_ids(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), process_review=True)
    path = proclog.unit(store.workspace_dir(group["id"]))
    entries = []
    first = proclog.report(entries, title="first", symptom="missing")
    other = proclog.report(entries, title="unrequested", symptom="s")
    proclog.write(path, entries)
    started, resume = asyncio.Event(), asyncio.Event()
    async def complete(messages, **kw):
        assert kw["only"] == "independent" and "preferred" not in kw
        started.set()
        await resume.wait()
        return SimpleNamespace(text=json.dumps([
            {"id": first.id, "cause": "model cause", "fix": "repair the input"},
            {"id": other.id, "cause": "unrequested edit", "fix": "bad"}]))
    monkeypatch.setattr(scoring, "pick_judge", lambda *a: "independent")
    monkeypatch.setattr(orch.router, "complete", complete)
    task = asyncio.create_task(orch._process_review(group, [first.id]))
    await started.wait()
    async with orch._lock(group["id"]):
        current = proclog.read(path)
        current[0].cause = "manual cause during review"
        third = proclog.report(current, title="new observation", symptom="s")
        proclog.write(path, current)
    resume.set()
    await task
    back = {e.id: e for e in proclog.read(path)}
    assert third.id in back and back[first.id].cause == "manual cause during review"
    assert back[first.id].fix == "repair the input" and back[first.id].review_state == "reviewed"
    assert not back[other.id].cause and not back[other.id].fix


async def test_unavailable_review_is_visible_and_retried_next_round(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM())
    path = proclog.unit(store.workspace_dir(group["id"]))
    entries = []
    first = proclog.report(entries, title="failure", symptom="s", hint="use a real tool")
    proclog.write(path, entries)
    monkeypatch.setattr(scoring, "pick_judge", lambda *a: "")
    await orch._process_review(group, [first.id])
    assert proclog.read(path)[0].review_state == "unavailable"
    # A subsequent clean round still schedules incomplete work; there need not be a new defect.
    assert first.id in orch._process_record(group, RunState(group["id"], "retry"))
    assert "use a real tool" in proclog.feedback(proclog.read(path))[0]


async def test_cancelled_round_keeps_already_measured_failures(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), process_review=False)
    async def run(group, text, emit, state):
        state.defects.append({"key": "observed", "title": "failed before stop", "symptom": "failed"})
        raise asyncio.CancelledError()
    monkeypatch.setattr(orch, "_run_turns", run)
    collector = Collector()
    with pytest.raises(asyncio.CancelledError):
        await orch.handle_user_message(group["id"], "work", collector)
    assert collector.events[-1]["type"] == "idle"
    assert proclog.read(proclog.unit(store.workspace_dir(group["id"])))[0].key == "observed"


def test_bounded_feedback_and_verification_do_not_leak_to_other_operations():
    entries = []
    for i in range(9):
        proclog.report(entries, title=str(i), symptom="s", key=f"k{i}",
                       check={"kind": "tool", "scope": str(i)})
    text, ids = proclog.feedback(entries)
    assert len(ids) == 6 and len(text) < 6500
    proclog.settle(entries, [{"check": {"kind": "tool", "scope": "1"}, "ok": True,
                             "evidence": "tool call succeeded"}], {"k1"}, ids, "r1")
    assert all(e.status == "open" for e in entries)  # same-round failure blocks verification
    proclog.settle(entries, [{"check": {"kind": "tool", "scope": "1"}, "ok": True,
                             "evidence": "tool call succeeded"}], set(), [], "r2")
    assert entries[1].status == "verified" and entries[0].status == "open"


async def test_single_tool_failure_in_unplanned_chat_is_recorded_and_rechecked(store, make_router, monkeypatch):
    def script(messages):
        if '<tool_result ' in messages[-1]['content']:
            return '工具结果已收到'
        return call('write_document', path='工具输出.md', body='real output')
    orch, group = setup(store, make_router, FakeLLM(default=script),
                        process_review=False, scoring_enabled=False, plan_mode='off')
    original = orch.toolhub.call
    async def fail(*args, **kwargs):
        return ToolOutcome(text='Temporary dependency failure', ok=False, ms=0)
    monkeypatch.setattr(orch.toolhub, 'call', fail)
    await orch.handle_user_message(group['id'], '@Copywriter 写文件', Collector())
    path = proclog.unit(store.workspace_dir(group['id']))
    entry = next(e for e in proclog.read(path) if e.key.startswith('tool-failed:'))
    assert entry.check['kind'] == 'tool' and entry.status == 'open'
    monkeypatch.setattr(orch.toolhub, 'call', original)
    await orch.handle_user_message(group['id'], '@Copywriter 写文件', Collector())
    entry = next(e for e in proclog.read(path) if e.id == entry.id)
    assert entry.status == 'verified' and 'write_document: ok' in entry.verify
    await orch.drain()


async def test_review_failure_does_not_fallback_and_can_recover(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM())
    path = proclog.unit(store.workspace_dir(group['id']))
    entries = []
    entry = proclog.report(entries, title='failure', symptom='s')
    proclog.write(path, entries)
    monkeypatch.setattr(scoring, 'pick_judge', lambda *a: 'independent')
    seen = []
    async def fail(messages, **kw):
        seen.append(kw)
        raise RuntimeError('unreachable')
    monkeypatch.setattr(orch.router, 'complete', fail)
    await orch._process_review(group, [entry.id])
    assert len(seen) == 1 and seen[0]['only'] == 'independent'
    assert proclog.read(path)[0].review_state == 'failed'
    async def recover(messages, **kw):
        return SimpleNamespace(text=json.dumps([{'id': entry.id, 'cause': 'missing input', 'fix': 'supply input'}]))
    monkeypatch.setattr(orch.router, 'complete', recover)
    await orch._process_review(group, orch._process_record(group, RunState(group['id'], 'retry')))
    assert proclog.read(path)[0].review_state == 'reviewed'
