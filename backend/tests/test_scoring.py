"""Scoring a round of collaboration.

The rules these lock down, in order of how much they matter:

1. **Mechanical facts are not opinions.** A task that produced nothing is not rescued by an
   optimistic judge, and everything mechanical keeps working with no model at all.
2. **Two questions, not one.** "Delivered" and "usable downstream" come apart, and the gap between
   them is the whole point of grading a round — see `verdict_of`.
3. **Fail closed.** A judge that is missing, unreachable or answers nonsense produces mechanical
   signals and an error note, never a guessed score and never a lesson.
4. **Grading never touches the work.** The round's output is unchanged, and a broken judge can only
   add a note.

The judge here is the project's `FakeLLM`, dispatched by model prefix — the same instance that
plays the members answers the grading prompt, which is also how the integration test at the bottom
proves the two calls really are different requests.
"""

from __future__ import annotations

import json

import pytest

from app import planner, scoring
from app.planner import Plan, PlanTask
from tests.conftest import FakeLLM
from tests.test_collab import PLAN, Collector, role, last_user, has, setup
from tests.conftest import INTEGRATE, PLAN_MODE, TASK_HEAD

JUDGE = "ollama/qwen2.5:7b"          # local, and not one of the fixture group's members
ENOUGH = "初稿正文:" + "面向客户的发布通知,含时间、地点与报名方式。" * 2


def make_plan(status: str = "done", owner: str = "Copywriter") -> Plan:
    return Plan(goal="写一份发布通知", conventions="", tasks=[
        PlanTask(id="t1", owner=owner, owner_id="a1", title="初稿", instruction="写通知初稿",
                 deliverable="Markdown", status=status)])


def reply(**per_task) -> str:
    return json.dumps(per_task, ensure_ascii=False)


def judge_aware(judge_reply, member_reply=ENOUGH) -> FakeLLM:
    """One fake playing both roles, dispatched on the prompt rather than the model name.

    Keying on the model string looks simpler but is brittle: `litellm_params` decides how a model is
    spelled before it is called, so a prefix like "ollama/" may never match and the judge silently
    gets the members' answer instead. The grading prompt is unmistakable, and `JUDGE_HEAD` is
    deliberately not translated, which makes it a stable key.
    """
    def script(messages):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "You are grading one round" in text:
            if isinstance(judge_reply, BaseException):
                raise judge_reply
            return judge_reply
        return member_reply(messages) if callable(member_reply) else member_reply

    return FakeLLM(default=script)


def wire(store, make_router, judge_reply: str, **cfg):
    """An orchestrator whose members answer normally and whose judge answers `judge_reply`."""
    fake = judge_aware(judge_reply)
    orch, group = setup(store, make_router, fake)
    store.update_settings({"scoring_enabled": True, "score_judge_model": JUDGE,
                           "score_threshold": 50, **cfg})
    return orch, group, fake


# ============================================================ mechanical signals
def test_mechanical_signals_need_no_model_at_all():
    done = PlanTask(id="t1", owner="x", owner_id="a1", title="t", instruction="i", status="done")
    failed = PlanTask(id="t2", owner="x", owner_id="a1", title="t", instruction="i",
                      status="failed", error="模型调用失败")
    assert scoring.mechanical(done, ENOUGH, {"fallback": True, "tools": ["library_search"]}) == {
        "status": "done", "chars": len(ENOUGH), "empty": False, "error": False,
        "fallback": True, "tools": ["library_search"], "delivered": True}
    assert scoring.mechanical(done, "   ", None)["delivered"] is False      # nothing came back
    assert scoring.mechanical(failed, ENOUGH, None)["delivered"] is False   # it said it failed


def test_a_short_but_real_answer_still_counts_as_delivered():
    """Regression on the emptiness gate: it exists to catch a task that produced nothing, not to
    impose a style guide. A one-line deliverable is a legitimate deliverable, and marking it a
    failure would put a false note in front of the next round."""
    t = PlanTask(id="t1", owner="x", owner_id="a1", title="t", instruction="i", status="done")
    assert scoring.mechanical(t, "交付日期:2026-10-12(已与对方确认)", None)["delivered"] is True
    assert scoring.mechanical(t, "ok", None)["delivered"] is False


def test_abridging_keeps_the_head_and_the_tail():
    assert scoring.abridge("短短一句", 200) == "短短一句"
    body = "开头" + "x" * 500 + "结论在此"
    got = scoring.abridge(body, 60)
    assert got.startswith("开头") and got.endswith("结论在此") and "omitted" in got
    assert "446" in got                       # the dropped middle is counted, not silently lost


# ============================================================ the judge's reply
def test_a_judge_reply_is_read_strictly_but_tolerantly_about_shape():
    wanted = ["t1", "t2"]
    good = reply(t1={"delivered": 0.9, "usable": 0.4, "reason": "完成了但下一步用不了"},
                 t2={"delivered": 0.2, "usable": 0.1, "reason": "跑偏了"})
    assert scoring.parse_judge_reply(good, wanted)["t1"]["usable"] == 0.4
    # a code fence and surrounding prose are shape, not substance
    assert scoring.parse_judge_reply(f"```json\n{good}\n```", wanted)["t2"]["delivered"] == 0.2
    assert scoring.parse_judge_reply(f"Sure! {good}", wanted)["t1"]["delivered"] == 0.9
    # a number, or a decimal written as a string, both count
    got = scoring.parse_judge_reply('{"t1": {"delivered": 0.8, "usable": "0.5"}}', ["t1"])
    assert got["t1"]["delivered"] == 0.8 and got["t1"]["usable"] == 0.5
    # an explicit percentage does too
    assert scoring.parse_judge_reply('{"t1": {"delivered": "80%"}}', ["t1"])["t1"]["delivered"] == 0.8
    # but a bare out-of-range number is refused rather than rescaled, and a word is not a number:
    # reading 80 as 80% and 5 as 5% would be guessing, and the guess lands on the score
    for bad in ('{"t1": {"delivered": 80}}', '{"t1": {"delivered": 5}}',
                '{"t1": {"delivered": -1}}', '{"t1": {"delivered": true}}',
                '{"t1": {"delivered": "yes"}}'):
        assert scoring.parse_judge_reply(bad, ["t1"])["t1"]["delivered"] is None, bad


def test_a_reply_that_cannot_be_used_says_which_way_it_failed():
    """Two failures with two different fixes: an empty answer points at the prompt or the model, a
    wrongly-shaped one points at the answer format. Lumping them together wastes the diagnosis."""
    with pytest.raises(ValueError, match="did not return JSON"):
        scoring.parse_judge_reply("我觉得都挺好的", ["t1"])
    with pytest.raises(ValueError, match="none of the tasks"):
        scoring.parse_judge_reply('{"zz": {"delivered": 1}}', ["t1"])
    with pytest.raises(ValueError, match="not as objects"):
        scoring.parse_judge_reply('{"t1": 0.9}', ["t1"])


# ============================================================ the verdict
def test_the_verdict_separates_delivered_from_usable():
    m = {"delivered": True, "error": False, "status": "done"}
    assert scoring.verdict_of(m, {"delivered": 0.9, "usable": 0.9}, 0.5) == "ok"
    assert scoring.verdict_of(m, {"delivered": 0.9, "usable": 0.2}, 0.5) == "weak"
    assert scoring.verdict_of(m, {"delivered": 0.2, "usable": 0.9}, 0.5) == "rework"
    # the threshold is the user's, not a constant
    assert scoring.verdict_of(m, {"delivered": 0.9, "usable": 0.6}, 0.8) == "weak"
    assert scoring.verdict_of(m, None, 0.5) == "ok"          # nothing judged it, nothing to fault


def test_a_mechanical_failure_outranks_an_optimistic_judge():
    """A judge reading a truncated or empty transcript sometimes says "looks fine". The mechanical
    signals decide first, so that answer cannot turn a missing deliverable into a pass."""
    dead = {"delivered": False, "error": True, "status": "failed"}
    assert scoring.verdict_of(dead, {"delivered": 1.0, "usable": 1.0}, 0.5) == "failed"
    empty = {"delivered": False, "error": False, "status": "done"}
    assert scoring.verdict_of(empty, {"delivered": 1.0, "usable": 1.0}, 0.5) == "rework"


def test_the_judge_is_never_one_of_the_groups_own_members(store, make_router):
    """A model that just answered is the last thing that should decide whether it answered well."""
    orch, group, _ = wire(store, make_router, reply(t1={"delivered": 1, "usable": 1}))
    member_models = {store.get_agent(i).get("model_id") for i in group["member_ids"]}
    chosen = scoring.pick_judge(store, orch.router, group)
    assert chosen and chosen not in member_models
    assert chosen == JUDGE, "the local model should win: cheaper, and the round stays on this machine"


# ============================================================ the scorecard
async def test_nothing_happens_while_scoring_is_off(store, make_router):
    """Off means off: no judge request, no lesson, and the round is not penalised for it."""
    fake = judge_aware(reply(t1={"delivered": 1, "usable": 1}))
    orch, group = setup(store, make_router, fake)
    store.update_settings({"scoring_enabled": False})
    card = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])
    assert card["skipped"] == "disabled" and card["judge_error"] == ""
    assert card["summary"] == {"total": 1, "judged": 0, "ok": 1, "weak": 0, "rework": 0, "failed": 0}
    assert not [c for c in fake.calls if "grading" in json.dumps(c[1], ensure_ascii=False)]
    assert store.list_memories("group", group["id"], "lesson") == []


async def test_a_round_is_scored_and_the_weak_task_is_named(store, make_router):
    judge = reply(t1={"delivered": 0.9, "usable": 0.2, "reason": "缺结束日期,下游没法直接排版"})
    orch, group, fake = wire(store, make_router, judge)
    card = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])

    assert card["judge"] == JUDGE and card["judge_error"] == ""
    assert card["threshold"] == 50
    task = card["tasks"][0]
    assert (task["verdict"], task["delivered"], task["usable"]) == ("weak", 0.9, 0.2)
    assert "结束日期" in task["reason"] and task["judged"] is True
    assert task["mechanical"]["chars"] == len(ENOUGH)
    assert card["summary"] == {"total": 1, "judged": 1, "ok": 0, "weak": 1, "rework": 0, "failed": 0}
    # the judge saw the task and the deliverable, and was asked both questions
    prompt = json.dumps([m for _model, msgs in fake.calls for m in msgs], ensure_ascii=False)
    assert "写通知初稿" in prompt and "usable" in prompt and "delivered" in prompt
    line = scoring.summarize_card(card)
    assert JUDGE in line and "Copywriter" in line and "结束日期" in line


async def test_a_broken_judge_falls_back_and_writes_no_lessons(store, make_router):
    """The whole point of failing closed: a judge talking nonsense must not become a lesson that
    misleads every future round in this group."""
    orch, group, _ = wire(store, make_router, "我觉得这份稿子写得挺好的,给个高分吧")
    card = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])

    assert card["judge"] == "" and JUDGE in card["judge_error"]
    assert card["tasks"][0]["judged"] is False
    assert card["tasks"][0]["verdict"] == "ok"          # mechanically fine, and nobody said otherwise
    assert card["lessons"] == []
    assert store.list_memories("group", group["id"], "lesson") == []
    assert "could not" not in scoring.summarize_card(card)   # the note explains, it does not alarm


async def test_no_eligible_judge_falls_back_rather_than_guessing(store, make_router, monkeypatch):
    """When every usable model is a member of the group, there is nobody left to grade it — and the
    answer is to say so, not to let the group mark its own homework."""
    orch, group, _ = wire(store, make_router, reply(t1={"delivered": 1, "usable": 1}),
                          score_judge_model="")
    members_only = [{"id": store.get_agent(i).get("model_id"), "is_local": False}
                    for i in group["member_ids"]]
    members_only = [m for m in members_only if m["id"]]
    monkeypatch.setattr(orch.router, "usable_models", lambda: members_only)

    card = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])
    assert card["judge"] == "" and card["judge_error"]
    assert card["tasks"][0]["judged"] is False, "no score may be invented"
    assert card["lessons"] == []


async def test_lessons_come_from_the_judge_and_are_not_repeated(store, make_router):
    """The feedback half: the reason a task was weak becomes a note the next round can read — once.
    Repeating it every round would cost context and teach nothing."""
    judge = reply(t1={"delivered": 0.3, "usable": 0.1, "reason": "没有引用任何来源"})
    orch, group, _ = wire(store, make_router, judge)
    first = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])

    assert first["tasks"][0]["verdict"] == "rework"
    assert len(first["lessons"]) == 1
    lessons = store.list_memories("group", group["id"], "lesson")
    assert len(lessons) == 1
    assert "没有引用任何来源" in lessons[0]["content"] and "初稿" in lessons[0]["content"]
    assert lessons[0]["source"] == "auto"

    second = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])
    assert second["lessons"] == [], "the same note must not be written twice"
    assert len(store.list_memories("group", group["id"], "lesson")) == 1


async def test_lessons_can_be_switched_off_and_the_round_is_still_scored(store, make_router):
    judge = reply(t1={"delivered": 0.3, "usable": 0.1, "reason": "跑偏了"})
    orch, group, _ = wire(store, make_router, judge, score_max_lessons=0)
    card = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])
    assert card["tasks"][0]["verdict"] == "rework" and card["lessons"] == []
    assert store.list_memories("group", group["id"], "lesson") == []


async def test_scoring_never_raises_even_when_the_model_does(store, make_router):
    """It grades a round that already produced an answer; an exception here would throw that away."""
    fake = judge_aware(RuntimeError("judge exploded"))
    orch, group = setup(store, make_router, fake)
    store.update_settings({"scoring_enabled": True, "score_judge_model": JUDGE})
    card = await scoring.score_round(store, orch.router, group, make_plan(), {"t1": ENOUGH}, [])
    assert card["judge_error"] and card["tasks"][0]["verdict"] == "ok"
    assert card["summary"]["total"] == 1


# ============================================================ the wire
def test_the_board_carries_the_scorecard_only_once_there_is_one():
    plan = make_plan()
    assert "score" not in plan.to_meta(), "an unscored plan must not grow an empty field"
    plan.scorecard = {"judge": JUDGE, "tasks": [], "summary": {"total": 0}}
    assert plan.to_meta()["score"]["judge"] == JUDGE


def long_script():
    """`plan_script` with deliverables that clear the emptiness gate — the shared fixture's short
    strings would be judged 'nothing produced' before the judge ever saw them."""
    draft = "【分工】我负责初稿;发挥写作;用无;承接无\n初稿正文:" + "发布通知正文,含时间地点与报名方式。" * 2
    review = "【分工】我负责审校;发挥中文;用无;承接文案\n审校意见:" + "统一称呼、补齐日期格式,核对三处数字。" * 2

    def script(messages):
        name, u = role(messages), last_user(messages)
        if name == "Aide" and has(u, PLAN_MODE):
            return "思路:先写后审。\n<plan>" + PLAN + "</plan>"
        if name == "Aide" and has(u, INTEGRATE):
            return "最终通知:各位同事…(来自:文案)"
        if name == "Copywriter" and has(u, TASK_HEAD):
            return draft
        if name == "Proofreader" and has(u, TASK_HEAD):
            return review
        return "好的"

    return script


async def test_a_planned_round_lands_the_scorecard_on_the_task_board(store, make_router):
    """End to end through the orchestrator: the members' own answers are graded by a *different*
    model, the board gains the scores, the transcript gets one plain sentence about them, and the
    weak hand-off becomes a lesson for the next round."""
    judge = reply(t1={"delivered": 0.95, "usable": 0.9, "reason": "初稿可用"},
                  t2={"delivered": 0.9, "usable": 0.2, "reason": "没有指出改动点,作者无法据此修改"})
    fake = judge_aware(judge, member_reply=long_script())
    orch, g = setup(store, make_router, fake)
    store.update_settings({"scoring_enabled": True, "score_judge_model": JUDGE, "score_threshold": 50})

    await orch.handle_user_message(g["id"], "帮我出一份发布会通知", Collector())
    await orch.drain()

    board = [m for m in store.list_messages(g["id"]) if m["sender_type"] == "plan"][0]
    score = board["meta"]["score"]
    assert score["judge"] == JUDGE
    assert [t["task_id"] for t in score["tasks"]] == ["t1", "t2"]
    assert [t["verdict"] for t in score["tasks"]] == ["ok", "weak"]
    assert score["summary"] == {"total": 2, "judged": 2, "ok": 1, "weak": 1, "rework": 0, "failed": 0}

    notes = [m["content"] for m in store.list_messages(g["id"]) if m["sender_type"] == "system"]
    assert any(JUDGE in n and "weak" in n.lower() for n in notes), notes[-3:]
    lessons = store.list_memories("group", g["id"], "lesson")
    assert len(lessons) == 1 and "没有指出改动点" in lessons[0]["content"]

    # the members' outputs are exactly what they produced — grading annotates, it does not edit
    members = [m for m in store.list_messages(g["id"]) if m["sender_type"] == "agent"]
    assert any("复核三处数字" in m["content"] or "核对三处数字" in m["content"] for m in members)

    # the grading was its own request, to a model that is not one of the members
    judge_calls = [(model, msgs) for model, msgs in fake.calls
                   if "You are grading one round" in "\n".join(str(m.get("content") or "") for m in msgs)]
    assert len(judge_calls) == 1
    member_models = {store.get_agent(i).get("model_id") for i in g["member_ids"]}
    assert judge_calls[0][0] not in member_models
    assert "没有指出改动点" not in json.dumps(judge_calls[0][1], ensure_ascii=False), \
        "the judge is asked about the work, it is not shown its own previous answers"
