"""The delivery contract: a task is done when something was delivered.

Until this rule existed, `_execute_plan` marked a task `done` the moment its owner replied — so a
turn whose every tool call failed, or one that was supposed to write a file and wrote none, was handed
downstream and reported to the user as finished. These tests pin the three shapes:

1. every tool call of the turn failed → the task failed, with the calls named;
2. the deliverable names a file and the turn made no successful tool call at all → the file cannot
   exist, so the task failed (a member that used *some* tool successfully is trusted about the name:
   `报告-v2.docx` for a plan that said `报告.docx` must not be a failure);
3. a member that cannot take a turn at all (an external agent with no usable configuration) is taken
   out of the round *before* it is handed one, and the round says why once.
"""

from __future__ import annotations

import pathlib

from app import planner
from app.orchestrator import Orchestrator


def task(deliverable: str = "") -> planner.PlanTask:
    return planner.PlanTask(id="t1", owner="Copywriter", owner_id="a1", title="写稿",
                            instruction="写", deliverable=deliverable)


def msg(tools: list[dict]) -> dict:
    return {"meta": {"tools": tools}}


# ------------------------------------------------------------------ _task_shortfall
def test_every_tool_call_failing_is_not_a_delivery(tmp_path):
    orch = Orchestrator.__new__(Orchestrator)          # the method touches no instance state
    calls = [{"name": "generate_image", "status": "failed"}, {"name": "generate_image", "status": "failed"}]

    why = orch._task_shortfall(task(), msg(calls), tmp_path)

    assert "generate_image(failed)" in why
    assert "全都失败" in why or "every tool call failed" in why


def test_a_named_deliverable_with_no_successful_call_is_a_failure(tmp_path):
    orch = Orchestrator.__new__(Orchestrator)
    calls = [{"name": "write_document", "status": "denied"}]

    why = orch._task_shortfall(task("交付/报告.docx"), msg(calls), tmp_path)

    assert "报告.docx" in why
    assert "落盘" in why or "never written" in why


def test_a_reply_with_no_tools_is_still_a_normal_writing_task(tmp_path):
    """Most tasks are prose: a member that wrote a review, a decision or a script has no tool to call,
    and calling that a failure would break every honest text task."""
    orch = Orchestrator.__new__(Orchestrator)

    assert orch._task_shortfall(task("一段评审意见"), msg([]), tmp_path) == ""
    assert orch._task_shortfall(task(""), msg([]), tmp_path) == ""


def test_some_successful_call_means_the_name_is_not_second_guessed(tmp_path):
    """A member may deliver `报告-v2.docx` while the plan said `报告.docx`. Failing that would be worse
    than the thing this check is for."""
    orch = Orchestrator.__new__(Orchestrator)
    calls = [{"name": "write_document", "status": "ok", "files": [{"name": "交付/报告-v2.docx"}]}]

    assert orch._task_shortfall(task("交付/报告.docx"), msg(calls), tmp_path) == ""


def test_a_skipped_call_alone_is_not_a_delivery_either(tmp_path):
    orch = Orchestrator.__new__(Orchestrator)

    assert orch._task_shortfall(task("报告.docx"), msg([{"name": "write_document", "status": "denied"}]), tmp_path)


# ------------------------------------------------------------------ who may be given a turn
def member(**kw) -> dict:
    return {"id": "m1", "name": "WorkBuddy", "role": "External agent", **kw}


def test_an_external_member_with_the_switch_off_is_taken_out_of_the_round():
    orch = Orchestrator.__new__(Orchestrator)
    cfg = {"external_agents_enabled": False}

    blocked = orch._blocked_members([member(engine="workbuddy"), {"id": "m2", "name": "Aide"}], cfg)

    assert list(blocked) == ["m1"]
    assert "master switch" in blocked["m1"]


def test_a_member_whose_config_does_not_comply_is_taken_out_with_the_reason():
    orch = Orchestrator.__new__(Orchestrator)
    cfg = {"external_agents_enabled": True}
    # Full permissions without the acknowledgement: the shape a hand-edited database or a restored
    # backup can bring in. `clean_cfg` rejects it, so the member would fail identically every round.
    bad = member(engine="workbuddy", engine_cfg={"level": "full", "risk_ack": False})

    blocked = orch._blocked_members([bad], cfg)

    assert "not valid" in blocked["m1"]
    assert "不合规" in blocked["m1"] or "not valid" in blocked["m1"]


def test_a_plain_member_is_never_blocked_by_this_check():
    """Only *configuration* problems are caught here — a model that times out still gets its turn."""
    orch = Orchestrator.__new__(Orchestrator)

    assert orch._blocked_members([{"id": "m1", "name": "Aide"}, {"id": "m2", "name": "文案"}],
                                 {"external_agents_enabled": False}) == {}


# ------------------------------------------------------------------ the board and the host's answer
def test_the_task_board_carries_the_files_a_task_produced():
    t = task("交付/报告.docx")
    t.status = "done"
    t.files = ["交付/报告.docx"]

    board = t.to_dict()

    assert board["files"] == ["交付/报告.docx"]
    assert board["deliverable"] == "交付/报告.docx"


def test_the_host_is_told_to_list_the_files_and_not_to_pass_off_a_failed_task():
    plan = planner.Plan(goal="出一份报告", conventions="", tasks=[task("交付/报告.docx")])
    plan.tasks[0].status = "failed"
    plan.tasks[0].files = []

    text = planner.integration_prompt(plan, {})

    # The wording follows the request language, so the assertions are on what the instruction has to
    # contain rather than on one spelling of it.
    assert ("List the files" in text) or ("把这一轮真正产出的文件列出来" in text)
    assert ("not among them" in text) or ("那就是没交付" in text)
    assert "报告.docx" not in text or "没有完成" in text


def test_the_task_prompt_says_the_file_is_the_deliverable():
    plan = planner.Plan(goal="出一份报告", conventions="", tasks=[task("交付/报告.docx")])

    text = planner.task_prompt(plan, plan.tasks[0], {}, 1)

    assert "write_document" in text
    assert "交付/报告.docx" in text
