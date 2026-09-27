"""The automatic half: every round measures itself, and what is wrong lands in the ledger.

This is what "audit every task and record it automatically" has to mean in practice, so it is
asserted on the *record* rather than on a log line:

  * a task that promises a file and delivers none leaves an entry, with the measurement as its
    evidence and `by: auto` as its author — nobody claimed to have reviewed it;
  * running the same failing round again **counts** the defect (`seen`) instead of adding a second
    row, because a log that grows one duplicate per round is a log nobody reads;
  * the group chat itself gains no message from the watcher: recording is silent;
  * a round that goes well writes nothing at all.

And the second half — the cause and the fix — is filled in by a model *outside* the group, silently:
it runs only when new entries appeared, and it may only write into fields that are still empty.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from app import proclog, scoring, templates
from tests.conftest import FakeLLM, has, TASK_HEAD, PLAN_MODE
from tests.test_collab import Collector, setup


# A plan whose single task promises a file. The member then answers with prose and never calls a
# tool, which is exactly the shape that used to pass as a finished task.
PROMISING = json.dumps({
    "goal": "写一份报告",
    "tasks": [{"id": "t1", "owner": "Copywriter", "title": "写报告",
               "instruction": "写出来", "deliverable": "交付/报告.docx"}],
}, ensure_ascii=False)


def script_with_no_file(messages):
    name, last = None, ""
    for m in messages:
        if m["role"] == "user":
            last = m["content"]
        if m["role"] == "system" and "(host)" in m["content"]:
            name = "host"
    if name == "host" and has(last, PLAN_MODE):
        return "写吧。\n<plan>" + PROMISING + "</plan>"
    if has(last, TASK_HEAD):
        return "我写好了报告,内容如下……"          # prose, no tool call, no file
    return "好"


def ledgers(store):
    return [proclog.read(proclog.unit(store.workspace_dir(g["id"]))) for g in store.list_groups()][0]


async def test_a_task_that_promised_a_file_leaves_one_entry(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default=script_with_no_file))
    c = Collector()
    await orch.handle_user_message(g["id"], "写一份报告", c)
    await orch.drain()

    entries = ledgers(store)
    missing = [e for e in entries if e.key.startswith("missing-file:")]
    assert missing, [e.title for e in entries]
    e = missing[0]
    assert "报告.docx" in e.key
    assert e.stage == "delivery" and e.severity in ("blocker", "major")
    assert e.by == "auto"                       # the program wrote it, and it says so
    assert e.status == "open" and e.seen == 1
    assert "报告.docx" in e.evidence            # the measurement, not a memory of the chat
    assert e.found and e.history

    # The file the ledger lives in is in the group's workspace, where the user can open it.
    assert proclog.unit(store.workspace_dir(g["id"])).is_file()


async def test_the_watcher_never_says_anything(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default=script_with_no_file))
    name = templates.process_engineer(store)["name"]
    c = Collector()
    await orch.handle_user_message(g["id"], "写一份报告", c)
    await orch.drain()

    senders = {(m.get("sender_name"), m.get("sender_type")) for m in store.list_messages(g["id"])}
    assert all(s != name for s, _ in senders)
    # …and nothing in the transcript announces the audit either: the record is the file.
    assert not [m for m in store.list_messages(g["id"])
                if m["sender_type"] == "system" and "process" in (m.get("content") or "").lower()]


async def test_the_same_defect_again_is_counted_not_repeated(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default=script_with_no_file))
    c = Collector()
    await orch.handle_user_message(g["id"], "写一份报告", c)
    await orch.drain()
    first = len(ledgers(store))

    await orch.handle_user_message(g["id"], "再写一份报告", c)
    await orch.drain()
    entries = ledgers(store)
    missing = [e for e in entries if e.key.startswith("missing-file:")]
    assert len(missing) == 1, [e.key for e in entries]      # still one row for one defect…
    assert missing[0].seen >= 2                             # …with the repetition counted
    assert len(entries) >= first                            # and nothing lost
    assert len(missing[0].history) >= 2                     # the second sighting is in the history


async def test_a_round_that_went_well_writes_nothing(store, make_router):
    good = json.dumps({"goal": "打个招呼",
                       "tasks": [{"id": "t1", "owner": "Copywriter", "title": "打招呼",
                                  "instruction": "打招呼", "deliverable": "一句话"}]}, ensure_ascii=False)

    def script(messages):
        last = "".join(m["content"] for m in messages if m["role"] == "user")
        if has(last, PLAN_MODE):
            return "<plan>" + good + "</plan>"
        if has(last, TASK_HEAD):
            return "【分工】我负责打招呼;发挥写作;用无;承接无\n各位同事好"
        return "各位同事好"

    orch, g = setup(store, make_router, FakeLLM(default=script))
    c = Collector()
    await orch.handle_user_message(g["id"], "打个招呼", c)
    await orch.drain()
    assert [e for e in ledgers(store) if e.by == "auto"] == []


async def test_the_switch_off_records_nothing(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default=script_with_no_file), process_autolog=False)
    c = Collector()
    await orch.handle_user_message(g["id"], "写一份报告", c)
    await orch.drain()
    assert ledgers(store) == []


# ------------------------------------------------------------------ the second half: cause and fix
async def test_an_outside_model_fills_the_cause_and_the_fix(store, make_router, monkeypatch):
    def script(messages):
        blob = "\n".join(m["content"] for m in messages)
        if "流程日志" in blob or "process log" in blob:      # the review pass's own instruction
            # The id comes from the prompt, the way a model would quote it back. (Not "the first
            # bracketed thing": the instruction itself carries a JSON example with brackets in it.)
            found = re.search(r"\[(P-\d{8}-\d+)\]", blob)
            eid = found.group(1) if found else "P-1"
            return "```json\n" + json.dumps(
                [{"id": eid, "cause": "交付判定只看有没有人说话", "fix": "done 之前要求交付文件存在"}],
                ensure_ascii=False) + "\n```"
        return script_with_no_file(messages)

    monkeypatch.setattr(scoring, "pick_judge", lambda *a, **k: "deepseek/deepseek-flash")
    orch, g = setup(store, make_router, FakeLLM(script={"deepseek": script}, default=script))
    c = Collector()
    await orch.handle_user_message(g["id"], "写一份报告", c)
    await orch.drain()

    e = [x for x in ledgers(store) if x.by.startswith("process engineer")]
    assert e, [(x.title, x.cause, x.fix, x.by) for x in ledgers(store)]
    assert "交付判定" in e[0].cause and "done 之前要求" in e[0].fix
    assert e[0].hint                      # the rule's own suggestion is still there, as a hint
    assert "deepseek" in e[0].by or "process engineer" in e[0].by
    assert e[0].status == "open"                          # a model's suggestion is not a fix
    assert any(h.startswith("review@") for h in e[0].history)


async def test_a_review_never_overwrites_what_a_person_wrote(store, make_router):
    entries: list = []
    e = proclog.report(entries, title="交付物没落盘", symptom="x", cause="我自己写的根因", fix="", by="auto")
    assert proclog.apply_review(entries, json.dumps(
        [{"id": e.id, "cause": "模型猜的根因", "fix": "模型的修法"}], ensure_ascii=False),
        by="process engineer · m") == 1
    assert e.cause == "我自己写的根因"                      # untouched
    assert e.fix == "模型的修法"                            # the empty one got filled
    # A second pass cannot rewrite the first one's words either.
    assert proclog.apply_review(entries, json.dumps(
        [{"id": e.id, "cause": "又一个根因", "fix": "又一个修法"}], ensure_ascii=False), by="x") == 0


def test_a_review_that_does_not_parse_changes_nothing():
    entries: list = []
    e = proclog.report(entries, title="t", symptom="s", by="auto")
    for bad in ("我看了一下,没有明显问题。", "", "[{", "{\"id\": \"" + e.id + "\"}"):
        assert proclog.apply_review(entries, bad, by="m") == 0
    assert e.cause == "" and e.fix == "" and e.by == "auto"
