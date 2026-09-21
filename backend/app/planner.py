"""群主分工:把一个任务按成员的强项拆开、排好先后、让成果一环扣一环地接起来。

流程(orchestrator 里驱动,本模块只负责「计划」这份数据和几段提示词):
  1. 群主收到任务,看着「成员与强项」分工表,输出 <plan>{…}</plan>:总目标、全组统一约定、若干任务
     (每个任务:谁来做、做什么、依赖哪些任务、发挥什么强项、建议用什么工具、交付什么)。
  2. 程序校验计划(负责人必须是群成员、依赖必须存在、不能有环),并按依赖排出执行顺序。
  3. 依次执行:每个成员收到自己的任务 + 统一约定 + 上游成员的完整成果 + 全组分工表,
     发言第一行要先声明「我负责什么、发挥什么强项、用什么工具、承接谁」。
  4. 全部完成后群主整合成最终答复。
"""

from __future__ import annotations

from . import i18n

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanTask:
    id: str
    owner: str
    owner_id: str
    title: str
    instruction: str
    needs: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    deliverable: str = ""
    status: str = "pending"
    message_id: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "owner": self.owner, "owner_id": self.owner_id, "title": self.title,
            "instruction": self.instruction, "needs": self.needs, "strengths": self.strengths,
            "tools": self.tools, "deliverable": self.deliverable, "status": self.status,
            "message_id": self.message_id, "error": self.error,
        }


@dataclass
class Plan:
    goal: str
    conventions: str
    tasks: list[PlanTask]
    status: str = "running"      # running | integrating | done | stopped | failed
    message_id: str = ""

    def to_meta(self) -> dict:
        return {"kind": "plan", "goal": self.goal, "conventions": self.conventions, "status": self.status,
                "tasks": [t.to_dict() for t in self.tasks]}

    def by_id(self, tid: str) -> PlanTask | None:
        return next((t for t in self.tasks if t.id == tid), None)


class PlanError(Exception):
    pass


# ---------------------------------------------------------------------- 解析
_PLAN_TAG = re.compile(r"<plan>(.*?)(?:</plan>|\Z)", re.S)


def extract_plan_json(text: str) -> dict | None:
    """从群主的回复里取出计划 JSON:优先 <plan> 标签,其次 ```json 代码块。没有返回 None,格式坏了抛 PlanError。"""
    m = _PLAN_TAG.search(text)
    raw = m.group(1) if m else None
    if raw is None:
        m2 = re.search(r"```(?:json)?\s*(\{.*?\"tasks\".*?\})\s*```", text, re.S)
        raw = m2.group(1) if m2 else None
    if raw is None:
        return None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        obj = json.loads(raw)
    except ValueError:
        i, j = raw.find("{"), raw.rfind("}")
        try:
            obj = json.loads(raw[i:j + 1]) if 0 <= i < j else None
        except ValueError:
            obj = None
    if not isinstance(obj, dict) or not isinstance(obj.get("tasks"), list):
        raise PlanError(i18n.pick_now("The plan is not valid JSON, or it has no tasks array", "计划不是合法的 JSON,或缺少 tasks 数组"))
    return obj


def _match_member(name: str, members: list[dict]) -> dict | None:
    n = str(name or "").strip().lstrip("@").strip()
    for m in members:
        if m["name"] == n:
            return m
    for m in members:  # 宽松:「文案」写成「文案写手」之类
        if n and (n in m["name"] or m["name"] in n):
            return m
    return None


def _as_list(v: Any) -> list:
    """模型偶尔把列表写成单个值(needs: 1 / tools: "x"):统一成列表,别让类型问题变成崩溃。"""
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def build_plan(obj: dict, members: list[dict], max_tasks: int = 8, known_tools: set[str] | None = None) -> Plan:
    """校验并整理计划,任务按依赖顺序排好。不合法抛 PlanError(带具体原因)。"""
    if not isinstance(obj.get("tasks"), list):
        raise PlanError(i18n.pick_now("tasks is not a list", "tasks 不是列表"))
    raw_tasks = obj["tasks"][:max_tasks]
    if not raw_tasks:
        raise PlanError(i18n.pick_now("tasks is empty", "tasks 是空的"))
    tasks: list[PlanTask] = []
    used: set[str] = set()
    remap: dict[str, str] = {}
    for i, rt in enumerate(raw_tasks, 1):
        if not isinstance(rt, dict):
            raise PlanError(i18n.pick_now(f"Task {i} is not an object", f"第 {i} 个任务不是对象"))
        owner = _match_member(rt.get("owner", ""), members)
        if not owner:
            raise PlanError(i18n.pick_now(f"Task \"{rt.get('title') or i}\" names \"{rt.get('owner')}\" as its owner, and that is not a member of this group", f"任务「{rt.get('title') or i}」的负责人「{rt.get('owner')}」不是群成员"))
        tid = str(rt.get("id") or f"t{i}").strip()
        if tid in used:
            tid = f"t{i}"
        while tid in used:
            tid += "_"
        used.add(tid)
        remap[str(rt.get("id") or f"t{i}")] = tid
        instruction = str(rt.get("instruction") or rt.get("desc") or rt.get("title") or "").strip()
        if not instruction:
            raise PlanError(i18n.pick_now(f"Task {tid} is missing its instruction", f"任务 {tid} 缺少 instruction"))
        strengths = [s for s in _as_list(rt.get("strengths")) if isinstance(s, str)][:4]
        tools = [t for t in _as_list(rt.get("tools")) if isinstance(t, str) and (known_tools is None or t in known_tools)][:4]
        tasks.append(PlanTask(
            id=tid, owner=owner["name"], owner_id=owner["id"], title=str(rt.get("title") or instruction[:20]).strip()[:40],
            instruction=instruction, needs=[str(n) for n in _as_list(rt.get("needs"))],
            strengths=strengths, tools=tools, deliverable=str(rt.get("deliverable") or "").strip(),
        ))
    ids = {t.id for t in tasks}
    for t in tasks:
        t.needs = [remap.get(n, n) for n in t.needs]
        t.needs = [n for n in dict.fromkeys(t.needs) if n in ids and n != t.id]
    plan = Plan(goal=str(obj.get("goal") or "").strip()[:200], conventions=str(obj.get("conventions") or "").strip()[:800],
                tasks=_toposort(tasks))
    return plan


def _toposort(tasks: list[PlanTask]) -> list[PlanTask]:
    """稳定拓扑排序:没有依赖冲突时保持模型给出的顺序。有环抛 PlanError。"""
    done: list[PlanTask] = []
    done_ids: set[str] = set()
    remaining = list(tasks)
    while remaining:
        ready = next((t for t in remaining if all(n in done_ids for n in t.needs)), None)
        if ready is None:
            raise PlanError(i18n.pick_now("These tasks depend on each other in a loop: ", "任务之间存在循环依赖:") + i18n.pick_now(", ", "、").join(t.id for t in remaining))
        remaining.remove(ready)
        done.append(ready)
        done_ids.add(ready.id)
    return done


# ---------------------------------------------------------------------- 提示词
def planning_instruction(max_tasks: int, mode: str, past_actions: str = "") -> str:
    force = (i18n.pick_now("You must split the work this time: output the plan straight away.", "这次必须分工:请直接输出计划。") if mode == "on"
             else i18n.pick_now("Decide first: if you can answer this well on your own (small talk, a simple question, a single step), answer directly and do not output <plan>.", "先判断:如果这件事你一个人就能答好(闲聊、简单问答、单一步骤),就直接回答,不要输出 <plan>。"))
    return (
        i18n.pick_now("[Plan mode] You are the host, so arrange the team before you answer.\n", "【分工模式】你是群主,现在要先安排团队。\n") + force + i18n.pick_now((
            "\n"
            "To split the work: explain your thinking to the user in one or two sentences, then output the plan (strict JSON inside a <plan> tag):\n"
            "<plan>{\"goal\": \"the overall goal in one sentence\", \"conventions\": \"the wording, terminology, format, audience and length the whole team has to agree on (leave empty if there are none)\", "
            "\"tasks\": [{\"id\": \"t1\", \"owner\": \"member name\", \"title\": \"task name\", \"instruction\": \"exactly what to do and how far to take it\", "
            "\"needs\": [], \"strengths\": [\"writing\"], \"tools\": [\"library_search\"], \"deliverable\": \"the shape of the deliverable\"}]}</plan>\n"
            "How to split it:\n"
            "1. Assign strictly by the strengths, skills and available tools listed under \"Members and their parts\" above; never hand a task to someone clearly not suited to it;\n"
            "2. Use needs to state the dependencies (a downstream member receives the upstream result in full); tasks without dependencies can run in parallel; no dependency loops;\n"
            "3. The split has to cover everything the user asked for, with nothing overlapping and nothing missed; you own the final consolidation, so do not add a separate consolidation task;\n"
            f"4. Use no more than {max_tasks} tasks; owner must be the name of a group member (without the @); tools may only list tool names you can actually see;\n"
            "5. conventions is what stops the members from talking past each other: anything more than one of them will touch (names, numbers, tone, format, timeline) belongs here."
        ), (
            "\n"
            "需要分工时:先用一两句话向用户说明你的思路,然后输出计划(严格 JSON,放在 <plan> 标签里):\n"
            '<plan>{"goal": "一句话总目标", "conventions": "全组必须统一的口径、术语、格式、受众、篇幅等(没有就留空)", '
            '"tasks": [{"id": "t1", "owner": "成员名", "title": "任务名", "instruction": "具体做什么、做到什么程度", '
            '"needs": [], "strengths": ["writing"], "tools": ["library_search"], "deliverable": "交付物形式"}]}</plan>\n'
            "分工原则:\n"
            "1. 严格按上面「群成员与分工」里的强项、技能和可用工具来分配;不要把任务派给明显不擅长的人;\n"
            "2. 用 needs 写清依赖(下游成员会拿到上游的完整成果);没有依赖的任务可以并列;不要循环依赖;\n"
            "3. 分工要覆盖用户的全部要求,不重叠、不遗漏;最终汇总由你负责,不要单列汇总任务;\n"
            f"4. 任务数不超过 {max_tasks} 个;owner 必须是群成员名字(不带 @);tools 只能填你确实看到的工具名;\n"
            "5. conventions 是防止「各说各话」的关键:凡是多人都会碰到的口径(名称、数字、语气、格式、时间线)都写在这里。"
        ))
        + (i18n.pick_now(f"\n\n[How similar tasks were handled before, for reference]\n{past_actions}", f"\n\n【以往类似任务的做法,可参考】\n{past_actions}") if past_actions else "")
    )


def declaration_rule() -> str:
    return (
        i18n.pick_now((
            "Start with one line of declaration, in this form: [Assignment] I am responsible for ...; using the ... strength; with ... (tool or skill, or \"none\"); building on ... (upstream member, or \"none\"). "
            "Then give the deliverable."
        ), (
            "第一行先写一句声明,格式:【分工】我负责…;发挥…强项;用…(工具/技能,没有写「无」);承接…(上游成员,没有写「无」)。"
            "然后再给出交付物。"
        ))
    )


def assignments_text(plan: Plan, me: PlanTask) -> str:
    lines = []
    for t in plan.tasks:
        mark = i18n.pick_now("  <- you", "  ← 你") if t.id == me.id else ""
        dep = i18n.pick_now(f"(building on {', '.join(t.needs)})", f"(承接 {'、'.join(t.needs)})") if t.needs else ""
        lines.append(f"- {t.id} {t.owner}:{t.title}{dep}{mark}")
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + i18n.pick_now(f"\n...(the rest is omitted; {len(text)} characters in all)", f"\n…(后文已省略,共 {len(text)} 字)")


def task_prompt(plan: Plan, task: PlanTask, outputs: dict[str, str], index: int) -> str:
    ups = []
    for nid in task.needs:
        up = plan.by_id(nid)
        if up and outputs.get(nid):
            ups.append(i18n.pick_now(f"[{up.id} · result from {up.owner}: {up.title}]\n{_clip(outputs[nid], 3500)}", f"[{up.id} · {up.owner} 的成果:{up.title}]\n{_clip(outputs[nid], 3500)}"))
        elif up:
            ups.append(i18n.pick_now(f"[{up.id} · {up.owner}: {up.title}] produced nothing (status: {up.status}); do your best with what is available and say what is missing.", f"[{up.id} · {up.owner}:{up.title}] 该任务没有产出(状态:{up.status}),请基于已有信息尽力完成,并说明缺了什么。"))
    parts = [
        i18n.pick_now(f"[Task {index}/{len(plan.tasks)}] {task.title}", f"【分工任务 {index}/{len(plan.tasks)}】{task.title}"),
        i18n.pick_now(f"Overall goal: {plan.goal or '(see the user request)'}", f"总目标:{plan.goal or '(见用户需求)'}"),
    ]
    if plan.conventions:
        parts.append(i18n.pick_now(f"Team-wide conventions (binding — do not talk past each other):\n{plan.conventions}", f"全组统一约定(必须遵守,不得各说各话):\n{plan.conventions}"))
    parts.append(i18n.pick_now(f"Whole-team plan:\n{assignments_text(plan, task)}", f"全组分工:\n{assignments_text(plan, task)}"))
    parts.append(i18n.pick_now(f"Your task: {task.instruction}", f"你的任务:{task.instruction}"))
    if task.deliverable:
        parts.append(i18n.pick_now(f"Deliverable: {task.deliverable}", f"交付物:{task.deliverable}"))
    if task.strengths:
        parts.append(i18n.pick_now(f"The host thinks this task calls for your strength in: {', '.join(task.strengths)}.", f"群主认为这项任务需要你发挥:{'、'.join(task.strengths)}。"))
    if task.tools:
        parts.append(i18n.pick_now(f"Tools to reach for first: {', '.join(task.tools)} (call them when needed rather than working from memory).", f"建议优先使用的工具:{'、'.join(task.tools)}(需要时先调用,别凭记忆)。"))
    if ups:
        parts.append(i18n.pick_now("Upstream results (carry on from these; do not redo them and do not restate them):\n", "上游成果(在此基础上继续,不要重做、不要复述):\n") + "\n\n".join(ups))
    parts.append(
        i18n.pick_now("Requirements:\n1. ", "要求:\n1. ") + declaration_rule() + i18n.pick_now((
            "\n"
            "2. Do only your own item — do not do someone else's part; follow the conventions exactly;\n"
            "3. If an upstream result has a problem, point it out first, then continue from the corrected version;\n"
            "4. Do not @mention other members — coordinating and consolidating is the host's job."
        ), (
            "\n"
            "2. 只做你这一项,别人的部分不要代劳;严格遵守统一约定;\n"
            "3. 上游成果有问题时,先指出,再基于修正后的版本继续;\n"
            "4. 不要 @ 其他成员——协调和汇总由群主完成。"
        ))
    )
    return "\n\n".join(parts)


def integration_prompt(plan: Plan, outputs: dict[str, str], budget: int = 14000) -> str:
    per = max(800, budget // max(len(plan.tasks), 1))
    blocks = []
    for t in plan.tasks:
        body = outputs.get(t.id)
        blocks.append(i18n.pick_now(f"[{t.id} · {t.owner}: {t.title}](status: {t.status})\n", f"[{t.id} · {t.owner}:{t.title}](状态:{t.status})\n") + (_clip(body, per) if body else i18n.pick_now("(no output)", "(无产出)")))
    failed = [t for t in plan.tasks if t.status != "done"]
    return (
        i18n.pick_now((
            "[Integration] Every task in the plan has finished; now give the user the final answer.\n"
            f"Overall goal: {plan.goal or '(see the user request)'}\n"
        ), (
            "【整合】分工任务已全部执行完毕,现在由你给用户最终答复。\n"
            f"总目标:{plan.goal or '(见用户需求)'}\n"
        ))
        + (i18n.pick_now(f"Conventions: {plan.conventions}\n", f"统一约定:{plan.conventions}\n") if plan.conventions else "")
        + i18n.pick_now("\nWhat each member produced:\n", "\n各成员成果:\n") + "\n\n".join(blocks) + i18n.pick_now((
            "\n\n"
            "Requirements:\n1. Give the finished deliverable itself, not \"a summary of what everyone said\";\n"
            "2. Use one consistent wording; where members conflict, pick the more reasonable one and say briefly why;\n"
            "3. Mark the source of key parts as \"(from: member name)\";\n"
        ), (
            "\n\n"
            "要求:\n1. 直接给出可交付的最终成品,而不是「各位的总结」;\n"
            "2. 统一口径,成员之间有冲突的地方选更合理的并简要说明;\n"
            "3. 在关键部分用「(来自:成员名)」标明出处;\n"
        ))
        + (i18n.pick_now(f"4. These tasks did not finish: {', '.join(t.id + t.title for t in failed)} — say clearly what is missing and how to make up for it;\n", f"4. 这些任务没有完成:{'、'.join(t.id + t.title for t in failed)},请明确指出缺了什么并给出补救办法;\n") if failed else "")
        + i18n.pick_now("Close with 1-3 next steps, or questions the user needs to answer. Do not @mention anyone.", "最后给出 1~3 条下一步建议或需要用户确认的问题。不要再 @ 任何人。")
    )


def summarize(plan: Plan) -> str:
    """任务板消息的正文(纯文本,给不渲染卡片的地方用)。"""
    lines = [i18n.pick_now(f"Plan: {plan.goal}", f"分工:{plan.goal}") if plan.goal else i18n.pick_now("Plan", "分工")]
    for t in plan.tasks:
        lines.append(f"{t.id} {t.owner}:{t.title} [{t.status}]")
    return "\n".join(lines)
