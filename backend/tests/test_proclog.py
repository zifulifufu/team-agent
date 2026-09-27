"""The process log: the ledger of what is wrong with the way a group works.

What is defended here is not "a file was written". It is the three things that make such a ledger
worth keeping at all:

  * an entry survives the round trip — written, read back, still saying what it said, ids stable, so
    a later `update` finds the entry it means;
  * the states mean what they say — nothing moves to `verified` as a side effect, and a change that
    was never re-checked stays distinguishable from one that was;
  * `scan` reports what the app measured, including the absences (no tool calls at all, no task
    board, a promised file that is not on disk), because "nothing happened" and "everything was
    fine" must not read the same.

The last two tests drive the real tool through the hub, which is what a member actually calls.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from app import proclog


# ------------------------------------------------------------------ the ledger itself
def test_an_entry_round_trips_through_the_file(tmp_path):
    path = proclog.unit(tmp_path)
    entries = proclog.read(path)
    proclog.report(
        entries, title="计划格式错误之后没有人被指派", symptom="写到文件的任务最后只在群里说了一句话",
        evidence="系统提示「计划格式不对」;工作目录里 0 个文件", severity="major", stage="planning",
        cause="解析失败直接降级成接力,没有重试", fix="解析失败后回问一次")
    proclog.write(path, entries, "测试群")

    back = proclog.read(path)
    assert len(back) == 1
    e = back[0]
    assert e.id == "P-20260924-1" or e.id.startswith("P-")
    assert e.title == "计划格式错误之后没有人被指派"
    assert e.severity == "major" and e.stage == "planning" and e.status == "open"
    assert "系统提示" in e.evidence and "没有重试" in e.cause
    assert e.found and e.history, e


def test_ids_count_the_entries_of_the_day_and_survive_a_rewrite(tmp_path):
    path = proclog.unit(tmp_path)
    entries = proclog.read(path)
    first = proclog.report(entries, title="甲", symptom="x")
    second = proclog.report(entries, title="乙", symptom="y")
    assert (first.id, second.id) == (f"P-{first.id.split('-')[1]}-1", f"P-{first.id.split('-')[1]}-2")

    proclog.write(path, entries)
    again = proclog.read(path)
    assert [e.id for e in again] == [first.id, second.id]     # a rewrite keeps the ids
    third = proclog.report(again, title="丙", symptom="z")
    assert third.id.endswith("-3")                            # and the counter keeps counting


def test_a_state_only_changes_when_it_is_told_to_and_the_history_grows(tmp_path):
    path = proclog.unit(tmp_path)
    entries = proclog.read(path)
    entry = proclog.report(entries, title="工具反复失败", symptom="同一个参数失败两次", stage="tool")
    assert entry.status == "open"

    moved = proclog.update(entries, entry.id, status="fixed", note="改了提示词里的口径")
    assert moved is entry and moved.status == "fixed"
    assert moved.history[0].startswith("open@") and moved.history[-1].startswith("fixed@")
    assert "改了提示词" in moved.fix

    checked = proclog.update(entries, entry.id, status="verified", verify="重跑同一句,这次落了文件")
    assert checked.status == "verified" and "落了文件" in checked.verify
    proclog.write(path, entries)
    assert proclog.read(path)[0].status == "verified"          # it is the file that says so


def test_an_unknown_id_and_an_illegal_state_are_both_reported(tmp_path):
    entries = proclog.read(proclog.unit(tmp_path))
    proclog.report(entries, title="甲", symptom="x")

    assert proclog.update(entries, "P-19700101-9", status="fixed") is None
    with pytest.raises(ValueError) as e:
        proclog.update(entries, entries[0].id, status="done")
    assert "open" in str(e.value) and "verified" in str(e.value)


def test_one_ledger_even_when_the_interface_language_changes(tmp_path):
    """Both spellings are read, so switching language does not hide half the history."""
    chinese = tmp_path / proclog.LOG_NAMES_ZH[0]
    first: list = []
    proclog.report(first, title="写中文名的那一次", symptom="x")
    proclog.write(chinese, first)
    assert proclog.unit(tmp_path) == chinese                  # the existing file wins
    assert len(proclog.read(proclog.unit(tmp_path))) == 1


def test_values_that_would_break_the_parse_are_flattened(tmp_path):
    path = proclog.unit(tmp_path)
    entries = proclog.read(path)
    proclog.report(entries, title="标题里有\n换行", symptom="现象\n分行写了两行",
                   evidence="- 看起来像字段的一行: x")
    proclog.write(path, entries)

    back = proclog.read(path)
    assert len(back) == 1
    assert "\n" not in back[0].symptom and "分行写了两行" in back[0].symptom
    assert "看起来像字段的一行" in back[0].evidence


# ------------------------------------------------------------------ the measured half
def test_scan_says_what_was_measured_including_the_absences():
    from app import proclog as p

    text = p.render_scan({
        "window": 12, "messages": {"user": 1, "agent": 8, "system": 2, "plan": 1},
        "speakers": [("Aide", 5)], "silent": ["Storyboard"],
        "tool_calls": {"ok": 0, "failed": 0, "denied": 0, "other": 0, "failed_by": [], "messages": []},
        "tasks": [], "files": [], "file_count": 0, "missing": [],
        "system_notes": [("达到单次最大发言轮数(8)", 2)], "log": {"open": 1},
    })
    assert "12" in text
    # The absence is stated, not left to be inferred from a zero: a round where nothing was produced
    # by a tool is the single most useful thing this digest can say.
    assert "none recorded" in text or "一次都没有" in text
    assert "Storyboard" in text                              # who never spoke
    assert "never split up" in text or "没有任务板" in text
    assert "×2" in text                                      # the repeated system note
    assert "open" in text


def test_scan_names_a_promised_file_that_is_not_there():
    from app import proclog as p

    text = p.render_scan({
        "window": 4, "messages": {"user": 1, "agent": 2, "system": 1},
        "speakers": [("Aide", 2)], "silent": [],
        "tool_calls": {"ok": 1, "failed": 1, "denied": 0, "other": 0,
                       "failed_by": [("write_document", 1)],
                       "messages": ["write_document(failed): 缺少必填参数:body"]},
        "tasks": [{"id": "T1", "title": "写报告", "owner": "Scribe", "status": "done",
                   "deliverable": "交付/报告.docx"}],
        "files": [], "file_count": 0, "missing": ["报告.docx"],
        "system_notes": [], "log": {},
    })
    # The name of the file, not its path: `planner.named_files` is the one reader of a `deliverable`,
    # so the ledger and the executor agree by construction on what "it promised a file" means. They
    # did not before: this module had a 12-extension list and the executor had a 27-extension one,
    # and the 15 extensions only the executor knew about were audio and video — the work this group
    # does — so a missing `旁白.wav` never reached the ledger's count.
    assert "报告.docx" in text
    assert "write_document(failed)" in text
    assert "T1" in text


# ------------------------------------------------------------------ through the tool members call
def test_the_tool_scans_reports_and_lists_inside_the_group_workspace(store, make_router):
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    hub = orch.toolhub
    ctx = asyncio.run(hub.context(group, agent, connect=False))
    assert "process_log" in ctx.tools, list(ctx.tools)
    assert ctx.tools["process_log"]["risk"] == "write"
    assert not any("process_log" in p for p in ctx.problems), ctx.problems

    # Something for the scan to have measured: a tool call that failed, and a task board that
    # promised a file. Written through the store, the same way the orchestrator writes them.
    store.add_message(g["id"], "agent", agent["id"], agent["name"], "我试着写文件",
                      meta={"tools": [{"name": "write_document", "status": "failed",
                                       "preview": "缺少必填参数:body"}]})
    store.add_message(g["id"], "plan", None, "任务板", "任务板", meta={"kind": "plan", "tasks": [
        {"id": "T1", "title": "写报告", "owner": "Scribe", "status": "done",
         "deliverable": "交付/报告.docx", "error": "", "dir": ""}]})

    scanned = asyncio.run(hub.call(ctx, "process_log", {"action": "scan"}))
    assert scanned.ok, scanned.text
    assert "write_document(failed)" in scanned.text, scanned.text
    # The basename of the promised file, which is what the scan can match against the workspace.
    assert "报告.docx" in scanned.text, scanned.text
    assert not scanned.files                                   # a scan produces nothing

    reported = asyncio.run(hub.call(ctx, "process_log", {
        "action": "report", "title": "任务判成功但文件不存在", "symptom": "任务板写 done,工作目录里没有报告",
        "evidence": scanned.text.splitlines()[0], "severity": "blocker", "stage": "delivery"}))
    assert reported.ok, reported.text
    workspace = store.workspace_dir(g["id"])
    ledger = proclog.unit(workspace)
    assert ledger.is_file()
    assert [f["name"] for f in reported.files] == [str(ledger.relative_to(workspace))]
    assert "P-" in reported.text

    listed = asyncio.run(hub.call(ctx, "process_log", {"action": "list"}))
    assert listed.ok and "任务判成功但文件不存在" in listed.text
    assert "blocker" in listed.text or "阻断" in listed.text

    moved = asyncio.run(hub.call(ctx, "process_log", {
        "action": "update", "id": proclog.read(ledger)[0].id, "status": "fixed",
        "note": "交付判定改成看文件"}))
    assert moved.ok and proclog.read(ledger)[0].status == "fixed"

    # …and the ledger stays a file in the group's workspace. It used to be handed to the group's own
    # knowledge base as well; nothing enters a library by itself any more, so what is asserted here is
    # that the record is where it was written and readable.
    assert store.list_docs() == []
    assert "任务判成功但文件不存在" in ledger.read_text(encoding="utf-8")


def test_the_tool_refuses_a_defect_it_cannot_evidence(store, make_router):
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False))

    no_title = asyncio.run(orch.toolhub.call(ctx, "process_log", {"action": "report", "symptom": "x"}))
    assert not no_title.ok and "title" in no_title.text
    unknown = asyncio.run(orch.toolhub.call(ctx, "process_log", {"action": "update", "id": "P-19700101-1"}))
    assert not unknown.ok and "P-19700101-1" in unknown.text
    bad_action = asyncio.run(orch.toolhub.call(ctx, "process_log", {"action": "read"}))
    assert not bad_action.ok and "scan" in bad_action.text
