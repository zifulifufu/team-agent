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

from . import docwrite, i18n, presets, cooperation

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ⭐⭐ **The one place that decides what counts as "a file name" in this app.** It used to be four,
# and they disagreed — which is how a task ends up unverified:
#
#     planner._DELIVERABLE_EXT    16 extensions   "does it name a file at all"
#     planner._ANY_SUFFIX         any suffix      only used to word a complaint
#     orchestrator._FILE_IN_PLAN  27 extensions   **whether to verify the delivery at all**
#     proclog.FILE_IN_TEXT        12 extensions   the ledger's "promised and not delivered"
#
# Three measured consequences of the disagreement, all of them silent:
#
#   * `notes.tex` / `animation.json` / `index.html` matched nothing in `_FILE_IN_PLAN`, so
#     `_execute_plan` skipped delivery verification for those tasks **entirely** and left them
#     `done` with nothing on disk. `video/…-字幕轨.html` is exactly the shape this group planned.
#   * `voice/旁白.wav` and `out/clip.mkv` matched nothing in `proclog.FILE_IN_TEXT`, so the process
#     engineer's "promised and not delivered" count could never see an audio or video handover.
#   * `video/x.srt` matched nothing in `_DELIVERABLE_EXT`, so a **production** task whose deliverable
#     plainly named a file was refused with "you must name your output file". `.srt` is one of the
#     two files `assemble_video` emits, beside the film.
#
# The leading letter is required so a version number inside a name ("v2.3") is not read as a suffix.
_FILE_SUFFIX = r"[a-z][a-z0-9]{1,4}"
_FILE_NAME = re.compile(r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff .()（）\-]*\." + _FILE_SUFFIX + r"\b", re.I)
# ⚠️ A name may contain spaces and CJK — `中秋散文（配图）.docx` is a real deliverable, and the
# ledger compares names *whole* (`n not in files`), so cutting the space out would turn a delivered
# `Final Report.docx` into a false "promised and not delivered". What it may **not** contain is the
# punctuation that separates one deliverable from the next: `交付/综述.md、交付/综述.docx` is two
# files, and read as one blob it reported the pair as a single missing file.
_CLAUSE = re.compile(r"[，,。；;！？!?、\n\r]+")


def named_files(text: object) -> list[str]:
    """The file names `text` mentions, in order and deduplicated.

    The plan validator, the executor and the ledger all ask this one function instead of keeping a
    list of extensions each. Two of those lists had grown apart, and the way they disagreed made a
    deliverable that was never written read as delivered — the failure mode this whole file is
    arranged to avoid.
    """
    out: list[str] = []
    for clause in _CLAUSE.split(str(text or "")):
        for m in _FILE_NAME.finditer(clause):
            name = m.group(0).strip()
            if name and name not in out:
                out.append(name)
    return out


# ⭐ Tools whose result **is a file**, so a task using one must name its output — otherwise nothing
# can be checked against the workspace and the task can be reported `done` with nothing on disk.
#
# ⚠️ This used to be six names, inline, and it was missing every drawing/sound/print tool that was
# added later: `make_figure`, `make_music`, `render_document`, `study_video`. A plan that gave
# `make_figure` to a member **and promised no file** passed this check, and then `_execute_plan`
# found no file name to verify against and skipped verification altogether — so "draw a diagram"
# could end as `done` without a diagram. The list is kept whole and guarded by
# `test_every_file_writing_tool_is_declared_a_producer` in the tests, which fails when a new
# `make_*` / `render_*` tool appears and is not declared here.
#
# Deliberately **not** here: tools that write a file *incidentally* while answering something else
# (`process_log` keeps the ledger, `memory_save` records a memory) — their file is a side effect, not
# the deliverable, and demanding a file name for them would be wrong. `run_code` is here because it
# is the one route to a format `write_document` cannot write, and a task that uses it to produce a
# file has to say which file.
FILE_TOOLS = (
    "assemble_video", "generate_image", "generate_video", "make_animation", "make_figure",
    "make_music", "render_document", "run_code", "study_video", "synthesize_speech",
    "write_document",
)

# The tools that write a file **as a side effect of answering** — see `FILE_TOOLS` above. Named here
# rather than left implicit so the guard in the tests knows what it is allowed to skip.
INCIDENTAL_WRITERS = ("memory_save", "process_log")


def suffixes(text: object) -> set[str]:
    """The lowercase suffixes of every file name `text` mentions."""
    return {n.rsplit(".", 1)[-1].lower() for n in named_files(text)}


# The tools whose *absence* has to be distinguished from an ordinary missing tool: a review that
# cannot run means "no quality approval was granted", not "this task could not be attempted", and
# the two are handled differently (see `unavailable_reviews` below, and `_pending_reviews` in the
# orchestrator). One tuple, because both places have to agree on it — this is the same duplication
# that produced four extension lists, one level down.
REVIEW_TOOLS = ("review_picture", "review_audio")


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
    arguments: dict = field(default_factory=dict)  # structured input for a generating member
    artifacts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "owner": self.owner, "owner_id": self.owner_id, "title": self.title,
            "instruction": self.instruction, "needs": self.needs, "strengths": self.strengths,
            "tools": self.tools, "deliverable": self.deliverable, "status": self.status,
            "message_id": self.message_id, "error": self.error, "dir": self.dir, "files": self.files,
            "arguments": self.arguments,
            "artifacts": self.artifacts,
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
    # Tasks the model wrote that could not be used, with the reason, kept so the board can say what
    # was left out. Dropping a task silently would be the same lie as dropping the whole plan used
    # to be: the user would see a smaller board and no hint that part of their request went nowhere.
    dropped: list[str] = field(default_factory=list)
    integration: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        meta = {"kind": "plan", "goal": self.goal, "conventions": self.conventions, "status": self.status,
                "tasks": [t.to_dict() for t in self.tasks]}
        if self.scorecard:
            meta["score"] = self.scorecard
        if self.dropped:
            meta["dropped"] = self.dropped
        if self.integration:
            meta["integration"] = self.integration
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


def build_plan(obj: dict, members: list[dict], max_tasks: int = 8, known_tools: set[str] | None = None,
               member_tools: dict[str, set[str]] | None = None) -> Plan:
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
    dropped: list[str] = []
    first_reason = ""
    for i, rt in enumerate(raw_tasks, 1):
        why = ""
        if not isinstance(rt, dict):
            why = i18n.pick_now(f"task {i} is not an object", f"第 {i} 个任务不是对象")
        owner = None if why else _match_member(rt.get("owner", ""), members)
        if not why and not owner:
            why = i18n.pick_now(
                f"task \"{rt.get('title') or rt.get('id') or i}\" names \"{rt.get('owner')}\" as its "
                f"owner, and that is not a member of this group. Write one of these: {owner_list(members)}.",
                f"任务「{rt.get('title') or rt.get('id') or i}」的负责人「{rt.get('owner')}」不是群成员。"
                f"请改成其中之一:{owner_list(members)}。")
        tid = "" if why else str(rt.get("id") or f"t{i}").strip()
        if not why and (not tid or tid in used):
            # ⚠️ Structural, so it stops the whole plan — the opposite of the per-task rule below.
            # Two tasks sharing an id means the model mistyped, and no program can say which of the
            # two 「t1」 a dependency meant: keeping one of them silently would drop a piece of the
            # user's request *and* leave every `needs: ["t1"]` pointing at an arbitrary one. A
            # missing instruction is a different kind of wrong — the task is unambiguous, only its
            # text is absent — and that one is worth surviving.
            raise PlanError(i18n.pick_now(f"Task ID is empty or duplicated: {tid}",
                                          f"任务 ID 为空或重复:{tid}"))
        # The instruction is what the member is actually handed. The fallback chain is long on
        # purpose: models omit `instruction` and use a synonym, and the *worst* usable answer ("do
        # what the title says") still beats discarding the delegation — which is what used to happen,
        # and it is why this ledger keeps a "the round produced nothing" entry keyed to planning.
        instruction = ""
        if not why:
            for key in ("instruction", "desc", "description", "detail", "brief", "task", "action", "what"):
                instruction = str(rt.get(key) or "").strip()
                if instruction:
                    break
            if not instruction:
                instruction = str(rt.get("title") or "").strip()
            if not instruction and str(rt.get("deliverable") or "").strip():
                instruction = i18n.pick_now(f"Produce {rt['deliverable']}.",
                                            f"产出 {rt['deliverable']}。")
            if not instruction:
                why = i18n.pick_now(f"task {tid} is missing its instruction", f"任务 {tid} 缺少 instruction")
        if why:
            # One unusable task must not cost the whole plan. Measured: a one-task plan whose only
            # task had no instruction was rejected outright, the round fell back to turn-taking, one
            # member replied and it stopped — the flow the user describes as "it does not run".
            dropped.append(f"{tid or f't{i}'}: {why}")
            first_reason = first_reason or why
            continue
        used.add(tid)
        # The executor implements success dependencies, not conditional branches.
        # Do not silently run a standby generator after its primary already worked.
        conditional = re.search(r"(?:仅在|仅当|只在|only\s+(?:if|when|on))[^。\n]{0,100}(?:失败|fail)",
                                str(rt.get("title") or "") + "\n" + instruction, re.I)
        if conditional or any(rt.get(k) for k in ("when", "condition", "on_failure", "fallback_for")):
            raise PlanError(i18n.pick_now(
                f"Task {tid}: conditional standby branches are not supported by this task board. Schedule the primary only; the host can assign a backup after an observed failure. Do not schedule both generators as unconditional tasks.",
                f"任务 {tid}:当前任务板不支持条件备用分支。先只派主路，观察到真实失败后主持人再派备路；不能把两路生成器都当成无条件任务执行。"))
        strengths = [s for s in _as_list(rt.get("strengths")) if isinstance(s, str)][:4]
        tools = list(dict.fromkeys(t for t in _as_list(rt.get("tools")) if isinstance(t, str)
                                  and (known_tools is None or t in known_tools)))
        arguments = rt.get("arguments") or {}
        preflight_error = ""
        if not isinstance(arguments, dict):
            raise PlanError(f"Task {tid}: arguments must be an object")
        if member_tools is not None:
            allowed = member_tools.get(owner["id"], set())
            requested = {t for t in _as_list(rt.get("tools")) if isinstance(t, str)}
            missing = requested - allowed
            unavailable_reviews = missing.intersection(REVIEW_TOOLS) - set().union(*member_tools.values())
            if unavailable_reviews and missing == unavailable_reviews and not cooperation.is_listener(owner):
                # A reviewer becoming unavailable must not invalidate independent
                # local production. Keep the review visible and block its consumers.
                preflight_error = i18n.pick_now(
                    f"Required review tools are unavailable in this group: {sorted(unavailable_reviews)}. Restore the service and rerun verification; no quality approval was granted.",
                    f"本群暂时没有可用的验收工具:{sorted(unavailable_reviews)}。恢复服务后重跑验收，当前不视为质量通过。")
                tools = list(dict.fromkeys([*tools, *sorted(unavailable_reviews)]))
            elif missing or (cooperation.is_listener(owner) and not allowed):
                raise PlanError(i18n.pick_now(
                    f"Task {tid}: {owner['name']} can only use {sorted(allowed)}; assign {sorted(requested - allowed)} to a chat member with those tools.",
                    f"任务 {tid}:{owner['name']} 只能使用 {sorted(allowed)};请把 {sorted(requested - allowed)} 交给实际具备这些工具的成员。"))
            producers = set(FILE_TOOLS)
            produces = requested.intersection(producers) or {t for t in requested if t.startswith("local:")}
            if produces:
                deliverable = str(rt.get("deliverable") or "")
                # ⚠️ The question is "does it name a file **at all**", asked of the one function
                # that answers it. The old test was "does it carry one of 16 known extensions",
                # which refused a perfectly named `video/x.srt` — an extension `assemble_video`
                # itself emits — with a sentence saying no file name had been given.
                named = named_files(deliverable)
                asked = suffixes(deliverable)
                # ⚠️ Two different mistakes used to wear the same sentence here, and the second one
                # is self-contradictory: `video/…-字幕轨.html` **does** name a file, so a model told
                # "you must name your output file" can only re-send the same plan. Measured
                # 2026-09-26: the video group lost two rounds in a row to exactly that — the host
                # planned an .html subtitle track for the HyperFrames chain, was told it had not
                # named a file, rewrote nothing, and the round ended with no work assigned.
                if not named:
                    raise PlanError(i18n.pick_now(
                        f"Task {tid}: a production task must name its output file and extension in "
                        f"deliverable, e.g. \"out/figure-1.png\" — a description of the result is not "
                        f"a file name.",
                        f"任务 {tid}:制作任务必须在 deliverable 里写出**输出文件名和扩展名**(例如 "
                        f"\"out/figure-1.png\"),只写一句成果描述不算文件名。"))
                # ⚠️ `others` is what keeps this off the multi-tool case: a task that hands
                # `out/film.mp4` to `assemble_video` **and** `write_document` is not asking
                # write_document for the mp4, and complaining about it would refuse a sound plan.
                # Whether the suffix is "one of ours" is answered by the tools named, not by a list
                # of extensions repeated here.
                if "write_document" in requested and asked and not (asked & set(docwrite.KINDS)) \
                        and not (produces - {"write_document"}):
                    # ⚠️ Only point at `run_code` when this member actually has it. It is behind a
                    # switch that is off by default (`code_enabled`), and naming a tool that is
                    # not in the roster sends the host to a second rejection: unknown tool names
                    # are filtered out of `tools` silently, so the task would come back with no
                    # tools at all. Measured 2026-09-26: this machine has `code_enabled = False`.
                    writable = ", ".join("." + k for k in docwrite.KINDS)
                    cannot = ", ".join("." + a for a in sorted(asked))
                    way_out = (
                        i18n.pick_now(
                            "Write that file with run_code, or change the deliverable to a format "
                            "write_document can write.",
                            "要产出这种文件请改用 `run_code`,或把 deliverable 换成 write_document 能写的格式。")
                        if "run_code" in allowed else
                        i18n.pick_now(
                            "No tool available here can write that format — change the deliverable "
                            "to one of the formats above.",
                            "本群当前没有任何工具能写这种格式,请把 deliverable 换成上面这些格式之一。"))
                    raise PlanError(i18n.pick_now(
                        f"Task {tid}: write_document writes {writable} and cannot produce {cannot}. "
                        + way_out,
                        f"任务 {tid}:write_document 只能写 {writable},写不了 {cannot}。" + way_out))
        if member_tools is not None and cooperation.is_listener(owner):
            if owner.get("origin") == "media" and not str(arguments.get("prompt") or "").strip():
                raise PlanError(i18n.pick_now(
                    f"Task {tid}: a generator requires arguments.prompt and structured reference paths. It cannot interpret planning instructions as tool parameters.",
                    f"任务 {tid}:生成工具必须有 arguments.prompt，参考文件路径也必须写在 arguments 中；不能让工具从任务说明猜参数。"))
            row = cooperation.localcmd.row(str(owner.get("engine") or "")) or {}
            if row.get("voice_engine") and not str(arguments.get("text") or "").strip():
                raise PlanError(i18n.pick_now(
                    f"Task {tid}: the voice tool requires exact narration in arguments.text. A chat member must prepare it first, or use synthesize_speech after reading the script.",
                    f"任务 {tid}:配音工具需要 arguments.text 中的逐字旁白。请先由对话成员备好，或读取脚本后调用 synthesize_speech。"))
            # ⚠️ The same suffix judgement as everywhere else, and it has to be: this check used a
            # 16-item list, so a generating member asked for `out/clip.mkv`, `frames/f001.tif` or
            # `voice/旁白.flac` passed planning and only failed at run time — after the round had
            # spent its turns. Unknown formats were exactly the ones not checked.
            asked_here = suffixes(str(rt.get("deliverable") or ""))
            if owner.get("origin") == "media":
                outputs = {"mp4", "mov", "webm"} if "generate_video" in allowed else {"png", "jpg", "jpeg", "webp"}
            else:
                row = cooperation.localcmd.row(str(owner.get("engine") or "")) or {}
                outputs = {pattern.rsplit(".", 1)[-1] for pattern in row.get("artifacts", [])}
            # ⚠️ Only judge when the member's own table says what it produces. Every row in
            # `localcmd.TOOLS` declares `artifacts`, but "declares nothing" must not silently become
            # "can produce nothing" — that would turn an incomplete table into a refusal.
            if outputs and (asked_here - outputs):
                raise PlanError(i18n.pick_now(
                    f"Task {tid}: {owner['name']} cannot produce the requested file type"
                    f" ({', '.join('.' + s for s in sorted(asked_here - outputs))}); "
                    f"split image, video and document work between capable members.",
                    f"任务 {tid}:{owner['name']} 不能交付这种文件"
                    f"({', '.join('.' + s for s in sorted(asked_here - outputs))});"
                    f"请按图片、视频、文档能力拆分负责人。"))
        tasks.append(PlanTask(
            id=tid, owner=owner["name"], owner_id=owner["id"], title=str(rt.get("title") or instruction[:20]).strip()[:40],
            instruction=instruction, needs=[str(n).strip() for n in _as_list(rt.get("needs"))],
            strengths=strengths, tools=tools, deliverable=str(rt.get("deliverable") or "").strip(), arguments=arguments,
            status="skipped" if preflight_error else "pending", error=preflight_error,
        ))
    if not tasks:
        # Nothing survived, so there is no plan to run: raise the first reason and let the caller's
        # one-shot repair ask for it again.
        raise PlanError(first_reason or i18n.pick_now("no usable task in the plan", "计划里没有一个可用的任务"))
    # The dependency check counts every id the model wrote, **including the ones that were dropped**.
    # A task whose upstream was dropped cannot run — but that is not an unknown dependency, it is a
    # known one that will not be delivered, and `_execute_plan` already marks such a task `skipped`
    # with the reason and puts it on the board. Rejecting the whole plan here instead would turn one
    # missing field into "nothing at all runs", which is the failure this whole function is being
    # changed to stop.
    ids = {t.id for t in tasks} | {str(rt.get("id") or "").strip() for rt in raw_tasks
                                   if isinstance(rt, dict) and str(rt.get("id") or "").strip()}
    for t in tasks:
        if t.id in t.needs or any(n not in ids for n in t.needs):
            raise PlanError(i18n.pick_now(f"Task {t.id} has an unknown or self dependency: {t.needs}",
                                          f"任务 {t.id} 存在未知依赖或自我依赖:{t.needs}"))
        t.needs = list(dict.fromkeys(t.needs))
    plan = Plan(goal=str(obj.get("goal") or "").strip()[:200], conventions=str(obj.get("conventions") or "").strip()[:800],
                tasks=_toposort(tasks, omitted=ids - {t.id for t in tasks}), dropped=dropped)
    return plan


def _toposort(tasks: list[PlanTask], omitted: set[str] | None = None) -> list[PlanTask]:
    """Stable topological sort: keeps the order given by the model when there is no dependency
conflict. Raises PlanError on a cycle."""
    done: list[PlanTask] = []
    done_ids: set[str] = set(omitted or ())
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
            "This is planning only. You may inspect existing materials, but must not write files, generate media or execute tasks here. Output the plan and stop; the task board will execute it once. Even a single production step needs a one-task plan.\n"
            "To split the work: explain your thinking to the user in one or two sentences, then output the plan (strict JSON inside a <plan> tag):\n"
            "<plan>{\"goal\": \"the overall goal in one sentence\", \"conventions\": \"the wording, terminology, format, audience and length the whole team has to agree on (leave empty if there are none)\", "
            "\"tasks\": [{\"id\": \"t1\", \"owner\": \"member name\", \"title\": \"task name\", \"instruction\": \"exactly what to do and how far to take it\", "
            "\"needs\": [], \"strengths\": [\"writing\"], \"tools\": [\"library_search\"], \"deliverable\": \"the shape of the deliverable\"}]}</plan>\n"
            "How to split it:\n"
            "1. Assign strictly by the strengths, skills and available tools listed under \"Members and their parts\" above; never hand a task to someone clearly not suited to it. "
            "A **generating member** is the one to watch: it makes pictures *or* video with its own generator and nothing else (the roster says which) — it cannot write, research, assemble, or call other tools. "
            "When the task needs the other kind of output, give it to somebody else (or use a built-in tool such as `generate_image`). "
            "⚠️ A **mechanism or schematic animation** — blood flowing along a vessel, a sac bulging out, a coil "
            "filling, a catheter advancing, contrast opacifying — belongs to `make_animation` **on a chat member**, "
            "not to a video generator: it draws those frames on this machine in seconds, at the exact frame size "
            "asked for, and it does not invent anatomy. A **diffusion** video takes minutes to tens of minutes per "
            "clip and may not honour the aspect ratio at all, so reserve it for the few shots that genuinely need "
            "photographic motion. "
            "For a workflow that requires a **fetchable reference image**, \"still frames → video\" is *two* tasks: the frames go to whoever can produce them, and the video task builds on those;\n"
            "2. needs contains task IDs from this plan only, never file paths or member names. Put existing input paths in instruction or arguments instead. Independent tasks have no dependencies; failed prerequisites block downstream tasks; no dependency loops. A dependency means the output is required, not merely that another task should run first. Separate narration segments from the same approved script are independent; missing status notes must not block unrelated production;\n"
            "3. The split has to cover everything the user asked for, with nothing overlapping and nothing missed; you own the final consolidation, so do not add a separate consolidation task. This board has success dependencies only: do not add standby tasks that say 'only if the primary fails', or unsupported condition/when fields. Assign a backup after an observed failure, not in advance;\n"
            f"4. Use no more than {max_tasks} tasks; owner must be the name of a group member (without the @); tools may only list tool names you can actually see;\n"
            "5. conventions is what stops the members from talking past each other: anything more than one of them will touch (names, numbers, tone, format, timeline) belongs here.\n"
            "6. **deliverable must be a file that ends up in the workspace, with its name** — e.g. "
            "\"交付/科普脚本.docx\" — and say which tool writes it (`write_document` for a document, a "
            "deck, a workbook or a text file; `make_figure`/`generate_image` for a picture; "
            "`assemble_video` for a film). \"a script\" is not a deliverable: a task whose deliverable "
            "names a file is checked against the workspace, and a task with no file cannot be handed to "
            "anyone or published. A task that genuinely produces no file (a review, a decision, a "
            "round of questions) says so in words. "
            "⚠️ `write_document` writes .docx/.pptx/.xlsx/.md and nothing else: if the file has to be "
            "another format (.html, .csv, .svg, .json), only use it when a tool that can write it is "
            "listed under \"Executable capabilities\" (`run_code`, when this machine allows it); "
            "otherwise pick a format that `write_document` can write. "
            "⚠️ Before you describe what an earlier round already finished, check the workspace "
            "(`list_workspace_files`): a member saying 「已落盘」 in the chat is **not** evidence — only "
            "a file that is actually there is. Plan on top of what exists, and re-assign what does not."
        ), (
            "\n"
            "本轮只做规划：可以读取已有材料，不得写文件、生成媒体或提前执行任务。输出计划后停止，任务板随后只执行一次。即使只有一个制作步骤，也要给出单任务计划。\n"
            "需要分工时:先用一两句话向用户说明你的思路,然后输出计划(严格 JSON,放在 <plan> 标签里):\n"
            '<plan>{"goal": "一句话总目标", "conventions": "全组必须统一的口径、术语、格式、受众、篇幅等(没有就留空)", '
            '"tasks": [{"id": "t1", "owner": "成员名", "title": "任务名", "instruction": "具体做什么、做到什么程度", '
            '"needs": [], "strengths": ["writing"], "tools": ["library_search"], "deliverable": "交付物形式"}]}</plan>\n'
            "分工原则:\n"
            "1. 严格按上面「群成员与分工」里的强项、技能和可用工具来分配;不要把任务派给明显不擅长的人。"
            "**要特别当心「生成成员」**:它只会用它自己那一个生成器做「图片」或「视频」(名册里写明是哪一种),"
            "不写字、不查资料、不装配,也不能调用别的工具。需要另一种产物时,请派给别人(或用内置工具,例如 `generate_image`)。"
            "⚠️ **机制/示意动画** —— 血流沿血管流动、瘤体鼓出、弹簧圈填塞、微导管推进、造影剂显影 —— 应当交给**对话成员**"
            "用 `make_animation` 做,而不是交给视频生成器:它在本机**逐帧画**、几秒完成、画幅就是你要求的那个,也不会编造解剖。"
            "**扩散式**视频每段要几分钟到几十分钟,而且**可能根本不理你要的画幅**,只留给确实需要实拍质感的那几个镜头。"
            "对于明确要求**能取到的参考图**的图生视频工作流,「静帧 → 图生视频」是**两个**任务:静帧交给会出图的人,视频任务承接它;\n"
            "2. needs 只能填写本计划中的任务 ID，不能填文件路径或成员名字；已有素材路径写进 instruction 或 arguments。下游会拿到上游的完整成果；独立任务不填依赖，不要循环依赖。依赖表示必须使用上游产物，不能只为安排先后而连线；同一已审定脚本的不同配音段相互独立，状态文档缺失不应阻断其他独立制作；\n"
            "3. 分工要覆盖用户的全部要求,不重叠、不遗漏;最终汇总由你负责,不要单列汇总任务。当前任务板只有成功依赖，不支持条件分支，禁止预排「仅主路失败时启用」的备用制作节点或condition/when字段；观察到真实失败后再派备路；\n"
            f"4. 任务数不超过 {max_tasks} 个;owner 必须是群成员名字(不带 @);tools 只能填你确实看到的工具名;\n"
            "5. conventions 是防止「各说各话」的关键:凡是多人都会碰到的口径(名称、数字、语气、格式、时间线)都写在这里;\n"
            "6. **deliverable 必须是落在工作目录里的文件,并写出文件名** —— 例如「交付/科普脚本.docx」—— "
            "并写明用哪个工具写(`write_document` 写文档/幻灯片/表格/文本,`make_figure`/`generate_image` 出图,"
            "`assemble_video` 出片)。写「一份脚本」不算交付物:凡 deliverable 里写了文件名,程序会去工作目录里核对;"
            "**没有文件的任务,既不能交给下游,也不能交付给用户**。确实不产出文件的任务(评审、决策、提问)用文字写明。"
            "⚠️ `write_document` **只能写 .docx/.pptx/.xlsx/.md**,别的格式(.html/.csv/.svg/.json 等)写不了 —— "
            "只有「实际可执行能力」里确实列出了 `run_code` 时才用它写,否则请换成 write_document 能写的格式。"
            "⚠️ 描述「上一轮已经完成什么」之前**先看工作目录**(`list_workspace_files`):成员在聊天里说「已落盘」"
            "**不算证据**,只有真的在盘上的文件才算。基于真实存在的文件往下排,不存在的那部分重新派人做。"
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
    if task.arguments:
        parts.append(i18n.pick_now("Task parameters: ", "任务参数:")
                     + json.dumps(task.arguments, ensure_ascii=False))
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
        + (i18n.pick_now(f"5. These tasks did not finish: {_unfinished(failed)} — say clearly what is missing and how to make up for it;\n", f"5. 这些任务没有完成:{_unfinished(failed)},请明确指出缺了什么并给出补救办法;\n") if failed else "")
        + i18n.pick_now(
            "Report the actual completion state and any concrete blocker. Do not manufacture a confirmation step for work the user already authorized, routine reversible corrections, team selection or tool choice. Ask only for genuinely missing input or an action outside the authorized scope. Do not lower acceptance criteria to declare success, and do not claim a suggested next step has already run. Do not @mention anyone.",
            "报告实际完成状态及具体阻塞。用户已经授权的工作、常规可逆修正、成员挑选或工具选择，不要再制造确认环节。仅在确实缺输入或超出授权范围时提问。不降低验收条件来宣称完成，不把下一步建议说成已经执行。不要再@任何人。")
    )


def _unfinished(tasks: list["PlanTask"], limit: int = 4) -> str:
    """The unfinished tasks, each with **why** it is unfinished.

    The name alone was what this line used to carry, and it made the consolidating host guess: a round
    that ended with four failed tasks produced an answer that named them and could say nothing about
    any of them, while each task's own reason was sitting on the board one field away. The reason is
    the action ("the folder it renders from is empty", "every tool call failed") — exactly what the
    next step is written from, and exactly what the user has to be told to fix anything at all.
    """
    shown = []
    for t in tasks[:limit]:
        why = " ".join(str(t.error or "").split())
        shown.append(f"{t.id} {t.title}" + (f" ({why[:160]})" if why else ""))
    more = len(tasks) - len(shown)
    rest = i18n.pick_now(f" and {more} more", f" 等另外 {more} 个") if more > 0 else ""
    return i18n.pick_now(", ", "、").join(shown) + rest


def summarize(plan: Plan) -> str:
    """Body of the task board message (plain text, for places that do not render cards)."""
    lines = [i18n.pick_now(f"Plan: {plan.goal}", f"分工:{plan.goal}") if plan.goal else i18n.pick_now("Plan", "分工")]
    for t in plan.tasks:
        lines.append(f"{t.id} {t.owner}:{t.title} [{t.status}]")
    if plan.integration:
        lines.append(i18n.pick_now("Consolidate", "整合") + f" [{plan.integration['status']}] "
                     + plan.integration.get("error", ""))
    for d in plan.dropped:
        # Said out loud on the board: a dropped task is part of the user's request that nobody was
        # given, and a board that quietly shows fewer rows is how that goes unnoticed.
        lines.append(i18n.pick_now(f"(dropped, could not be assigned) {d}",
                                   f"(已丢弃,无法派单){d}"))
    return "\n".join(lines)


def said_headline(row: dict | None, limit: int = 72) -> dict | None:
    """**没有任务板**时那一行的替身:这个群最近做了什么。

    形状与 `headline` 完全一样(前端那一行只认这一种形状),唯一的区别是 **`board` 是空串** ——
    前端据此知道「这不是任务板」,于是不显示完成数、也不显示任务状态词,只显示这句摘要和时间。

    ⚠️ 取**第一句**:一条发言可以是几段话,而那一行放不下;标点之后通常是细节。
    """
    if not row:
        return None
    text = " ".join(str(row.get("text") or "").split())
    if not text:
        return None
    ends = [i for i in (text.find(p) for p in "。！？!?\n") if i > 0]
    if ends:
        text = text[:min(ends) + 1]
    return {
        "id": "", "title": text[:limit], "owner": str(row.get("owner") or ""),
        "status": "", "board": "", "goal": "", "at": float(row.get("at") or 0.0),
        "done": 0, "failed": 0, "open": 0, "total": 0,
    }


def headline(row: dict | None) -> dict | None:
    """The one task a project should be summarised by, from its newest task board.

    "Newest or most important" has an order, and it is: **what is running now**, then **what is next
    unfinished**, then **the last thing that was finished**. The first is what the project is doing
    at this second; the second is what it is waiting to do; the third is what it just did. Showing
    the first task of the board instead would put a step that finished an hour ago next to a project
    that is working right now, which is the one thing this line exists to tell apart.

    Counts travel with it, so the sidebar can say "3/8" without reading the whole board.
    """
    if not row:
        return None
    meta = row.get("meta") or {}
    tasks = [t for t in (meta.get("tasks") or []) if isinstance(t, dict)]
    if not tasks:
        return None
    integration = meta.get("integration") or {}
    if integration:
        tasks.append({"id": "final", "title": i18n.pick_now("Consolidate", "最终整合"),
                      "owner": "", **integration})
    done = [t for t in tasks if t.get("status") == "done"]
    # A task still marked `running` is only "doing it now" while the round itself is live. The
    # board's own status is what says whether anyone is still working: a round that ended, failed or
    # was stopped leaves its running task behind, and picking it first meant the line advertised "in
    # progress" for ever and never moved on to what is actually next.
    live = str(meta.get("status") or "") in ("running", "integrating")
    counts = {"done": len(done),
              "failed": sum(1 for t in tasks if t.get("status") in ("failed", "stopped", "skipped")),
              # `pending` and `running` are the tasks that still have work in them. They were in
              # neither count, so a board of eight untouched tasks reported 0/8 done and 0 failed
              # and the sidebar showed no sign that anything was outstanding.
              "open": sum(1 for t in tasks if t.get("status") in ("pending", "running")),
              "total": len(tasks)}
    picked = (next((t for t in tasks if t.get("status") == "running"), None) if live else None) \
        or next((t for t in tasks if t.get("status") == "pending"), None) \
        or (done[-1] if done else None) \
        or tasks[-1]
    if integration.get("status") in ("failed", "stopped"):
        picked = tasks[-1]
    return {
        "id": str(picked.get("id") or ""),
        "title": str(picked.get("title") or ""),
        "owner": str(picked.get("owner") or ""),
        "status": str(picked.get("status") or ""),
        "board": str(meta.get("status") or ""),
        "goal": str(meta.get("goal") or ""),
        "at": float(row.get("at") or 0.0),
        **counts,
    }
