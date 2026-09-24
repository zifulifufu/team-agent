"""Group owner delegation: split a task along member strengths, order it, and chain the
results into one another.

Flow (driven inside the orchestrator; this module only owns the "plan" data and a few prompts):
  1. The owner receives the task, looks at the "members and strengths" roster, and emits
     <plan>{...}</plan>: the overall goal, group-wide conventions, and several tasks
     (each task: who does it, what it is, which tasks it depends on, which strength to
     use, which tools to prefer, what to deliver).
  2. The program validates the plan (the assignee must be a group member, dependencies
     must exist, no cycles) and orders execution by dependency.
  3. Execution in order: each member receives its own task + the conventions + the full
     output of upstream members + the group roster, and the first line of its reply must
     state "what I own, which strength I use, which tools I use, who I take over from".
  4. Once everything is done the owner merges it into the final answer.
"""

from __future__ import annotations

from . import i18n, presets

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
    files: list[str] = field(default_factory=list)   # what this task actually produced, once it ran
    status: str = "pending"
    message_id: str = ""
    error: str = ""
    dir: str = ""          # workspace-relative folder this task delivers into (set when it runs)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "owner": self.owner, "owner_id": self.owner_id, "title": self.title,
            "instruction": self.instruction, "needs": self.needs, "strengths": self.strengths,
            "tools": self.tools, "deliverable": self.deliverable, "status": self.status,
            "message_id": self.message_id, "error": self.error, "dir": self.dir, "files": self.files,
        }


@dataclass
class Plan:
    goal: str
    conventions: str
    tasks: list[PlanTask]
    status: str = "running"      # running | integrating | done | stopped | failed
    message_id: str = ""
    # Filled in once the tasks are finished (see scoring.py). It lives on the plan because the
    # scorecard is a judgement about this round's hand-offs, and the task board is what the round
    # produced — so the board is where a reader expects to find it.
    scorecard: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        meta = {"kind": "plan", "goal": self.goal, "conventions": self.conventions, "status": self.status,
                "tasks": [t.to_dict() for t in self.tasks]}
        if self.scorecard:
            meta["score"] = self.scorecard
        return meta

    def by_id(self, tid: str) -> PlanTask | None:
        return next((t for t in self.tasks if t.id == tid), None)


class PlanError(Exception):
    pass


# --------------------------------------------------------------------- parsing
_PLAN_TAG = re.compile(r"<plan>(.*?)(?:</plan>|\Z)", re.S)


def extract_plan_json(text: str) -> dict | None:
    """Extract the plan JSON from the owner's reply: prefer the <plan> tag, then a ```json
code block. Returns None when absent; raises PlanError when malformed."""
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


def member_names(member: dict) -> list[str]:
    """Every spelling a plan's `owner` field may use for this member.

    Three, and all three are needed. The stored `name` ("Copywriter"). The other-language spelling of
    a built-in member ("文案") — read from `presets`, **the same source the @mention resolver reads**,
    because a second, weaker matcher living here is exactly what caused the failure this fixes: in one
    session the coordinator wrote 「文案」「分镜」「校对」 as owners and every plan was thrown away,
    twelve turns in a row, because only the stored English name was accepted while the rest of the
    conversation addressed those members in Chinese. And the `role`, because a model that has just read
    a roster of roles naturally writes the role.
    """
    stored = str(member.get("name") or "").strip()
    out = [stored] if stored else []
    twin = presets.twin_name(stored) if stored else None
    if twin and twin not in out:
        out.append(twin)
    # Both languages of the role, including the localized one from the built-in table. A model that
    # has just read a Chinese roster writes 「审校」 while the stored role is "Proofreader" — the same
    # failure that threw away twelve consecutive plans, one level down: the name matched, the role
    # did not. `role_zh` is not stored on the member row, so it is read from the preset table.
    entry = presets.builtin_for(stored or None) or {}
    for value in (member.get("role"), member.get("role_zh"),
                  entry.get("role"), entry.get("role_zh")):
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def owner_list(members: list[dict]) -> str:
    """The owners a plan may name, written out. Handed to the model and put in the error.

    Saying "not a member" without saying who *is* one is how a model repeats the same mistake: it is
    told no and given nothing to correct towards.

    A media member's role is a whole line ("Media member · Video generation · Doubao Seedance
    (Volcengine Ark)"), so only its first segment is shown — the full line in every prompt entry would
    bury the names that actually matter.
    """
    parts = []
    for m in members:
        stored = str(m.get("name") or "").strip()
        twin = presets.twin_name(stored) if stored else None
        role = str(m.get("role") or "").split("·")[0].strip()
        label = stored + (f"（{twin}）" if twin and twin != role else "")
        parts.append(f"{label} · {role}" if role and role != stored else label)
    return "; ".join(parts)


def _match_member(name: str, members: list[dict]) -> dict | None:
    """The member a plan's `owner` means, by any of the spellings `member_names` allows.

    Exact first, then case-insensitive, then a loose containment pass over names and roles. The loose
    pass over a role is allowed to match at most one member — a media member's role is a long line like
    "Media member · Video generation · Doubao Seedance (Volcengine Ark)", and without that guard the
    word "video" would hand the task to whichever such member happened to come first.
    """
    n = str(name or "").strip().lstrip("@").strip()
    if not n:
        return None
    low = n.lower()
    all_names = {m["id"]: member_names(m) for m in members}
    # A canonical name wins over another member's role/alias. At every looser tier,
    # ambiguity is a planning error, never an implicit choice of the first member.
    for candidates in (
        [m for m in members if m["name"] == n],
        [m for m in members if m["name"].lower() == low],
        [m for m in members if any(x.lower() == low for x in all_names[m["id"]])],
        [m for m in members if any(x and (x.lower() in low or low in x.lower())
                                   for x in all_names[m["id"]])],
    ):
        if candidates:
            return candidates[0] if len(candidates) == 1 else None
    if len(low) >= 3:
        by_role = [m for m in members
                   if any(low in str(seg).strip().lower()
                          for seg in str(m.get("role") or "").split("·"))]
        if len(by_role) == 1:
            return by_role[0]
    return None


def _as_list(v: Any) -> list:
    """Models sometimes write a list as a single value (needs: 1 / tools: "x"): normalize it
to a list so a type quirk cannot turn into a crash."""
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def build_plan(obj: dict, members: list[dict], max_tasks: int = 8, known_tools: set[str] | None = None) -> Plan:
    """Validate and tidy the plan, ordering tasks by dependency. Raises PlanError with a
specific reason when it is invalid."""
    if not isinstance(obj.get("tasks"), list):
        raise PlanError(i18n.pick_now("tasks is not a list", "tasks 不是列表"))
    if len(obj["tasks"]) > max_tasks:
        raise PlanError(i18n.pick_now(f"The plan exceeds the limit of {max_tasks} tasks",
                                      f"计划超过 {max_tasks} 个任务的上限"))
    raw_tasks = obj["tasks"]
    if not raw_tasks:
        raise PlanError(i18n.pick_now("tasks is empty", "tasks 是空的"))
    tasks: list[PlanTask] = []
    used: set[str] = set()
    for i, rt in enumerate(raw_tasks, 1):
        if not isinstance(rt, dict):
            raise PlanError(i18n.pick_now(f"Task {i} is not an object", f"第 {i} 个任务不是对象"))
        owner = _match_member(rt.get("owner", ""), members)
        if not owner:
            raise PlanError(i18n.pick_now(
                f"Task \"{rt.get('title') or i}\" names \"{rt.get('owner')}\" as its owner, and that is "
                f"not a member of this group. Write one of these: {owner_list(members)}.",
                f"任务「{rt.get('title') or i}」的负责人「{rt.get('owner')}」不是群成员。"
                f"请改成其中之一:{owner_list(members)}。"))
        tid = str(rt.get("id") or f"t{i}").strip()
        if not tid or tid in used:
            raise PlanError(i18n.pick_now(f"Task ID is empty or duplicated: {tid}",
                                          f"任务 ID 为空或重复:{tid}"))
        used.add(tid)
        instruction = str(rt.get("instruction") or rt.get("desc") or rt.get("title") or "").strip()
        if not instruction:
            raise PlanError(i18n.pick_now(f"Task {tid} is missing its instruction", f"任务 {tid} 缺少 instruction"))
        strengths = [s for s in _as_list(rt.get("strengths")) if isinstance(s, str)][:4]
        tools = [t for t in _as_list(rt.get("tools")) if isinstance(t, str) and (known_tools is None or t in known_tools)][:4]
        tasks.append(PlanTask(
            id=tid, owner=owner["name"], owner_id=owner["id"], title=str(rt.get("title") or instruction[:20]).strip()[:40],
            instruction=instruction, needs=[str(n).strip() for n in _as_list(rt.get("needs"))],
            strengths=strengths, tools=tools, deliverable=str(rt.get("deliverable") or "").strip(),
        ))
    ids = {t.id for t in tasks}
    for t in tasks:
        if t.id in t.needs or any(n not in ids for n in t.needs):
            raise PlanError(i18n.pick_now(f"Task {t.id} has an unknown or self dependency: {t.needs}",
                                          f"任务 {t.id} 存在未知依赖或自我依赖:{t.needs}"))
        t.needs = list(dict.fromkeys(t.needs))
    plan = Plan(goal=str(obj.get("goal") or "").strip()[:200], conventions=str(obj.get("conventions") or "").strip()[:800],
                tasks=_toposort(tasks))
    return plan


def _toposort(tasks: list[PlanTask]) -> list[PlanTask]:
    """Stable topological sort: keeps the order given by the model when there is no dependency
conflict. Raises PlanError on a cycle."""
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


# --------------------------------------------------------------------- prompts
def planning_instruction(max_tasks: int, mode: str, past_actions: str = "",
                         owners: str = "") -> str:
    """The planning prompt. `owners` is `owner_list(members)` when the caller has the roster.

    Passing it is what keeps a plan from being discarded over a spelling. A model that reads a roster
    of English names but a conversation in Chinese writes 「文案」 for the owner, and without the list
    of writeable spellings in front of it there is nothing to copy — which is how one session lost
    twelve consecutive plans. The prompt is also the only place the model can be told, before it
    decides, that 「文案」 and "Copywriter" are the same member.
    """
    force = (i18n.pick_now("You must split the work this time: output the plan straight away.", "这次必须分工:请直接输出计划。") if mode == "on"
             else i18n.pick_now("Decide first: if you can answer this well on your own (small talk, a simple question, a single step), answer directly and do not output <plan>.", "先判断:如果这件事你一个人就能答好(闲聊、简单问答、单一步骤),就直接回答,不要输出 <plan>。"))
    owner_line = ""
    if owners:
        owner_line = "\n" + i18n.pick_now(
            f"Write owner exactly as one of these (either spelling is accepted): {owners}",
            f"owner 请原样写成下面之一(两种写法都认):{owners}")
    return (
        i18n.pick_now("[Plan mode] You are the host, so arrange the team before you answer.\n", "【分工模式】你是群主,现在要先安排团队。\n") + force + i18n.pick_now((
            "\n"
            "To split the work: explain your thinking to the user in one or two sentences, then output the plan (strict JSON inside a <plan> tag):\n"
            "<plan>{\"goal\": \"the overall goal in one sentence\", \"conventions\": \"the wording, terminology, format, audience and length the whole team has to agree on (leave empty if there are none)\", "
            "\"tasks\": [{\"id\": \"t1\", \"owner\": \"member name\", \"title\": \"task name\", \"instruction\": \"exactly what to do and how far to take it\", "
            "\"needs\": [], \"strengths\": [\"writing\"], \"tools\": [\"library_search\"], \"deliverable\": \"the shape of the deliverable\"}]}</plan>\n"
            "How to split it:\n"
            "1. Assign strictly by the strengths, skills and available tools listed under \"Members and their parts\" above; never hand a task to someone clearly not suited to it;\n"
            "2. Use needs to state real dependencies; independent tasks should have no dependency. The runtime executes in dependency order and skips tasks whose prerequisites failed; no dependency loops;\n"
            "3. The split has to cover everything the user asked for, with nothing overlapping and nothing missed; you own the final consolidation, so do not add a separate consolidation task;\n"
            f"4. Use no more than {max_tasks} tasks; owner must be the name of a group member (without the @); tools may only list tool names you can actually see;\n"
            "5. conventions is what stops the members from talking past each other: anything more than one of them will touch (names, numbers, tone, format, timeline) belongs here.\n"
            "6. **deliverable must be a file that ends up in the workspace, with its name** — e.g. "
            "\"交付/科普脚本.docx\" — and say which tool writes it (`write_document` for a document, a "
            "deck, a workbook or a text file; `make_figure`/`generate_image` for a picture; "
            "`assemble_video` for a film). \"a script\" is not a deliverable: a task whose deliverable "
            "names a file is checked against the workspace, and a task with no file cannot be handed to "
            "anyone or published. A task that genuinely produces no file (a review, a decision, a "
            "round of questions) says so in words."
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
            "5. conventions 是防止「各说各话」的关键:凡是多人都会碰到的口径(名称、数字、语气、格式、时间线)都写在这里;\n"
            "6. **deliverable 必须是落在工作目录里的文件,并写出文件名** —— 例如「交付/科普脚本.docx」—— "
            "并写明用哪个工具写(`write_document` 写文档/幻灯片/表格/文本,`make_figure`/`generate_image` 出图,"
            "`assemble_video` 出片)。写「一份脚本」不算交付物:凡 deliverable 里写了文件名,程序会去工作目录里核对;"
            "**没有文件的任务,既不能交给下游,也不能交付给用户**。确实不产出文件的任务(评审、决策、提问)用文字写明。"
        )) + owner_line
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
            # The upstream member's own words, clipped — and when it also wrote a file, that file is
            # named and readable: the group's workspace is indexed into its own library before each
            # turn, so the *whole* of a long script is one `library_read` away instead of being lost
            # past the clip. This is what keeps a 180-second script's tail from disappearing between
            # two members that each promise the next one "the full text".
            files = i18n.pick_now(f" (files it wrote: {', '.join(up.files)})", f"(它落盘的文件:{'、'.join(up.files)})") if up.files else ""
            ups.append(i18n.pick_now(f"[{up.id} · result from {up.owner}: {up.title}]{files}\n{_clip(outputs[nid], 3500)}", f"[{up.id} · {up.owner} 的成果:{up.title}]{files}\n{_clip(outputs[nid], 3500)}"))
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
            "4. Do not @mention other members — coordinating and consolidating is the host's job.\n"
            "5. **A deliverable is a file.** If this task's deliverable names a file (`.docx`, `.pptx`, "
            "`.xlsx`, `.md`, a picture, a clip), the task is finished when that file is in the group's "
            "workspace — write it with the tool that produces it (`write_document` for a document, a "
            "deck, a workbook or a text file) and put the path in your answer. Describing a document in "
            "the chat is not delivering it, and a task whose file never appears is marked as failed."
        ), (
            "\n"
            "2. 只做你这一项,别人的部分不要代劳;严格遵守统一约定;\n"
            "3. 上游成果有问题时,先指出,再基于修正后的版本继续;\n"
            "4. 不要 @ 其他成员——协调和汇总由群主完成;\n"
            "5. **交付物就是文件。** 本任务的 deliverable 里写了文件名(`.docx`/`.pptx`/`.xlsx`/`.md`、"
            "图片、片子)时,只有那个文件真的出现在本群工作目录里才算做完 —— 用产它的工具写出来"
            "(`write_document` 写文档/幻灯片/表格/文本),并在回答里写出路径。在群里描述一份文档不算交付,"
            "文件没出现的任务会被判为未完成。"
        ))
    )
    return "\n\n".join(parts)


def integration_prompt(plan: Plan, outputs: dict[str, str], budget: int = 14000) -> str:
    per = max(800, budget // max(len(plan.tasks), 1))
    blocks = []
    for t in plan.tasks:
        body = outputs.get(t.id)
        produced = i18n.pick_now(f" (files in the workspace: {', '.join(t.files)})",
                                 f"(落在工作目录里的文件:{'、'.join(t.files)})") if t.files else ""
        blocks.append(i18n.pick_now(f"[{t.id} · {t.owner}: {t.title}](status: {t.status}){produced}\n", f"[{t.id} · {t.owner}:{t.title}](状态:{t.status}){produced}\n") + (_clip(body, per) if body else i18n.pick_now("(no output)", "(无产出)")))
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
            "4. **List the files the group actually produced**, as paths relative to the workspace, "
            "one per line — the user opens those, so a file must never be silently omitted. If a task's "
            "deliverable names a file and it is not among them, that task delivered nothing: say so "
            "plainly, do not present its text as the deliverable;\n"
        ), (
            "\n\n"
            "要求:\n1. 直接给出可交付的最终成品,而不是「各位的总结」;\n"
            "2. 统一口径,成员之间有冲突的地方选更合理的并简要说明;\n"
            "3. 在关键部分用「(来自:成员名)」标明出处;\n"
            "4. **把这一轮真正产出的文件列出来**(相对工作目录的路径,一行一个)—— 用户要打开的就是它们,"
            "不许漏掉。如果某个任务的交付物是文件而列表里没有它,那就是没交付:请明说,不要把它的文字当作成品;\n"
        ))
        + (i18n.pick_now(f"5. These tasks did not finish: {', '.join(t.id + t.title for t in failed)} — say clearly what is missing and how to make up for it;\n", f"5. 这些任务没有完成:{'、'.join(t.id + t.title for t in failed)},请明确指出缺了什么并给出补救办法;\n") if failed else "")
        + i18n.pick_now("Close with 1-3 next steps, or questions the user needs to answer. Do not @mention anyone.", "最后给出 1~3 条下一步建议或需要用户确认的问题。不要再 @ 任何人。")
    )


def summarize(plan: Plan) -> str:
    """Body of the task board message (plain text, for places that do not render cards)."""
    lines = [i18n.pick_now(f"Plan: {plan.goal}", f"分工:{plan.goal}") if plan.goal else i18n.pick_now("Plan", "分工")]
    for t in plan.tasks:
        lines.append(f"{t.id} {t.owner}:{t.title} [{t.status}]")
    return "\n".join(lines)
