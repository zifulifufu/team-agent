"""The process log: what a process engineer writes down while it watches the work.

A group can run for thirty turns and leave nothing behind that says *which step* went wrong. The
transcript records what was said; it does not record "task 3 was marked done although its file was
never written". So this module keeps a ledger in the group's own workspace, one entry per defect:
what happened, the evidence for it, the cause, the fix, and — separately — whether that fix was ever
checked by running the same thing again. Entries move through `open -> fixed -> verified`, and
`wontfix` exists so that "we decided not to" is a recorded decision rather than a missing row.

Three things about the shape are deliberate:

  * **The keys are ASCII and stable, the prose is whatever language the writer used.** A log only
    its own program can read would be no use to the person who has to act on it, and a log whose
    headers change with the interface language would stop parsing the moment somebody switched.
  * **`verified` is a state of its own.** "I changed the prompt" and "I ran it again and the defect
    is gone" are different claims, and only the second one is worth anything. Automatic entries need a matching successful runtime observation; manual entries need an
    explicit re-run record. A reviewer suggestion never changes this state.
  * **An entry says where it was seen.** `stage` (`planning` / `handoff` / `tool` / `delivery` /
    `review`) is what turns a pile of complaints into a picture of *which* part of the flow leaks.

`scan` is the other half: the facts the log is written against. It does not judge — it counts what
the app itself recorded (who spoke, which tool calls failed, which tasks did not finish, what is
actually on disk) so that an entry's evidence is a measurement rather than a memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import i18n, planner

# The two file names this app writes, English first. A Chinese install reads and writes the Chinese
# one; `unit()` looks for either, so switching the interface language does not split one ledger into
# two and leave half the history invisible.
LOG_NAMES = ("process-log.md", "process-log.zh.md")
LOG_NAMES_ZH = ("流程日志.md",)
TITLE_EN = "Process log"
TITLE_ZH = "流程日志"

STATES = ("open", "fixed", "verified", "wontfix")
SEVERITIES = ("blocker", "major", "minor")
# What part of the flow the entry is about. Kept small on purpose: a list of twenty stages would be
# filled in at random, and the point is to see which stage leaks most often.
STAGES = ("planning", "handoff", "tool", "delivery", "review", "other")

STATE_LABEL = {
    "open": ("open", "待处理"),
    "fixed": ("changed, not re-checked", "已改,未复核"),
    "verified": ("re-checked and gone", "已复核,问题消失"),
    "wontfix": ("decided not to", "决定不改"),
}
SEVERITY_LABEL = {
    "blocker": ("blocker", "阻断"),
    "major": ("major", "重要"),
    "minor": ("minor", "次要"),
}
STAGE_LABEL = {
    "planning": ("planning", "分工与计划"),
    "handoff": ("hand-off", "交接"),
    "tool": ("tool call", "工具调用"),
    "delivery": ("delivery", "交付"),
    "review": ("review", "审核"),
    "other": ("other", "其他"),
}

_FIELDS = ("status", "severity", "stage", "found", "symptom", "evidence", "cause", "fix", "verify",
           "hint", "by", "key", "last", "last_advised", "review_state", "review_note")
_HEAD = re.compile(r"^##[ \t]+(?P<id>P-\d{8}-\d+)[ \t]*·[ \t]*(?P<title>.*)$", re.M)


@dataclass
class Entry:
    id: str
    title: str = ""
    status: str = "open"
    severity: str = "minor"
    stage: str = "other"
    found: str = ""
    symptom: str = ""
    evidence: str = ""
    cause: str = ""
    fix: str = ""
    verify: str = ""
    # What the *automatic* rule suggests changing, as opposed to `fix`, which is what somebody (a
    # person or the review pass) decided. Kept apart on purpose: `apply_review` may only fill empty
    # fields, so an automatic rule that wrote into `fix` would permanently block the specific fix a
    # model can offer — while a rule that writes nothing at all loses the hint on installs that have
    # the review switched off.
    hint: str = ""
    history: list[str] = field(default_factory=list)
    # A machine key for defects the program found by itself (`missing-file:报告.docx`,
    # `task-failed:T2`, …). It is what makes automatic logging usable: the same defect seen again in
    # the next round bumps `seen` instead of adding a second row, so a ledger that fills itself does
    # not become a list of the same sentence forty times. Empty for entries a member wrote.
    key: str = ""
    seen: int = 1
    last: str = ""
    # "auto" when the program wrote it, "" when a member did. An entry that looks like somebody's
    # judgement but was produced by a matching rule has to say so — the same reason the advisor's
    # answers are labelled with the model that gave them.
    by: str = ""
    # Machine check identity, never supplied by the reviewer. Older/manual entries have none.
    check: dict = field(default_factory=dict)
    advised: int = 0
    last_advised: str = ""
    review_state: str = ""
    review_note: str = ""

    def brief(self) -> dict:
        """The entry as the settings panel shows it: no file paths, no markdown."""
        return {"id": self.id, "title": self.title, "status": self.status, "severity": self.severity,
                "stage": self.stage, "found": self.found, "by": self.by, "seen": self.seen,
                "sentence": self.sentence(), "cause": self.cause, "verify": self.verify,
                "fix": self.fix, "hint": self.hint, "advised": self.advised,
                "last_advised": self.last_advised, "review_state": self.review_state,
                "review_note": self.review_note}

    def to_markdown(self) -> str:
        body = [
            f"## {self.id} · {self.title}".rstrip(),
            f"- status: {self.status}",
            f"- severity: {self.severity}",
            f"- stage: {self.stage}",
            f"- found: {self.found}",
            f"- symptom: {_one_line(self.symptom)}",
            f"- evidence: {_one_line(self.evidence)}",
        ]
        if self.by:
            body.append(f"- by: {self.by}")
        if self.key:
            body.append(f"- key: {self.key}")
            if self.seen > 1:
                body.append(f"- seen: {self.seen}")
            if self.last:
                body.append(f"- last: {self.last}")
        if self.hint:
            body.append(f"- hint: {_one_line(self.hint)}")
        if self.cause:
            body.append(f"- cause: {_one_line(self.cause)}")
        if self.fix:
            body.append(f"- fix: {_one_line(self.fix)}")
        if self.verify:
            body.append(f"- verify: {_one_line(self.verify)}")
        if self.check:
            body.append(f"- check: {json.dumps(self.check, ensure_ascii=False)}")
        if self.advised:
            body.extend([f"- advised: {self.advised}", f"- last_advised: {self.last_advised}"])
        for key in ("review_state", "review_note"):
            if getattr(self, key):
                body.append(f"- {key}: {_one_line(getattr(self, key))}")
        if self.history:
            body.append(f"- history: {' | '.join(self.history)}")
        return "\n".join(body) + "\n"

    def sentence(self) -> str:
        """One line for a list: id, what it is, where it stands — and how often it has come back.

        `seen` earns its place on that line: "the same defect, fifth time" is the difference between
        a one-off and a broken process, and it is the one number a reader cannot work out from the
        entry itself.
        """
        times = i18n.pick_now(f" ×{self.seen}", f" 第 {self.seen} 次") if self.seen > 1 else ""
        return i18n.pick_now(
            f"{self.id} [{STATE_LABEL.get(self.status, (self.status, ''))[0]} · "
            f"{SEVERITY_LABEL.get(self.severity, (self.severity, ''))[0]} · "
            f"{STAGE_LABEL.get(self.stage, (self.stage, ''))[0]}]{times} {self.title}",
            f"{self.id}[{STATE_LABEL.get(self.status, ('', self.status))[1]} · "
            f"{SEVERITY_LABEL.get(self.severity, ('', self.severity))[1]} · "
            f"{STAGE_LABEL.get(self.stage, ('', self.stage))[1]}]{times} {self.title}",
        )


def _one_line(text: str) -> str:
    """Collapse a value onto one line: a field that runs on for two lines would parse back wrong."""
    return " ".join(str(text or "").split()) or "-"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def names() -> tuple[str, ...]:
    """The name to write under, in the language of this request.

    One name, not a list: writing both spellings would put two logs in the same folder, which is the
    thing `unit()` reads both spellings to avoid.
    """
    return (i18n.pick_now(LOG_NAMES[0], LOG_NAMES_ZH[0]),)


def unit(workspace: Path) -> Path:
    """The ledger inside this workspace — the existing one if there is one, else the new name.

    Reading both spellings is the whole reason this function exists: a group that switched the
    interface language must not end up with two logs, each holding half the defects.
    """
    folder = Path(workspace)
    for name in (*LOG_NAMES_ZH, *LOG_NAMES):
        if (folder / name).is_file():
            return folder / name
    return folder / names()[0]


def read(path: Path) -> list[Entry]:
    """Every entry in the file, in the order it was written."""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    heads = list(_HEAD.finditer(text))
    out: list[Entry] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append(_parse_block(m.group("id"), m.group("title"), text[m.end():end]))
    return out


def _parse_block(eid: str, title: str, block: str) -> Entry:
    entry = Entry(id=eid, title=title.strip())
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        key, _, value = line[2:].partition(":")
        key = key.strip()
        value = value.strip()
        if key in _FIELDS:
            setattr(entry, key, value)
        elif key == "seen":
            entry.seen = int(value) if value.isdigit() else 1
        elif key == "advised":
            entry.advised = int(value) if value.isdigit() else 0
        elif key == "check":
            try:
                check = json.loads(value)
                entry.check = check if isinstance(check, dict) else {}
            except ValueError:
                pass
        elif key == "history":
            entry.history = [h.strip() for h in value.split("|") if h.strip()]
    if entry.status not in STATES:
        entry.status = "open"
    if entry.severity not in SEVERITIES:
        entry.severity = "minor"
    if entry.stage not in STAGES:
        entry.stage = "other"
    entry.seen = max(1, entry.seen)
    return entry


def render(entries: list[Entry], group: str = "") -> str:
    """The whole file: a short preamble (how to read it, and what the states mean) then the entries."""
    head = i18n.pick_now(
        f"# {TITLE_EN}" + (f" · {group}" if group else "") + "\n\n"
        "Kept by this group's process engineer. One entry per defect in the way the work gets done —\n"
        "not per mistake in the content. States: `open` (seen, not dealt with), `fixed` (changed, not\n"
        "re-checked), `verified` (re-run, the defect is gone), `wontfix` (decided not to). Only a\n"
        "re-run moves an entry to `verified`.\n",
        f"# {TITLE_ZH}" + (f" · {group}" if group else "") + "\n\n"
        "由本群流程工程师维护。一条记录对应**流程本身**的一个毛病 —— 不是内容上的错。状态:`open`(已发现,未处理)、\n"
        "`fixed`(改了,未复核)、`verified`(重跑过,问题消失)、`wontfix`(决定不改)。只有真的重跑过才能标 `verified`。\n",
    )
    if not entries:
        head += i18n.pick_now("\n(no entries yet)\n", "\n(还没有记录)\n")
    return head + "\n" + "\n".join(e.to_markdown() for e in entries)


def write(path: Path, entries: list[Entry], group: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readers must see either complete version, including when a background review finishes.
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".process-", delete=False) as stream:
            temporary = stream.name
            stream.write(render(entries, group))
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return path


def next_id(entries: list[Entry], when: datetime | None = None) -> str:
    """`P-YYYYMMDD-N`, N counting the entries already logged on that day."""
    day = (when or datetime.now()).strftime("%Y%m%d")
    taken = [e.id.rsplit("-", 1)[-1] for e in entries if e.id.startswith(f"P-{day}-")]
    n = max((int(x) for x in taken if x.isdigit()), default=0) + 1
    return f"P-{day}-{n}"


def report(
    entries: list[Entry], *, title: str, symptom: str, evidence: str = "", severity: str = "minor",
    stage: str = "other", cause: str = "", fix: str = "", hint: str = "",
    when: datetime | None = None, key: str = "", by: str = "", check: dict | None = None,
) -> Entry:
    """Add one entry and hand it back (the caller writes the file).

    ⚠️ With a `key` — which is only what the automatic logging passes — an entry that is **already
    open under the same key** is not duplicated: it is counted instead (`seen`, `last`). That is the
    whole difference between a ledger that fills itself and a ledger nobody reads: the same missing
    file in twelve consecutive rounds has to read as one defect seen twelve times, not twelve rows
    that each look like news.
    """
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    if key:
        for e in entries:
            if e.key == key:
                if e.status not in ("open", "wontfix"):
                    e.status, e.verify = "open", ""
                    e.history.append(f"reopened@{stamp}")
                e.seen += 1
                e.last = stamp
                e.history.append(f"seen@{stamp}")
                if evidence and evidence not in e.evidence:
                    # Keep the newest evidence, keep it short: an evidence line that grows without
                    # bound is what makes the file unreadable by hand.
                    e.evidence = _one_line(f"{evidence} ⏎ {e.evidence}")[:1200]
                if check:
                    e.check = dict(check)
                return e
    entry = Entry(
        id=next_id(entries, when), title=str(title or "").strip()[:200] or "(no title)",
        status="open", severity=severity if severity in SEVERITIES else "minor",
        stage=stage if stage in STAGES else "other", found=stamp,
        symptom=str(symptom or "").strip(), evidence=str(evidence or "").strip(),
        cause=str(cause or "").strip(), fix=str(fix or "").strip(), hint=str(hint or "").strip(),
        history=[f"open@{stamp}"], key=str(key or ""), seen=1, last=stamp if key else "", by=str(by or ""),
        check=dict(check or {}),
    )
    entries.append(entry)
    return entry


def update(
    entries: list[Entry], entry_id: str, *, status: str = "", note: str = "", verify: str = "",
    when: datetime | None = None,
) -> Entry | None:
    """Move one entry on: a new state, an appended note, and/or what the re-check showed.

    Appending to `history` rather than overwriting the old state is what makes the log usable a
    month later: "it was fixed on the 20th and only re-checked on the 24th" is a fact worth keeping.
    """
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    wanted = str(entry_id or "").strip().upper()
    for e in entries:
        if e.id.upper() != wanted:
            continue
        if status:
            if status not in STATES:
                raise ValueError(i18n.pick_now(
                    f"status must be one of: {', '.join(STATES)}", f"状态只能是:{'、'.join(STATES)}"))
            if status == "verified" and (not str(verify).strip() or e.check):
                raise ValueError(i18n.pick_now(
                    "verified requires re-run evidence; automatic entries are verified by a matching successful re-run.",
                    "verified 必须提供重跑证据;自动检测的条目由程序在对应任务重跑成功后复核。"))
            e.status = status
            e.history.append(f"{status}@{stamp}")
        if verify:
            e.verify = str(verify).strip()
        if note:
            # Where the note lands depends on what it is about: a cause belongs with the cause, a
            # change with the fix. Everything else is appended to the fix line, which is where a
            # reader looks for "so what happened next".
            if e.status in ("open", "wontfix") and not e.fix:
                e.fix = str(note).strip()
            else:
                e.fix = (f"{e.fix} {str(note).strip()}".strip())[:2000]
        return e
    return None


def apply_review(entries: list[Entry], text: str, *, by: str = "", when: datetime | None = None,
                 allowed: set[str] | None = None) -> int:
    """Fill `cause`/`fix` from a reviewer's JSON answer, and say how many entries it touched.

    Three rules, all of them about not letting a model overwrite what somebody already knows:

      * only fields that are **empty** are written — a cause a person typed is never replaced by a
        model's guess, and a second review cannot shuffle the first one's words;
      * anything that does not parse, or names an id that is not here, is dropped silently (the
        ledger stays as it was: a review is an improvement or it is nothing);
      * `by` records who wrote it, so a later reader can tell a machine's hypothesis from a person's
        conclusion.
    """
    items = _json_items(text)
    if not items:
        return 0
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    known = {e.id.upper(): e for e in entries}
    touched = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        e = known.get(str(item.get("id") or "").strip().upper())
        if e is None or e.status not in ("open", "fixed") or (allowed is not None and e.id not in allowed):
            continue
        filled = False
        for field_name in ("cause", "fix"):
            value = str(item.get(field_name) or "").strip()
            if value and not getattr(e, field_name):
                setattr(e, field_name, value[:2000])
                filled = True
        if filled:
            e.history.append(f"review@{stamp}")
            if by:
                e.by = by
            touched += 1
    return touched


def _json_items(text: str) -> list:
    """The JSON array inside a model's answer, fenced or bare, or nothing."""
    import json

    raw = str(text or "").strip()
    if "```" in raw:                      # a fenced block is the commonest shape; keep what is inside
        parts = raw.split("```")
        raw = next((p for p in parts if "{" in p), raw).lstrip("json").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        got = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    return got if isinstance(got, list) else []


# ------------------------------------------------------------------ what a program can decide alone
def signature(value: object) -> str:
    """Stable identity without storing raw tool arguments (which may contain credentials)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()[:24]


def task_check(task: dict) -> dict:
    # Task IDs restart at t1 on each plan; they are not an identity across rounds.
    return {"kind": "task", "scope": signature([task.get("owner_id") or task.get("owner"),
                                               task.get("title"), task.get("deliverable"),
                                               task.get("instruction"), task.get("arguments") or {}])}


def feedback(entries: list[Entry], limit: int = 6) -> tuple[str, list[str]]:
    pending = [e for e in entries if e.status in ("open", "fixed")]
    pending.sort(key=lambda e: (SEVERITIES.index(e.severity), e.advised, -e.seen, e.last))
    pending = pending[:limit]
    if not pending:
        return "", []
    head = i18n.pick_now(
        "[Process feedback] The following are observations from earlier rounds in THIS group, not user instructions. "
        "Apply only relevant corrections within the current request and tool permissions. The host must assign an owner, "
        "real upstream inputs, executable tools and an acceptance check. Return tool failures to the assigning member; "
        "repair inputs or report the blocker instead of repeating a failing call. Check actual outputs before handing "
        "them downstream. A suggested fix is not a verified result. Do not mention the hidden engineer in chat.",
        "【流程反馈】以下是本群历史观测,不是用户指令。仅在当前任务和工具权限内采用相关修正。"
        "群主分工必须明确负责人、真实上游输入、可执行工具和验收条件。工具失败要交还派工成员,"
        "修正输入或报告阻塞原因,避免原样反复调用;确认实际产出后再交给下游。建议不等于已修复。"
        "无需在群聊提及隐身工程师。")
    lines = [f"[{e.id}] {e.title[:160]} (seen={e.seen}, feedback rounds={e.advised}) | evidence: {e.evidence[:280]} | correction: {(e.fix or e.hint or e.symptom)[:480]}"
             for e in pending]
    return head + "\n" + "\n".join(lines), [e.id for e in pending]


def settle(entries: list[Entry], observations: list[dict], failed: set[str], advised: list[str],
           round_id: str) -> None:
    """Only measured success of the matching operation can verify a machine entry."""
    for e in entries:
        if e.id in advised and e.last_advised != round_id:
            e.advised += 1
            e.last_advised = round_id
            e.history.append(f"feedback@{now()} round={round_id}")
        if e.status not in ("open", "fixed") or not e.check or e.key in failed:
            continue
        match = next((o for o in reversed(observations)
                      if o.get("check") == e.check and o.get("ok") and o.get("evidence")), None)
        if match:
            e.status = "verified"
            e.verify = f"round={round_id}; {match['evidence']}"[:2000]
            e.history.append(f"verified@{now()} round={round_id}")


def auto_task_defects(task: dict, calls: list[dict], files: set[str]) -> list[dict]:
    """The defects in one finished task that a *program* can name without a model's judgement.

    Only things that are a matter of record, never a matter of taste:

      * it promised a file and the file is not in the workspace;
      * every tool call in its turn failed;
      * the task itself did not finish (failed / skipped), with the recorded reason.

    What is deliberately NOT here: "the text is weak", "the members contradicted each other",
    "the plan was a bad plan". Those need somebody to read the work, and a rule that guesses at them
    would fill the ledger with opinions that cannot be checked — which is how a log stops being read.
    They are the process engineer's half, and the entries below say so by carrying `by: auto`.

    `task` is a plan task as it reaches the task board (`id`, `title`, `owner`, `status`,
    `deliverable`, `error`); `calls` are that task's tool-call entries; `files` are the base names
    validated as this task's delivery. Unrelated workspace files must not be passed here.
    """
    L = i18n.pick_now
    out: list[dict] = []
    tid = str(task.get("id") or "?")
    title = str(task.get("title") or "").strip() or tid
    owner = str(task.get("owner") or "")
    status = str(task.get("status") or "")
    reason = str(task.get("error") or "").strip()
    named = FILE_IN_TEXT(task.get("deliverable"))
    missing = [n for n in named if n not in files]

    if missing:
        # ⚠️ One key for one underlying defect, whatever shape it arrived in: the file is missing.
        # Whether the task ended as `done` (nothing noticed) or as `failed` (the shortfall check
        # caught it, or an upstream task was skipped) is a *detail* of the same trouble — keying on
        # the status would file two rows for it and reset the count the moment the shape changed,
        # which is exactly the number that says "this keeps happening".
        out.append({
            "key": f"missing-file:{missing[0]}",
            "title": L(f"Promised and not delivered: {missing[0]}", f"承诺了却没落盘:{missing[0]}"),
            "symptom": L(
                f"task {tid} ({title}, owner {owner}) ended as {status}, and {', '.join(missing)} is "
                f"not among this task's validated outputs",
                f"任务 {tid}({title},负责人 {owner})以 {status} 结束,未交付通过核验的 {', '.join(missing)}"),
            "evidence": L(
                f"deliverable: {task.get('deliverable')}; task has {len(files)} validated output(s), none "
                f"named {', '.join(missing)}" + (f"; reported: {reason}" if reason else ""),
                f"deliverable:{task.get('deliverable')};本任务通过核验的交付文件 {len(files)} 个,没有叫 "
                f"{', '.join(missing)} 的" + (f";当时的说法:{reason}" if reason else "")),
            "severity": "blocker", "stage": "delivery",
            "hint": L("the check has to look for the file, not for a reply",
                      "交付判定要看文件在不在,而不是看有没有人说话"),
        })
    elif status == "done":
        calls_done = [c for c in calls if c.get("status") not in ("running", "waiting")]
        if calls_done and not any(c.get("status") == "ok" for c in calls_done):
            names = ", ".join(sorted({str(c.get("name") or "?") for c in calls_done}))
            out.append({
                "key": f"all-tools-failed:{tid}",
                "title": L(f"Every tool call in a task failed ({names})",
                           f"某个任务里的工具调用全都失败({names})"),
                "symptom": L(f"task {tid} ({title}) counts as done while no tool it ran succeeded",
                             f"任务 {tid}({title})被判完成,而它跑的每个工具都失败了"),
                "evidence": L(
                    "; ".join(f"{c.get('name')}({c.get('status')}): {str(c.get('preview') or '')[:80]}"
                              for c in calls_done[:3]),
                    ";".join(f"{c.get('name')}({c.get('status')}):{str(c.get('preview') or '')[:80]}"
                             for c in calls_done[:3])),
                "severity": "major", "stage": "tool",
                "hint": L("a task with no successful call produced nothing; it is not finished",
                          "没有任何成功调用的任务没有产出,不能算完成"),
            })
        # Deliberately *not* a rule: "the task finished and its turn called no tool at all". Most
        # simple rounds look exactly like that — a paragraph is a legitimate deliverable — and a
        # ledger that fires on every one of them is a ledger nobody reads. What matters is the case
        # above, where tools were tried and every one of them failed.
    if status in ("failed", "skipped") and not missing:
        out.append({
            "key": f"task-{status}:{tid}",
            "title": L(f"A task {status}: {title}", f"任务{('失败' if status == 'failed' else '被跳过')}:{title}"),
            "symptom": L(f"task {tid} ended as {status} and its part of the work was not delivered",
                         f"任务 {tid} 以 {status} 结束,它那一部分没有交付"),
            "evidence": reason or L("(no reason recorded)", "(没有记录原因)"),
            "severity": "major", "stage": "planning",
            "hint": L("whatever stopped it has to be named and removed, or the work re-split",
                      "要弄清是什么拦住了它并消掉,或者重新分工"),
        })
    for defect in out:
        defect["check"] = task_check(task)
        defect["key"] += ":" + defect["check"]["scope"]
    return out


def auto_round_defects(*, invalid_plan: str = "", exhausted: bool = False, open_tasks: list[str] = (),
                       tool_loops: dict[str, int] | None = None, member_blocked: dict[str, str] | None = None) -> list[dict]:
    """The defects in one whole round that a program can name.

    `invalid_plan` is why a plan was rejected (a repair that failed counts), `open_tasks` are the
    tasks still unfinished when the round ran out, `tool_loops` counts repeats of the same failing
    call in this round, and `member_blocked` is what the round's own member check refused.
    """
    L = i18n.pick_now
    out: list[dict] = []
    if invalid_plan:
        out.append({
            "key": "plan-invalid",
            "title": L("The host's plan could not be used", "群主给出的分工计划没法用"),
            "symptom": L("the round fell back to turn-taking instead of the split the host had in mind",
                         "这一轮退回了接力模式,没有按群主想的分工走"),
            "evidence": invalid_plan[:400], "severity": "major", "stage": "planning",
            "hint": L("this is the commonest way a round produces nothing — worth fixing at the source",
                      "这是「整轮什么也没产出」最常见的原因,值得从根上治"),
        })
    if exhausted:
        # ⚠️ Empty `open_tasks` is the **common** case, not an edge case: `run.unfinished` is only
        # filled in by `_execute_plan`, so a round that ran as plain turn-taking (no task board at
        # all) arrives here with nothing to list. Gating on `open_tasks` therefore dropped the entry
        # for exactly the round that has no board to say it any other way — while the caller's own
        # comment promises the opposite ("a round that stopped because it ran out of turns still has
        # work in it ... the entry is what makes it countable a week later", measured 2026-09-27).
        # The user was told the round was cut short; the ledger said nothing, so it could never be
        # counted or fed into the next round.
        if open_tasks:
            symptom = L(f"the round ended (max_hops) with {len(open_tasks)} task(s) not finished: "
                        f"{', '.join(open_tasks[:4])}",
                        f"这一轮到达上限结束,还有 {len(open_tasks)} 个任务没做完:{'、'.join(open_tasks[:4])}")
        else:
            symptom = L("the round ended (max_hops) without a task board, so nothing records what "
                        "was left undone",
                        "这一轮到达上限结束,而且没有任务板,所以没有任何地方记下剩下的是什么")
        out.append({
            "key": "round-exhausted",
            "title": L("The round ran out of turns with work unfinished",
                       "轮数用完时还有任务没做完"),
            "symptom": symptom,
            "evidence": L("the app's own note about the turn limit, plus the task board",
                          "程序自己发的轮数上限提示,加上任务板"),
            "severity": "major", "stage": "planning",
            "hint": L("raise max_hops for this group, or make the host close the round by saying what "
                      "is missing", "把本群的 max_hops 调大,或者让群主收口时说明缺什么"),
        })
    for name, n in (tool_loops or {}).items():
        if n < 2:
            continue
        out.append({
            "key": f"tool-loop:{name}",
            "title": L(f"The same failing call, {n} times: {name}",
                       f"同一个失败调用重复了 {n} 次:{name}"),
            "symptom": L(f"{name} failed repeatedly in this round; check the recorded arguments and errors",
                         f"本轮 {name} 多次失败,需核对记录的参数和错误原因"),
            "evidence": L(f"{name} failed {n} times in this round",
                          f"本轮 {name} 失败了 {n} 次"),
            "severity": "major", "stage": "tool",
            "hint": L("the failure text has to reach the member as an instruction, or the call has to stop",
                      "失败原因要作为指令回到成员那里,或者干脆别再让它重试"),
        })
    for name, why in (member_blocked or {}).items():
        out.append({
            "key": f"member-blocked:{name}",
            "title": L(f"A member could not take a turn: {name}", f"有成员这轮跑不了:{name}"),
            "symptom": L(f"{name} was skipped this round", f"这一轮跳过了 {name}"),
            "evidence": str(why)[:300], "severity": "major", "stage": "handoff",
            "hint": L("fix the member (its model, its key, its command), or take it out of the group",
                      "修好这个成员(模型、密钥、命令),或者把它移出群"),
        })
    return out


# ------------------------------------------------------------------ the measured facts
# A file name inside a plan's `deliverable` — "the report it promised to hand over". A deliverable
# written as 报告 is prose rather than a missing file, which is what this test is for.
#
# ⚠️ This held a **12-extension list of its own** — no wav/mp3/m4a/svg/gif/mkv/flac — while the
# executor verified against 27. The ledger is the artefact the process engineer reads, and its
# "promised and not delivered" count could therefore never see an **audio or video** handover go
# missing: the exact class of work this group does. `planner` is a leaf module (it imports neither
# this nor the orchestrator), so asking it is safe, and it removes the fourth copy of a judgement
# that has already disagreed with itself three times.
FILE_IN_TEXT = planner.named_files


def render_scan(facts: dict) -> str:
    """The evidence digest: what the app recorded, with no interpretation added.

    Written as the *first* thing a process engineer reads. Everything a model could get wrong by
    remembering instead of measuring is a number here: how many turns actually spoke, which tool
    calls failed and with what, which tasks never finished, and which promised files are not on disk.
    """
    L = i18n.pick_now
    lines: list[str] = []
    window = facts.get("window", 0)
    lines.append(L(f"Messages measured: the most recent {window}", f"量到的消息:最近 {window} 条"))
    total = facts.get("messages") or {}
    lines.append(L(
        "  by kind: {user} from the user, {agent} from members, {system} system notes, {plan} task boards".format(
            user=total.get("user", 0), agent=total.get("agent", 0),
            system=total.get("system", 0), plan=total.get("plan", 0)),
        "  按来源:用户 {user} 条、成员 {agent} 条、系统提示 {system} 条、任务板 {plan} 条".format(
            user=total.get("user", 0), agent=total.get("agent", 0),
            system=total.get("system", 0), plan=total.get("plan", 0))))

    speakers = facts.get("speakers") or []
    if speakers:
        lines.append(L("  who spoke: " + ", ".join(f"{n}×{c}" for n, c in speakers),
                       "  谁发过言:" + "、".join(f"{n}×{c}" for n, c in speakers)))
    silent = facts.get("silent") or []
    if silent:
        lines.append(L("  never spoke in this window: " + ", ".join(silent),
                       "  本窗口内一次未发言:" + "、".join(silent)))

    calls = facts.get("tool_calls") or {}
    ran = any(calls.get(k) for k in ("ok", "failed", "denied", "other"))
    if ran:
        lines.append(L("Tool calls: {ok} ok, {failed} failed, {denied} refused by the user, {other} other".format(
            ok=calls.get("ok", 0), failed=calls.get("failed", 0),
            denied=calls.get("denied", 0), other=calls.get("other", 0)),
            "工具调用:成功 {ok} 次、失败 {failed} 次、被用户拒绝 {denied} 次、其余 {other} 次".format(
                ok=calls.get("ok", 0), failed=calls.get("failed", 0),
                denied=calls.get("denied", 0), other=calls.get("other", 0))))
        for name, n in (calls.get("failed_by") or [])[:8]:
            lines.append(L(f"  failed: {name} ×{n}", f"  失败:{name} ×{n}"))
        for note in (calls.get("messages") or [])[:6]:
            lines.append("  " + note)
    else:
        # Stated outright rather than left as a row of zeros: a round in which no tool produced
        # anything is the single most useful line this digest can carry, and a model reading
        # "0 ok, 0 failed" can still believe something was made.
        lines.append(L("Tool calls: none recorded in this window — nothing here was produced by a tool.",
                       "工具调用:本窗口内一次都没有 —— 这里的任何东西都不是工具做出来的。"))

    tasks = facts.get("tasks") or []
    if tasks:
        lines.append(L("Latest task board:", "最近一次任务板:"))
        for t in tasks:
            mark = t.get("status", "?")
            extra = f" — {t['error']}" if t.get("error") else ""
            lines.append(f"  {t.get('id', '?')} [{mark}] {t.get('owner', '?')}: {t.get('title', '')}{extra}")
    else:
        lines.append(L("No task board in this window: the work was never split up.",
                       "本窗口内没有任务板:这轮工作从未被分工。"))

    missing = facts.get("missing") or []
    if missing:
        lines.append(L("Promised but NOT on disk: " + ", ".join(missing),
                       "计划里写了、但工作目录里没有的文件:" + "、".join(missing)))
    files = facts.get("files") or []
    lines.append(L(f"Files in the workspace: {facts.get('file_count', len(files))}",
                   f"工作目录里的文件:{facts.get('file_count', len(files))} 个"))
    for f in files[:12]:
        lines.append(L(f"  {f['path']} ({f.get('size', 0)} bytes, {f.get('age', '')})",
                       f"  {f['path']}（{f.get('size', 0)} 字节,{f.get('age', '')}）"))
    if facts.get("older_files"):
        lines.append(L(f"  …and {facts['older_files']} more", f"  ……还有 {facts['older_files']} 个"))

    notes = facts.get("system_notes") or []
    if notes:
        lines.append(L("System notes posted to the chat (repeats are the loudest signal):",
                       "发到群里的系统提示(同一条重复出现最值得注意):"))
        for text, n in notes[:8]:
            lines.append(f"  ×{n} {text}")

    log = facts.get("log") or {}
    lines.append(L(
        "Process log so far: {open} open, {fixed} changed-but-unchecked, {verified} re-checked, {wontfix} decided not to",
        "流程日志现状:{open} 条待处理、{fixed} 条已改未复核、{verified} 条已复核、{wontfix} 条决定不改").format(
            open=log.get("open", 0), fixed=log.get("fixed", 0),
            verified=log.get("verified", 0), wontfix=log.get("wontfix", 0)))
    return "\n".join(lines)
