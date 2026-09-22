"""Scoring one round of collaboration, so a group can notice its own bad teamwork.

What this is for: the hard part of a multi-model group is not any single answer, it is whether
the members actually handed work to each other well — did the task that was assigned get done,
and could the next task use what came back. That is invisible today: a round either produces
text or it does not.

The shape is borrowed from `fast-jev-compaction` (MIT, https://github.com/tamaratran/
fast-jev-compaction). No code was copied — it is a TypeScript context compactor for Claude
Code — but four of its decisions are worth keeping:

* **Ask about discrete items, not the whole thing.** A holistic "rate this round out of ten" is
  not checkable and drifts between runs. One question per plan task gives a verdict that can be
  argued with.
* **Two questions per item.** JEV asks whether a call still matters *and* whether its result is
  still needed. Here: did the task deliver, and is the result usable as input downstream. They
  come apart all the time — a plausible-looking draft the next task cannot build on.
* **Numbers compared to a threshold, with the text untouched.** The scorecard annotates; it never
  rewrites a member's output. Nothing a member produced is deleted or shortened because of a score.
* **Fail closed, and say so.** No judge model, an unreachable one, or an answer that does not
  parse means the round falls back to the mechanical signals and records the failure. It never
  guesses a score, and it never blocks the round.

One honest difference: JEV asks a model trained for scoring and gets a calibrated probability. We
are asking a general-purpose model, which is weaker evidence, so two rules follow: mechanical
signals (free, reproducible) are kept separate from judged ones and labelled as such, and a judged
score is only ever allowed to trigger a *note to the next round* — never a retry, never a deletion.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import i18n, planner

# A task whose deliverable is shorter than this counts as empty regardless of what the judge
# thinks: a "done" task with two characters in it is a mechanical fact, not an opinion.
#
# Kept deliberately small. This is not a quality bar — a legitimate deliverable can be one line
# ("the test passes", a date, a single figure). Setting it high would mark honest short answers as
# failures, and a false verdict is worse than a missed one here: it is what puts a note in front of
# the next round.
MIN_CHARS = 20


def abridge(text: str, limit: int) -> str:
    """Head + tail, with the middle dropped and counted — what JEV does to long texts before they
    enter the state, and for the same reason: the start says what the thing is, the end usually
    holds the conclusion, and the middle is what costs tokens."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    half = max(1, limit // 2)
    dropped = len(body) - 2 * half
    return f"{body[:half]}\n[… {dropped} {i18n.pick_now('characters omitted', '字符已省略')} …]\n{body[-half:]}"


# ------------------------------------------------------------------ mechanical signals
def mechanical(task: planner.PlanTask, text: str, step: dict | None) -> dict:
    """What can be established without asking anybody: free, reproducible, and the only thing that
    can be trusted when no judge model is available."""
    chars = len((text or "").strip())
    return {
        "status": task.status,
        "chars": chars,
        "empty": chars < MIN_CHARS,
        "error": bool(task.error),
        "fallback": bool((step or {}).get("fallback")),
        "tools": list((step or {}).get("tools") or []),
        "delivered": task.status == "done" and chars >= MIN_CHARS and not task.error,
    }


# ------------------------------------------------------------------ the judge
JUDGE_HEAD = (
    "You are grading one round of a multi-model team that split a job between members. For each "
    "task, answer two questions with a number from 0 to 1:\n"
    "  delivered = did the member actually produce what the instruction asked for?\n"
    "  usable    = could the next task in the list use this result as it stands, without redoing it?\n"
    "Judge only what is in front of you. An empty or truncated result is not delivered. A result "
    "that answers a different question than the one asked is not delivered. Do not reward length. "
    "If you cannot tell, answer 0.5 rather than guessing high.\n\n"
    "Answer with JSON only, no prose, no code fence, in exactly this shape:\n"
    '{"<task id>": {"delivered": 0.0, "usable": 0.0, "reason": "<one short sentence>"}, ...}\n'
    "Include every task id you were given. Write the reason in the same language as the task."
)


def build_judge_messages(plan: planner.Plan, excerpts: dict[str, str]) -> list[dict]:
    """One request for the whole round, not one per task.

    Batching is what makes this affordable: twelve tasks would otherwise be twelve model calls for
    information that fits on one screen.
    """
    lines = [JUDGE_HEAD, ""]
    if plan.goal:
        lines.append(i18n.pick_now(f"[Goal] {plan.goal}", f"【目标】{plan.goal}"))
    if plan.conventions:
        lines.append(i18n.pick_now(f"[Conventions] {plan.conventions}", f"【约定】{plan.conventions}"))
    for t in plan.tasks:
        lines.append("")
        lines.append(i18n.pick_now(f"[Task {t.id}] owner={t.owner}", f"【任务 {t.id}】负责人={t.owner}"))
        lines.append(i18n.pick_now(f"  instruction: {t.instruction}", f"  要求:{t.instruction}"))
        if t.deliverable:
            lines.append(i18n.pick_now(f"  expected deliverable: {t.deliverable}",
                                       f"  期望交付物:{t.deliverable}"))
        if t.error:
            lines.append(i18n.pick_now(f"  the round reported: {t.error}", f"  运行时报错:{t.error}"))
        body = excerpts.get(t.id) or ""
        lines.append(i18n.pick_now("  what the member produced:", "  成员产出的内容:"))
        lines.append(body if body else i18n.pick_now("  (nothing)", "  (空)"))
    return [{"role": "user", "content": "\n".join(lines)}]


def parse_judge_reply(text: str, task_ids: list[str]) -> dict[str, dict]:
    """Strict. A reply that does not parse as JSON, or that names no task we asked about, is a
    failure — not something to be salvaged with a loose regex. Salvaging is how a judge that
    answered nonsense ends up writing a confident lesson into memory.
    """
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(i18n.pick_now("the judge did not return JSON", "评判模型没有返回 JSON"))
        raw = raw[start:end + 1]
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ValueError(i18n.pick_now(f"the judge's JSON is malformed: {e}",
                                       f"评判模型返回的 JSON 不合法:{e}")) from None
    if not isinstance(obj, dict):
        raise ValueError(i18n.pick_now("the judge did not return an object", "评判模型返回的不是对象"))
    out: dict[str, dict] = {}
    malformed = 0
    for tid in task_ids:
        item = obj.get(tid)
        if not isinstance(item, dict):
            malformed += 1 if tid in obj else 0
            continue
        out[tid] = {"delivered": _prob(item.get("delivered")),
                    "usable": _prob(item.get("usable")),
                    "reason": re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:300]}
    if not out:
        # Two different failures, told apart on purpose: "answered nothing" needs a different fix
        # (the prompt or the model) than "answered in the wrong shape".
        raise ValueError(
            i18n.pick_now(f"the judge answered {malformed} task(s) but not as objects with "
                          f"delivered/usable", f"评判模型回答了 {malformed} 个任务,但不是带 "
                          f"delivered/usable 的对象")
            if malformed else
            i18n.pick_now("the judge named none of the tasks it was given",
                          "评判模型没有回答任何一个被问到的任务"))
    return out


def _prob(value: Any) -> float | None:
    """Read one number, or refuse it.

    Only two shapes count: a number in 0..1, or a percentage written with a `%` sign. Anything else
    — a word, a boolean, and especially a bare `80` that was meant as a percentage — comes back None,
    which is recorded as "not judged".

    Refusing is deliberate. The obvious alternative, dividing by 100 whenever the number is over 1,
    silently rescales a judge that meant something else: `5` out of 5 would become 0.05, and a score
    of `2` out of 2 would become a failing grade. A number this code cannot read is information the
    scorecard should show as missing, not information it should invent.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if 0.0 <= n <= 1.0 else None
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(%?)\s*", value)
        if not m:
            return None
        n = float(m.group(1))
        if m.group(2):                      # an explicit "80%" is unambiguous
            return max(0.0, min(1.0, n / 100.0))
        return n if 0.0 <= n <= 1.0 else None
    return None


# ------------------------------------------------------------------ judge selection
def pick_judge(store: Any, router: Any, group: dict) -> str:
    """Which model grades the round.

    Deliberately never one of the group's own members: a model that just answered is the worst
    possible judge of whether it answered well. Local first — grading is a careful reading task, not
    one that needs the strongest model, and keeping it on-device means the contents of a round do not
    leave the machine merely to be scored.
    """
    members = set(group.get("member_ids") or [])
    try:
        models = router.usable_models()
    except Exception:  # noqa: BLE001
        return ""
    eligible = [m for m in models if m.get("id") and m["id"] not in members]
    if not eligible:
        return ""
    local = [m for m in eligible if m.get("is_local")]
    return str((local or eligible)[0]["id"])


# ------------------------------------------------------------------ the scorecard
@dataclass
class TaskScore:
    task_id: str
    owner: str
    title: str
    verdict: str                  # ok | weak | rework | failed
    delivered: float | None = None
    usable: float | None = None
    reason: str = ""
    judged: bool = False
    mechanical: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "owner": self.owner, "title": self.title,
                "verdict": self.verdict, "delivered": self.delivered, "usable": self.usable,
                "reason": self.reason, "judged": self.judged, "mechanical": self.mechanical}


def verdict_of(m: dict, judged: dict | None, threshold: float) -> str:
    """Where the two questions land.

    `weak` is the interesting case, and the reason for asking two questions at all: the work was
    done, but whoever comes next cannot build on it. Mechanical signals decide first — a task that
    produced nothing is not rescued by an optimistic judge.
    """
    if not m["delivered"]:
        return "failed" if (m["error"] or m["status"] == "failed") else "rework"
    if not judged:
        return "ok"
    d, u = judged.get("delivered"), judged.get("usable")
    if d is None or u is None:
        return "ok"
    if d < threshold:
        return "rework"
    if u < threshold:
        return "weak"
    return "ok"


async def score_round(
    store: Any, router: Any, group: dict, plan: planner.Plan, outputs: dict[str, str],
    steps: list[dict],
) -> dict:
    """Score every task in the round and write the lessons worth keeping.

    Returns a JSON-serialisable scorecard. Never raises: a round that produced a good answer must
    not be turned into a failure by the thing that grades it.
    """
    cfg = store.get_settings()
    threshold = max(0.0, min(1.0, float(cfg["score_threshold"]) / 100.0))
    excerpt = max(100, int(cfg["score_excerpt_chars"]))
    by_owner = {s.get("agent"): s for s in steps}

    scores: list[TaskScore] = []
    for t in plan.tasks:
        m = mechanical(t, outputs.get(t.id) or "", by_owner.get(t.owner))
        scores.append(TaskScore(task_id=t.id, owner=t.owner, title=t.title,
                                verdict=verdict_of(m, None, threshold), mechanical=m))

    card: dict = {"at": time.time(), "threshold": int(cfg["score_threshold"]), "judge": "",
                  "judge_error": "", "tasks": scores, "lessons": []}

    if not cfg["scoring_enabled"]:
        card["skipped"] = "disabled"
        return _finish(card)
    if not scores:
        card["skipped"] = "no tasks"
        return _finish(card)

    judge = str(cfg["score_judge_model"] or "").strip() or pick_judge(store, router, group)
    if not judge:
        card["judge_error"] = i18n.pick_now(
            "no model is available to grade this round (every usable model is a member of it)",
            "没有可用于评分的模型(可用模型都在这个群里)")
        return _finish(card)

    excerpts = {t.id: abridge(outputs.get(t.id) or "", excerpt) for t in plan.tasks}
    try:
        res = await router.complete(build_judge_messages(plan, excerpts), only=judge, source="score")
        verdicts = parse_judge_reply(res.text, [t.id for t in plan.tasks])
        card["judge"] = judge
    except Exception as e:  # noqa: BLE001 — grading must never break the round it grades
        card["judge_error"] = f"{judge}: {e}"
        return _finish(card)

    for s in scores:
        judged = verdicts.get(s.task_id)
        s.judged = judged is not None
        if judged:
            s.delivered, s.usable, s.reason = judged["delivered"], judged["usable"], judged["reason"]
        s.verdict = verdict_of(s.mechanical, judged, threshold)

    card["lessons"] = _write_lessons(store, group, scores)
    return _finish(card)


def _finish(card: dict) -> dict:
    card["tasks"] = [t.to_dict() for t in card["tasks"]]
    card["summary"] = {
        "total": len(card["tasks"]),
        "judged": sum(1 for t in card["tasks"] if t["judged"]),
        **{v: sum(1 for t in card["tasks"] if t["verdict"] == v)
           for v in ("ok", "weak", "rework", "failed")},
    }
    return card


# ------------------------------------------------------------------ lessons
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub("", (text or "").lower())


def _write_lessons(store: Any, group: dict, scores: list[TaskScore]) -> list[dict]:
    """Turn the weakest verdicts into at most a handful of lessons.

    A lesson earns its place only if a future round can act on it, so it is built from the judge's
    own reason plus the task it came from — never invented here, and never written when the judge
    did not run. Duplicates are dropped: a memory block that repeats the same note every round costs
    context and teaches nothing.
    """
    budget = int(store.get_settings()["score_max_lessons"])
    if budget <= 0:
        return []
    # `weak` belongs here as much as `rework` does — "delivered, but whoever comes next cannot use
    # it" is exactly the hand-off problem worth remembering. Leaving it out would have kept the one
    # verdict this whole two-question design exists to produce from ever reaching memory.
    weak = [s for s in scores if s.verdict in ("rework", "failed", "weak") and s.reason]
    weak.sort(key=lambda s: s.delivered if s.delivered is not None else 0.0)
    try:
        existing = {_norm(m["content"]) for m in store.list_memories(kind="lesson", limit=200)}
    except Exception:  # noqa: BLE001
        existing = set()

    written: list[dict] = []
    for s in weak[:budget]:
        if s.verdict == "failed":
            body = i18n.pick_now(
                f"In this group the task \"{s.title}\" ({s.owner}) did not produce a usable result: {s.reason}",
                f"在本群,「{s.title}」({s.owner})没有产出可用结果:{s.reason}")
        else:
            body = i18n.pick_now(
                f"In this group the task \"{s.title}\" ({s.owner}) came back unusable for the next step: {s.reason}",
                f"在本群,「{s.title}」({s.owner})的结果下一步用不了:{s.reason}")
        body += i18n.pick_now(" Next time, write the deliverable into the instruction.",
                              "下次分工时把交付物写进要求里。")
        if _norm(body) in existing:
            continue
        try:
            item = store.add_memory(body, scope="group", scope_id=group["id"], kind="lesson",
                                    source="auto")
        except Exception:  # noqa: BLE001
            continue
        existing.add(_norm(body))
        written.append({"scope": "group", "scope_id": group["id"], "content": body,
                        "memory_id": item.get("id", "")})
    return written


def summarize_card(card: dict) -> str:
    """One line for the chat transcript.

    Deliberately plain, and it says how many tasks the judge actually saw — a scorecard built from
    six of twelve answers should not read like a verdict on all twelve. The numbers stay on the task
    board rather than being restated here, so there is one place to read them from.
    """
    s = card.get("summary") or {}
    if not s.get("total"):
        return ""
    if not card.get("judge"):
        line = i18n.pick_now(
            f"Mechanical check on this round: {s['total']} task(s), {s.get('failed', 0)} failed, "
            f"{s.get('rework', 0)} with no usable result.",
            f"本轮机械核对:{s['total']} 个任务,{s.get('failed', 0)} 个失败,"
            f"{s.get('rework', 0)} 个没有可用产出。")
        if card.get("judge_error"):
            line += i18n.pick_now(f" Nothing graded them: {card['judge_error']}",
                                  f" 没有模型参与评分:{card['judge_error']}")
        return line
    line = i18n.pick_now(
        f"Scored by {card['judge']} ({s.get('judged', 0)} of {s['total']} tasks judged): "
        f"{s.get('ok', 0)} good, {s.get('weak', 0)} delivered but unusable downstream, "
        f"{s.get('rework', 0)} to redo, {s.get('failed', 0)} failed.",
        f"由 {card['judge']} 评分(评分覆盖 {s.get('judged', 0)}/{s['total']} 个任务):"
        f"{s.get('ok', 0)} 个合格,{s.get('weak', 0)} 个交付了但下一步用不了,"
        f"{s.get('rework', 0)} 个需返工,{s.get('failed', 0)} 个失败。")
    weak = [t for t in card.get("tasks", []) if t["verdict"] in ("weak", "rework", "failed")]
    if weak:
        line += i18n.pick_now(" Weakest: ", " 最弱:") + "; ".join(
            f"{t['task_id']} {t['owner']}" + (f" — {t['reason']}" if t["reason"] else "")
            for t in weak[:3])
    if card.get("lessons"):
        line += i18n.pick_now(f" {len(card['lessons'])} lesson(s) saved, and they take effect next round.",
                              f" 已记下 {len(card['lessons'])} 条教训,下一轮生效。")
    return line
