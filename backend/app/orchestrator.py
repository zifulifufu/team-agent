"""Group chat orchestration: decides who speaks, assembles context, calls the routing layer,
drives tool calls, parses @ hand-offs, and runs owner delegation.

Processing one user message:
  A. The user @-mentioned members / @all -> the mentioned members speak in turn; an @ inside
     a reply hands off to someone else (at most max_hops rounds).
  B. Nobody was @-mentioned -> it goes to the owner. If delegation is on (default "auto"),
     the owner first looks at the member strengths roster to decide whether to delegate:
       - yes: emit <plan> -> validate -> let each member do one task in dependency order
         (with the shared conventions and upstream results) -> the owner merges.
       - no: the owner answers directly, and @ in the reply still hands off as usual.
  Within each turn a member can call tools (library / memory / plugins / MCP) through the
  text protocol, for at most tool_rounds rounds.
"""

from __future__ import annotations

from . import i18n
from . import images as images_lib

import asyncio
import json
import re
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import external, planner, scoring, vision, cooperation, teamrec, strengths
from . import attachments as attachments_lib
from . import coderun, comfyui, imagegen, localcmd, localocr, media, proclog, video
from .approvals import Approvals
from .external import ExternalError, ExternalRunner
from .library import Library
from .mcp_client import McpManager
from .memory import MemoryService
from .presets import twin_name
from .prompting import PromptBuilder
from .router import AllRoutesFailed, ModelRouter, redact
from .store import Store, new_id
from .toolcall import (TagFilter, format_result, parse_failure, parse_tool_calls, strip_hidden,
                       tools_prompt)
from .toolhub import ToolHub, planning_tools
from .tools import ToolRegistry
from . import templates
from . import tools

Emit = Callable[[dict], Awaitable[None]]

# Streaming delta coalescing: models usually emit one chunk per 1-3 characters, so
# forwarding them as-is turns a 1000-character answer into several hundred WebSocket frames
# and as many front-end re-renders. Send only once DELTA_BATCH_CHARS characters have
# accumulated, or DELTA_BATCH_SECONDS has passed since the last send; the concatenation is
# identical to the original, while frame count and re-render count drop by an order of magnitude.
DELTA_BATCH_CHARS = 24
DELTA_BATCH_SECONDS = 0.06

# Live output of a running tool call. A program that prints as it works would otherwise be shown as
# "something is running" for minutes. Two numbers, for the same reason the deltas above have two: a
# chatty program (a build log, `pip install`, an npm script) emits thousands of short lines, and one
# WebSocket frame per line is a re-render per line. Keep the last LIVE_OUTPUT_CHARS characters — a
# terminal tail is what tells the user what is happening now; the beginning of the output is in the
# finished result anyway.
LIVE_OUTPUT_CHARS = 600
LIVE_OUTPUT_SECONDS = 0.6

# The reasoning stream (see `router.complete(on_reasoning=…)`). Coarser than the answer text: it
# arrives as one long unbroken run, and a reader follows it in paragraphs, not characters. Only the
# tail is stored in the message — a reasoning model can emit tens of thousands of characters, and
# this is a record of how the answer was reached, not a transcript of the model's inner monologue.
THINK_BATCH_CHARS = 60
THINK_BATCH_SECONDS = 0.25
THINK_STORE_CHARS = 4000

# an @ preceded by alphanumerics (an address like me@x.com) is not a mention; @all followed
# by letters (@Allen) is not one either
_ALL_RE = re.compile(r"(?<![A-Za-z0-9_.])@(?:所有人|all(?![A-Za-z0-9_]))", re.IGNORECASE)  # i18n-keep: accepts @all and @所有人 in any language


# An audio file has no text to give, and no frames to look at: its line is kept apart so the
# prompt can say "there is sound here" without pretending it was read.
_AUDIO_MARK = "\x00audio\x00"


def _scene(extra_meta: dict | None) -> str:
    """Which part of a round this turn is: a plain reply, one task, or the consolidation.

    Handed to the `pre_prompt` hooks so they can act on the right one — "answer in the group's
    house style" may belong on every task instruction, or only on the answer the user reads.
    """
    tid = (extra_meta or {}).get("task_id")
    if not tid:
        return "reply"
    return "integration" if tid == "final" else "task"


def _mime_of(path: Path) -> str:
    """The type of a picture on disk. Only ever called for what we took out of a video or an
    upload we already classified, so the extension is enough."""
    ext = path.suffix.lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")


def _mention_re(name: str) -> "re.Pattern[str]":
    tail = r"(?![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", name[-1:]) else ""
    return re.compile(r"(?<![A-Za-z0-9_.])@" + re.escape(name) + tail)


def _member_names(member: dict) -> list[str]:
    """Every spelling this member answers to: the stored name plus its built-in twin.

    A built-in member may be stored under its Chinese name while the rest of the
    conversation uses the English one (or the reverse), so @mention has to accept both.
    """
    stored = member.get("name") or ""
    twin = twin_name(stored)
    return [n for n in (stored, twin) if n] or [stored]


def find_mentions(text: str, members: list[dict], exclude_id: str | None = None) -> list[dict]:
    """Returns the @-mentioned members in order of appearance (longer names are matched first,
    so a short name cannot accidentally capture a longer one).

    Two passes: first the name each member is actually stored under, then the
    other-language spelling of a built-in name. A stale alias must never steal a
    mention from a member whose real name matches.
    """
    hits: list[tuple[int, dict]] = []
    taken: list[tuple[int, int]] = []
    matched: set[str] = set()

    def try_match(m: dict, names: list[str]) -> None:
        for name in sorted(names, key=len, reverse=True):
            match = _mention_re(name).search(text)
            if not match:
                continue
            s, e = match.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            taken.append((s, e))
            hits.append((s, m))
            matched.add(m["id"])
            return

    order = sorted(members, key=lambda a: -len(a["name"]))
    for names_of in (lambda m: [m["name"]], _member_names):
        for m in order:
            if m["id"] == exclude_id or m["id"] in matched:
                continue
            try_match(m, names_of(m))
    hits.sort(key=lambda x: x[0])
    return [m for _, m in hits]


def mentions_all(text: str) -> bool:
    return bool(_ALL_RE.search(text))


def _strip_mentions(text: str, members: list[dict]) -> str:
    """The sentence with every member's name taken out.

    For a generating member the sentence is the instruction, and a name inside it is not harmless:
    "@Seedance 画一只猫" left whole gets "Seedance" written into the picture. Every member's name and
    alias is removed rather than only the addressee's, because "@A 和 @B 各画一张" reaches both of
    them and neither should see the other's name in its prompt. Matching is case-insensitive and
    anchored on the `@`, exactly as `find_mentions` does, so a name that happens to be an ordinary
    word in the sentence is left alone.
    """
    out = text or ""
    for m in members:
        for name in _member_names(m):
            out = re.sub(rf"@{re.escape(name)}(?=\s|$|[，,。.、!?！？:：;；])", "", out, flags=re.IGNORECASE)
    # A trailing "…，" left behind by a removed mention reads as if the sender trailed off.
    out = re.sub(r"^[\s，,。.、:：;；!?！？]+", "", out)
    return " ".join(out.split()).strip()


def _clean_prompt(text: str) -> str:
    """The prompt out of a model's answer, with the packaging taken off.

    Models like to answer with a fenced block, or with "Here is the prompt:", or with a closing
    offer to change it. None of that belongs in a video model's text field, and taking it off here
    costs less than explaining to the user afterwards why their clip has `**` in the middle of it.

    A fenced block wins outright: if one is there, its contents are the answer and everything around
    it (including the offer to adjust it) is dropped, which is the one case where the model has
    already marked the boundary itself.
    """
    s = (text or "").strip()
    fenced = re.search(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", s, re.S)
    if fenced:
        s = fenced.group(1)
    else:
        s = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"^(?:prompt|提示词|生成提示词|视频提示词)\s*[:：]\s*", "", s.strip(), flags=re.I)
    s = s.strip()
    # A whole answer wrapped in one pair of quotes is packaging; a quote *inside* a prompt (someone's
    # line of dialogue) is content, and only the outermost pair is looked at.
    for a, b in (('"', '"'), ("“", "”"), ("「", "」")):
        if len(s) > 1 and s.startswith(a) and s.endswith(b):
            s = s[1:-1].strip()
    # The last defensive step, and the one that saves a whole render: a model that answered with
    # `{"prompt": "…"}` would otherwise have that JSON filmed literally.
    if s.startswith("{"):
        try:
            got = json.loads(s)
        except ValueError:
            got = None
        if isinstance(got, dict):
            for key in ("prompt", "text", "content", "提示词"):
                if isinstance(got.get(key), str) and got[key].strip():
                    return got[key].strip()
    return s.strip()


def _tool_instruction(text: str, members: list[dict], agent: dict) -> str:
    """Separate multiple @tool assignments in one reply before executing either one."""
    matches = [(match.start(), match.end(), m["id"]) for m in members
               for name in _member_names(m) for match in _mention_re(name).finditer(text)]
    matches.sort()
    own = next((x for x in matches if x[2] == agent["id"]), None)
    if own:
        end = next((s for s, _, aid in matches if s >= own[1] and aid != agent["id"]), len(text))
        segment = text[own[1]:end].strip(" \n\t:：,，;；")
        if segment:
            return segment
    return _strip_mentions(text, members)


# The optional last line of a generation-prompt answer: what the writer could not establish from the
# material and therefore left out of the picture. Both spellings are accepted because the writer is
# told to answer in the language of the conversation, which need not be the language of this program.
_UNGROUNDED_RE = re.compile(r"^[ \t]*(?:未确证|未证实|未能确证|Ungrounded|Not\s+(?:established|grounded))"
                            r"[ \t]*[:：][ \t]*(.*)$", re.I | re.M)
_NOTHING_RE = re.compile(r"^(?:无|没有|none|nothing|n/?a)[。.!\s]*$", re.I)

# A file name inside a task's `deliverable` — what that task promised to hand over. Read by
# `_task_shortfall` to catch "the deliverable was never written".
#
# ⚠️⚠️ This used to be a **27-extension list of its own**, and it was the riskiest copy of that
# judgement in the app: it did not merely word an error, it decided **whether delivery was verified
# at all** (`_execute_plan` skips the whole check when nothing here matches). So a deliverable named
# `out/animation.json`, `notes.tex` or `video/字幕轨.html` — none of them in the list — was accepted
# as `done` with nothing on disk, and the run had no way to notice. `planner.named_files` is the one
# place that answers this; ask it, do not restate it.
_FILE_IN_PLAN = planner.named_files


def split_ungrounded(answer: str) -> tuple[str, str]:
    """"(the prompt, what the writer could not ground)" out of one answer.

    The writer is asked for one extra line saying which facts it could not establish from the
    material. Splitting it off here is what keeps that line out of the video model's text field —
    and what lets the group be told, next to the clip, which parts of the picture have nothing
    behind them. A writer that ignores the request costs nothing: the whole answer is the prompt.
    """
    found = _UNGROUNDED_RE.search(answer or "")
    if not found:
        return (answer or "").strip(), ""
    said = found.group(1).strip()
    prompt = (answer[:found.start()] + answer[found.end():]).strip()
    return prompt, ("" if _NOTHING_RE.match(said) else said)


# The host says this when it produced a plan but no prose. Kept as a pair so the value
# follows the request language instead of freezing at import time.
PLAN_FALLBACK = ("The work is split — see the task board.", "已做好分工,见任务板。")


def plan_fallback() -> str:
    """The stand-in line for a host turn that only produced a plan."""
    return i18n.pick_now(*PLAN_FALLBACK)


def _bad_task_id(why: str) -> str:
    """The task id a rejection names, if it names one ("任务 t6: …" / "Task t6: …")."""
    m = re.search(r"(?:任务|Task)\s+(t\d+)\b", why or "")
    return m.group(1) if m else ""


def _plan_excerpt(raw: str, limit: int = 500, focus: str = "") -> str:
    """What the model actually sent, appended to a rejection notice.

    The notice used to say only *why* the plan was refused ("任务 t1 缺少 instruction") and the raw
    text was not kept anywhere: the chat stores the message with its `<plan>` block already stripped,
    so the one artefact that would explain the failure was gone by the time anybody looked. Reading
    the plan back is how a reader sees whether the model used another key, nested the tasks, or
    simply stopped mid-object — three different repairs for one sentence of complaint.

    ⚠️ `focus` is the task the rejection is *about*, and without it the excerpt is useless in the one
    case it is needed most: a plan long enough to hit `limit` is cut at the top of the file, so a
    complaint about `t6` arrives next to the JSON for `t1`–`t3`. Measured 2026-09-26 on the video
    group: two rounds were rejected over `t6` and neither the user nor the repairing model could see
    what `t6` said. When a task id is given, that task's own JSON is shown instead of the head.
    """
    text = " ".join((raw or "").split())
    block = ""
    low = text.lower()
    if "<plan>" in low:
        start = low.index("<plan>")
        end = low.find("</plan>", start)
        block = text[start:end + 7] if end >= 0 else text[start:]
    shown = block or text
    if not shown:
        return ""
    if focus:
        # Match through a space-stripped copy, then cut on that same copy: the JSON the model sent is
        # pretty-printed, and `"id": "t6"` versus `"id":"t6"` must both find the task.
        compact = shown.replace(" ", "")
        at = compact.find(f'"id":"{focus}"')
        if at >= 0:
            head = compact.rfind("{", 0, at)
            depth, i, quoted, esc = 0, head, False, False
            while i < len(compact):
                ch = compact[i]
                if quoted:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        quoted = False
                elif ch == '"':
                    quoted = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            # The one task object, so the reader sees every field the validator looked at, plus the
            # overall goal — which is what makes a broken task's intent still legible.
            goal = compact.split('"goal":"', 1)[-1].split('","', 1)[0][:180] if '"goal":"' in compact else ""
            shown = f"[{focus}] {compact[head:i + 1]}" + (f"\n(goal: {goal})" if goal else "")
    if len(shown) > limit:
        shown = shown[:limit] + "…"
    return i18n.pick_now(f"\nWhat it sent: {shown}", f"\n它发出来的是:{shown}")


@dataclass
class RunState:
    """Record of one user message from start to finish (used for memory and statistics)."""
    gid: str
    user_text: str
    started: float = field(default_factory=time.time)
    steps: list[dict] = field(default_factory=list)
    final_text: str = ""
    refs_block: str = ""
    history: list[dict] | None = None
    warned: set[str] = field(default_factory=set)
    # The files this message carries, as descriptors ({id, name, kind, mime, bytes}). The content
    # they contribute is prepared per member in `_files_for_turn`, because whether a member reads a
    # description or looks at the picture depends on its own model.
    files: list[dict] = field(default_factory=list)
    # Descriptions worked out during this round, keyed by attachment id, so two members asking in
    # the same round do not describe the same picture twice.
    described: dict[str, str] = field(default_factory=dict)
    # Set for rounds whose trigger came from outside this machine (the WhatsApp channel).
    # It restricts the tool list to read-risk tools only: such a message can ask questions
    # but never reaches exec/write tools, and nobody local is watching the approval prompt
    # that those tools would otherwise raise.
    read_only: bool = False
    # What the process engineer will write down about this round: the measured defects the pieces of
    # the round collected (see `proclog.auto_task_defects` / `auto_round_defects`). Collected during
    # the round and written once at the end, so the ledger is touched once per round rather than
    # once per defect.
    defects: list[dict] = field(default_factory=list)
    # How many times each tool *failed* in this round, by name. Two failures of the same call is the
    # signature of a member that is looping, which is a defect in the arrangement rather than in the
    # model, so it is one of the things the process log records.
    tool_failures: dict[str, int] = field(default_factory=dict)
    # Titles of the tasks still unfinished when the plan ended.
    unfinished: list[str] = field(default_factory=list)
    # Why the last turn that failed did so, in the words of the piece that failed. A turn that
    # returns nothing knows a specific reason — a local tool with an empty project folder, an
    # external agent whose settings do not comply, every model in the chain out of quota — and that
    # reason used to be thrown away by its caller: `_execute_plan` wrote "the model call failed" over
    # all of them, which is not only less useful, it can be false (no model was involved). Set by the
    # failure paths, cleared at the start of every turn and read once by the plan's task loop.
    last_error: str = ""
    round_id: str = ""
    process_feedback: str = ""
    process_ids: list[str] = field(default_factory=list)
    process_used: bool = False
    observations: list[dict] = field(default_factory=list)


def _file_label(entry: dict) -> str:
    """A file an artifact entry names — the one spelling, in the one place.

    Two copies of this existed and they were not the same: `_task_shortfall` read
    `path or rel or name or ""`, while the assignment two functions below read `path or rel or
    name` — so an artifact entry carrying none of the three raised `KeyError` out of the task loop,
    taking the whole round with it. `synthesize_speech` is the entry that has `name` but no `path`;
    anything storing `name` under another key would have crashed there.
    """
    return str(entry.get("path") or entry.get("rel") or entry.get("name") or "")


def _delivery_tools(message: dict) -> list[dict]:
    """Keep successful work and review failures visible across a delivery-only retry."""
    meta = message.get("meta") or {}
    return [*(meta.get("delivery_previous_tools") or []), *(meta.get("tools") or [])]


@dataclass
class TurnOut:
    text: str          # visible content stored in the chat record
    raw: str           # raw model output of each round (including <plan> / <tool_call>)
    message: dict


@dataclass
class TurnFiles:
    """What a member is told about the files of the message it is answering.

    `block` is the part it reads: extracted document text, media metadata, and a description of
    anything it cannot look at. `pictures` are attached to the request directly, when the member's
    own model can see them — then no description is needed and none is paid for.
    """
    block: str = ""
    pictures: list[dict] = field(default_factory=list)      # content parts: image_url


class Orchestrator:
    def __init__(
        self, store: Store, router: ModelRouter, *, prompts: PromptBuilder | None = None,
        toolhub: ToolHub | None = None, memory: MemoryService | None = None, library: Library | None = None,
        registry: ToolRegistry | None = None, mcp: McpManager | None = None,
        approvals: Approvals | None = None, external_runner: ExternalRunner | None = None,
        hooks: Any = None,
    ):
        self.store = store
        # The runner is handed the store because an engine bound to a model provider (MetaChat)
        # takes its address, its key and its model list from that provider row.
        self.external = external_runner or ExternalRunner(store.data_dir, store=store)
        self.router = router
        self.library = library or Library(store)
        self.memory = memory or MemoryService(store, router)
        self.registry = registry or ToolRegistry()
        self.mcp = mcp or McpManager()
        self.toolhub = toolhub or ToolHub(store, self.registry, self.mcp, self.library, self.memory,
                                          hooks=hooks, router=router)
        self.prompts = prompts or PromptBuilder(store, router)
        self.approvals = approvals or Approvals(store)
        # The user's own hooks (see app/hooks.py). Optional everywhere so a test can build an
        # orchestrator without one; the channel layer reads it from here.
        self.hooks = hooks
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_inputs: dict[str, list[dict]] = {}
        # All entry points (desktop and chat channels) share this lifecycle registry.
        self.tasks: dict[str, set[asyncio.Task]] = {}
        self._bg: set[asyncio.Task] = set()
        self._process_locks: dict[str, asyncio.Lock] = {}
        # Optional callback `(group_id, final_text)` run once per finished round, for
        # channels that only push (a group robot cannot be answered, so it receives what
        # the group produced rather than a reply to a question). Set by main.py; absent in
        # tests, where nothing should leave the machine.
        self.on_answer: Callable[[str, str], Awaitable[None]] | None = None

    def _lock(self, gid: str) -> asyncio.Lock:
        return self._locks.setdefault(gid, asyncio.Lock())

    def _spawn(self, coro: Awaitable[Any]) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """Wait for background tasks (memory extraction) to finish. Used by tests and on shutdown."""
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ---------------------------------------------------------------- context
    def build_messages(
        self, group: dict, agent: dict, members: list[dict], *, memory_block: str = "", tools_block: str = "",
        extra_system: str = "", extra_user: str | None = None, exclude_plan_id: str | None = None,
        files: "TurnFiles | None" = None,
        history: list[dict] | None = None,
    ) -> list[dict]:
        cfg = self.store.get_settings()
        history = (self.store.list_messages(group["id"], int(cfg["history_limit"]))
                   if history is None else history[-int(cfg["history_limit"]):])
        clip = int(cfg["history_clip"])
        sysmsg = self.prompts.system_prompt(
            group, agent, members, memory_block=memory_block, tools_block=tools_block, extra=extra_system
        )
        convo: list[dict] = []
        last_user = max((i for i, h in enumerate(history) if h["sender_type"] == "user"), default=-1)
        for i, h in enumerate(history):
            if h["sender_type"] in ("system", "plan"):
                continue
            if exclude_plan_id and h["meta"].get("plan_id") == exclude_plan_id:
                continue  # other members' results for this delegation are given in full in the task prompt, so they
# are not repeated in the history
            text = h["content"] if i in (len(history) - 1, last_user) else clip_middle(h["content"], clip)   # the latest message and the user's latest request are carried over as-is
            if h["sender_type"] == "agent" and h["sender_id"] == agent["id"]:
                role, content = "assistant", text
            else:
                role, content = "user", f"[{h['sender_name']}] {text}"
            if convo and convo[-1]["role"] == role:  # merge adjacent messages with the same role, for APIs that require strict alternation
                convo[-1]["content"] += "\n\n" + content
            else:
                convo.append({"role": role, "content": content})
        while convo and convo[0]["role"] == "assistant":
            convo.pop(0)
        if not convo or convo[-1]["role"] != "user":
            convo.append({"role": "user", "content": i18n.pick_now("(please continue)", "(请继续)")})
        convo[-1]["content"] += i18n.pick_now(f"\n\n(it is now your turn, {agent['name']})", f"\n\n(现在轮到你「{agent['name']}」发言)")
        if extra_user:
            convo[-1]["content"] += "\n\n" + extra_user
        if files is not None:
            self._attach_files(convo[-1], files)
        return [{"role": "system", "content": sysmsg}] + convo

    def _head_model(self, agent: dict) -> dict | None:
        """The model this member will try first — the one that decides whether images fit."""
        chain, _ = self.router.build_chain(agent.get("model_id") or "", agent.get("tags"))
        return chain[0] if chain else None

    def _may_see_images(self, model: dict | None, cfg: dict) -> bool:
        if not model or "multimodal" not in (model.get("strengths") or []):
            return False
        return bool(model.get("is_local")) or bool(cfg["vision_cloud"])

    # ------------------------------------------------------------------ files the model reads
    REF_FILE_CHARS = 4000        # per document, whether attached or referenced
    REF_DIR_FILES = 60           # entries listed for a referenced folder

    def _attach_files(self, turn: dict, files: "TurnFiles") -> None:
        """Put the prepared files into the turn: what it reads, and the pictures it looks at.

        The content only becomes a list of parts when there really are pictures — a text-only turn
        stays a plain string, which is what every provider handles best.
        """
        if files.block:
            turn["content"] += "\n\n" + files.block
        if files.pictures:
            turn["content"] = [{"type": "text", "text": turn["content"]}, *files.pictures]

    async def _files_for_turn(self, group: dict, agent: dict, run: "RunState") -> "TurnFiles":
        """What this member should know about the files in the message it is answering.

        Two shapes, decided by the member's own model: pictures it can look at are attached to the
        request as content parts, and pictures nobody can look at are described first — the
        description is cached on the attachment, so one screenshot is described once however many
        members then discuss it. Documents need neither: they are text, extracted at upload.
        """
        cfg = self.store.get_settings()
        sees = self._may_see_images(self._head_model(agent), cfg)
        out = TurnFiles()
        lines: list[str] = []
        workspace = self.workspace(group["id"])

        for meta in run.files:
            row = self.store.get_attachment(str(meta.get("id") or ""))
            if not row or row["group_id"] != group["id"]:
                continue
            path = attachments_lib.path_for_row(self.store, row)
            if path is None:
                lines.append(i18n.pick_now(f"[{row['name']}] the file is gone from disk",
                                           f"【{row['name']}】文件已不在磁盘上"))
                continue
            text, pictures = await self._file_parts(group, row, path, workspace, sees, run)
            if text:
                lines.append(text)
            out.pictures += pictures

        for rel, path in self._referenced_files(run, workspace):
            row = {"id": f"file:{rel}", "name": path.name, "kind": attachments_lib.kind_of_name(path.name),
                   "bytes": path.stat().st_size if path.exists() else 0, "meta": "{}"}
            text, pictures = await self._file_parts(group, row, path, workspace, sees, run, rel=rel)
            if text:
                lines.append(text)
            out.pictures += pictures

        audio = [line for line in lines if line.startswith(_AUDIO_MARK)]
        lines = [line for line in lines if not line.startswith(_AUDIO_MARK)]
        if lines:
            out.block = i18n.pick_now("[Files in this message]\n", "【这条消息里的文件】\n") + "\n\n".join(lines)
        if audio:
            out.block += ("\n\n" if out.block else "") + "\n".join(audio)
        # The same budget the other references answer to, applied to the whole block: ten files
        # that are each within their own limit still add up to a prompt nobody asked for.
        budget = int(cfg["refs_budget"])
        if len(out.block) > budget:
            keep = max(0, budget - 200)
            out.block = out.block[:keep] + "\n\n" + i18n.pick_now(
                "(the rest was cut to keep this prompt within its budget; read the files from the "
                "workspace if you need them)",
                "(其余内容因超出预算已截断;需要的话请直接从工作目录读取文件)")
        return out

    async def _file_parts(self, group: dict, row: dict, path: Path, workspace: Path, sees: bool,
                          run: "RunState", rel: str = "") -> tuple[str, list[dict]]:
        """One file -> (the text a member reads, the pictures it is handed)."""
        kind = row.get("kind") or attachments_lib.kind_of_name(row.get("name") or "")
        name = row.get("name") or path.name
        where = rel or (row.get("rel_path") or "")
        header = f"[{attachments_lib.heading(row)}]" + (f" @ {where}" if where else "")
        if kind == attachments_lib.DOCUMENT:
            text = row.get("text") or ""
            if not text:
                text = await asyncio.to_thread(attachments_lib.text_of_file, workspace, path)
                if row.get("id") and not str(row["id"]).startswith("file:"):
                    self.store.set_attachment_text(str(row["id"]), text or None)
            if not text:
                # Name, size and path only — the member is *told* that nothing was pulled out
                # instead. A scanned PDF used to arrive as a bare header, which reads like a
                # document whose contents were simply uninteresting; the reader has no way to know
                # that the text was never there to read, and would confidently discuss a report
                # nobody has seen.
                return i18n.pick_now(
                    f"{header} — a document, but no text could be pulled out of it on this machine "
                    "(it may be a scan). The file itself is in the workspace if a member has the "
                    "tools to open it.",
                    f"{header} —— 文档,但本机没能从它里面抽出文字(可能是扫描件)。文件本身就在工作目录里,"
                    "成员如果有工具可以自己打开它。"), []
            body = clip_middle(text, self.REF_FILE_CHARS)
            tail = i18n.pick_now(" (excerpt; read the whole file in the workspace if you need it)",
                                 "(节选,需要完整内容可在工作目录里读)")
            return f"{header}\n{body}{tail}", []
        if kind == attachments_lib.IMAGE:
            return await self._visual_text(row, path, [path], workspace, sees, run, header, kind)
        if kind == attachments_lib.VIDEO:
            frames = await asyncio.to_thread(self._video_frames, workspace, str(row.get("id") or name), path, int(
                self.store.get_settings()["video_frames"]))
            note = (i18n.pick_now("(key frames were taken from it; the audio track is not transcribed)",
                                  "(已抽取关键帧;音轨不做转写)") if not frames else "")
            text, pictures = await self._visual_text(row, path, frames, workspace, sees, run, header, kind)
            return (f"{text}\n{note}".strip() if note else text), pictures
        if kind == attachments_lib.AUDIO:
            # Speech is read once, on this machine, and remembered on the row — the same deal as a
            # document: one slow first turn, then it is text like any other, for every member.
            words = row.get("text") or ""
            if not words:
                words = await asyncio.to_thread(attachments_lib.transcribe, path,
                                                self.store.get_settings()) or ""
                if words and row.get("id") and not str(row["id"]).startswith("file:"):
                    self.store.set_attachment_text(str(row["id"]), words)
            if words:
                tail = i18n.pick_now(" (transcribed on this machine; the audio itself was not sent anywhere)",
                                     "(由本机转写的文字;音频本身没有发到任何地方)")
                return f"{header}\n{clip_middle(words, self.REF_FILE_CHARS)}{tail}", []
            return _AUDIO_MARK + i18n.pick_now(
                f"{header} — audio; nothing on this machine could transcribe it, so what it says was "
                f"not read. A member can open the file from the workspace, and a transcriber can be "
                f"named under Settings → General → Files in a group chat.",
                f"{header} —— 音频;这台机器上没有能转写它的工具,所以内容没有被读到。成员可以在工作目录里打开它;"
                f"装了转写工具后,可在「设置 → 通用 → 群聊里的文件」里填上命令。",
            ), []
        return i18n.pick_now(
            f"{header} — not a format that can be read as text; the file is in the workspace.",
            f"{header} —— 不是可读为文本的格式;文件就在工作目录里。"), []

    async def _visual_text(self, row: dict, path: Path, pictures: list[Path], workspace: Path,
                           sees: bool, run: "RunState", header: str, kind: str) -> tuple[str, list[dict]]:
        """Pictures: handed over when the model can look, described when it cannot."""
        blobs: list[tuple[str, bytes]] = []
        for picture in pictures:
            try:
                blobs.append((_mime_of(picture), picture.read_bytes()))
            except OSError:
                continue
        if not blobs:
            return i18n.pick_now(f"{header} — the file could not be read.",
                                 f"{header} —— 文件读不出来。"), []
        if sees:
            limit = int(self.store.get_settings()["vision_max_mb"]) * 1024 * 1024
            parts = []
            for mime, data in blobs:
                if kind == attachments_lib.IMAGE:
                    mime, data = await asyncio.to_thread(attachments_lib.shrink_image, data, mime, limit)
                else:
                    mime, data = "image/jpeg", data
                parts.append({"type": "image_url", "image_url": {"url": images_lib.data_uri(data, mime)}})
            return "", parts
        key = f"file:{row.get('rel_path') or path.name}"
        text = run.described.get(key) or (row.get("vision_text") or None)
        if not text:
            text = await self._describe(row, workspace, key, path.name, kind, blobs, run)
        if text:
            return f"{header}\n{text}", []
        # Nobody here can look at a picture, and the reader is owed both halves of that: *why* no
        # model looked, and whatever this machine can still get out of the file on its own. On a
        # machine with no vision model and cloud vision off, an attached screenshot used to produce
        # only the first half — which is a true and useless answer to a member that just wanted the
        # words in an error dialog. The reason stays in the sentence rather than being replaced by
        # the text: a reader handed words with no explanation would take them for a description.
        if localocr.available()[0] and pictures:
            try:
                blocks = await asyncio.to_thread(localocr.read, pictures)
            except localocr.OcrError as e:
                print("local text reading failed:", repr(e))
                blocks = []
            if any(b.get("text") for b in blocks):
                return (f"{header} — {vision.reason_missing(self.store, self.router)}\n"
                        f"{localocr.render(blocks)}"), []
        return f"{header} — {vision.reason_missing(self.store, self.router)}", []

    async def _describe(self, row: dict, workspace: Path, key: str, name: str, kind: str,
                        blobs: list[tuple[str, bytes]], run: "RunState") -> str:
        """Ask the vision model once, remember it on the row (or in the workspace cache)."""
        try:
            text = await vision.describe(self.store, self.router, blobs,
                                         attachments_lib.describe_prompt(name, kind))
        except Exception as e:  # noqa: BLE001 — a picture nobody could look at must not fail the round
            print("vision description failed:", repr(e))
            return ""
        run.described[key] = text
        aid = str(row.get("id") or "")
        if aid and not aid.startswith("file:"):
            self.store.set_attachment_vision(aid, text or None)
        elif text:
            attachments_lib.remember(workspace, attachments_lib.file_key(workspace, workspace / key), "desc", text)
        return text

    def _video_frames(self, workspace: Path, aid: str, path: Path, count: int) -> list[Path]:
        """Frames for a video, kept under the workspace so the work is done once.

        The folder is named after the attachment, and `attachments.frames` writes numbered files
        into it rather than reading it — so a second round re-uses the stills instead of running
        ffmpeg again over the same film.
        """
        picked = sorted((workspace / ".frames" / aid).glob("f*.jpg"))
        return picked[:count] if picked else attachments_lib.frames(path, workspace / ".frames" / aid, count)

    # ------------------------------------------------------------------ references
    def _parse_refs(self, text: str) -> list[dict]:
        """The reference tokens in a user message.

        The whole grammar, most of it written by the picker rather than remembered by the user:
          `@name`            a member speaks (handled by find_mentions, deliberately not here)
          `@file:<path>`     a file in this group's workspace
          `@dir:<path>`      a folder in it
          `@msg:<id>`        an earlier message of this group
          `@doc:<id>`        a document this group can reach
          `#<title>`         the same document by title (the long-standing shorthand)
        """
        found: list[dict] = []
        for m in re.finditer(r"@(file|dir|msg|doc):(\"[^\"]+\"|[^\s,，。;；]+)", text):
            value = m.group(2)
            found.append({"kind": m.group(1), "value": value[1:-1] if value.startswith('"') else value})
        return found

    def _referenced_files(self, run: "RunState", workspace: Path) -> list[tuple[str, Path]]:
        """Workspace files named with `@file:`, resolved and checked to be inside the workspace."""
        out: list[tuple[str, Path]] = []
        for ref in self._parse_refs(run.user_text):
            if ref["kind"] != "file":
                continue
            target = attachments_lib.resolve(workspace, ref["value"])
            if target and target.is_file():
                out.append((ref["value"], target))
        return out

    def _refs_items(self, group: dict, text: str, workspace: Path) -> list[tuple[str, str]]:
        """What the user asked for by reference, as (a name to cite it by, the text itself).

        Documents the knowledge base can reach (by `#title` or `@doc:`), folders and earlier
        messages of this conversation, already clipped into the reference budget. The name is what
        makes the same material citable somewhere else — a generation prompt says which documents it
        was built on, and that has to come from the thing that resolved them, not from a second
        guess at what the `#`-tokens meant.
        """
        budget = int(self.store.get_settings()["refs_budget"])
        out: list[tuple[str, str]] = []
        used = 0

        def take(name: str, chunk: str) -> bool:
            nonlocal used
            if used + len(chunk) > budget:
                return False
            out.append((name, chunk))
            used += len(chunk)
            return True

        allowed = None
        if group["ext"]["library"]["mode"] != "off":
            allowed = self.library.scope_ids(group["ext"]["library"], group["id"])
            for m in re.finditer(r"#([^\s#@,,。;;::!!??]{2,40})", text):   # i18n-keep: hashtag regex; the CJK punctuation set is the delimiter list
                doc = self.library.find_by_title(m.group(1))
                if doc and doc["enabled"] and (allowed is None or doc["id"] in allowed):
                    take(doc["title"], self._library_chunk(doc))
                if len(out) >= 3:
                    break

        for ref in self._parse_refs(text):
            if ref["kind"] == "doc":
                doc = self.store.get_doc(ref["value"])
                if doc and doc["enabled"] and (allowed is None or doc["id"] in allowed):
                    take(doc["title"], self._library_chunk(doc))
            elif ref["kind"] == "msg":
                chunk = self._message_chunk(group, ref["value"])
                if chunk:
                    take(f"@msg:{ref['value']}", chunk)
            elif ref["kind"] == "dir":
                chunk = self._folder_chunk(workspace, ref["value"])
                if chunk:
                    take(f"{ref['value']}/", chunk)
        return out

    def _refs_block(self, group: dict, text: str, workspace: Path) -> str:
        """`_refs_items` as one block of text, with the budget applied across all of them."""
        items = self._refs_items(group, text, workspace)
        if not items:
            return ""
        head = i18n.pick_now("[What the user referenced]\n", "【用户引用的内容】\n")
        return head + "\n\n".join(chunk for _, chunk in items)

    def _library_chunk(self, doc: dict) -> str:
        r = self.library.read(doc["id"], 0, 2500)
        more = i18n.pick_now("(excerpt; use library_read to read the rest)",
                             "(节选,完整内容可用 library_read 继续读)") if r["end"] < r["total"] else ""
        return i18n.pick_now(f"[{doc['title']}]{more}\n{r['text']}", f"《{doc['title']}》{more}\n{r['text']}")

    def _message_chunk(self, group: dict, mid: str) -> str:
        """An earlier message of this conversation, quoted whole."""
        row = next((m for m in self.store.list_messages(group["id"], 2000) if m["id"] == mid), None)
        if not row:
            return ""
        when = time.strftime("%m-%d %H:%M", time.localtime(row["created_at"]))
        files = row["meta"].get("files") or row["meta"].get("images") or []
        note = i18n.pick_now(f" (with {len(files)} file(s))", f"(附带 {len(files)} 个文件)") if files else ""
        return i18n.pick_now(
            f"[earlier message · {row['sender_name']} · {when}]{note}\n{clip_middle(row['content'], self.REF_FILE_CHARS)}",
            f"【既往消息 · {row['sender_name']} · {when}】{note}\n{clip_middle(row['content'], self.REF_FILE_CHARS)}")

    def _folder_chunk(self, workspace: Path, rel: str) -> str:
        """A folder as a listing with a first taste of each file — enough to decide what to open."""
        folder = attachments_lib.resolve(workspace, rel)
        if not folder or not folder.is_dir():
            return ""
        rows: list[str] = []
        for path in sorted(folder.rglob("*"))[: self.REF_DIR_FILES]:
            if not path.is_file() or path.is_symlink():
                continue
            kind = attachments_lib.kind_of_name(path.name)
            taste = ""
            if kind == attachments_lib.DOCUMENT:
                text = attachments_lib.text_of_file(workspace, path)
                taste = " — " + clip_middle(text, 200).replace("\n", " ") if text else ""
            rows.append(f"- {path.relative_to(workspace)} ({attachments_lib.human_size(path.stat().st_size)}){taste}")
        if not rows:
            return i18n.pick_now(f"[folder {rel}/] it is empty", f"【文件夹 {rel}/】里面是空的")
        return i18n.pick_now(f"[folder {rel}/ · {len(rows)} file(s)]\n", f"【文件夹 {rel}/ · {len(rows)} 个文件】\n") + "\n".join(rows)

    # ------------------------------------------------------------- entrypoint
    async def handle_user_message(self, gid: str, text: str, emit: Emit,
                               sender_name: str | None = None,
                               files: list[dict] | None = None,
                               read_only: bool = False) -> None:
        current = asyncio.current_task()
        active = self.tasks.setdefault(gid, set())
        active.add(current)
        try:
            await self._handle_user_message(gid, text, emit, sender_name, files, read_only)
        finally:
            active.discard(current)
            if not any(not t.done() for t in active):
                self._pending_inputs.pop(gid, None)
                await emit({"type": "idle"})

    async def _handle_user_message(self, gid: str, text: str, emit: Emit,
                                  sender_name: str | None, files: list[dict] | None,
                                  read_only: bool) -> None:
        # The name the user is labelled with in the transcript; resolved per request
        # rather than as a default argument, which is evaluated once at import time.
        sender_name = sender_name or i18n.pick_now("me", "我")
        group = self.store.get_group(gid)
        if not group:
            return
        user_msg = self.store.add_message(gid, "user", "user", sender_name, text,
                                          meta={"files": files} if files else None)
        pending = self._pending_inputs.setdefault(gid, [])
        pending.append({"id": user_msg["id"], "text": text, "files": list(files or []),
                        "read_only": read_only})
        await emit({"type": "message", "message": user_msg})
        # Coalesce consecutive queued inputs, including their files and references. Keep
        # network/read-only inputs separate from local ones so a local follow-up cannot
        # silently grant execution rights to a queued channel request.
        async with self._lock(gid):
            group = self.store.get_group(gid) or group
            index = next((i for i, item in enumerate(pending) if item["id"] == user_msg["id"]), -1)
            if index < 0:
                return
            if index + 1 < len(pending) and pending[index + 1]["read_only"] == read_only:
                return
            start = index
            while start > 0 and pending[start - 1]["read_only"] == read_only:
                start -= 1
            batch = pending[start:index + 1]
            del pending[start:index + 1]
            text = "\n\n".join(item["text"] for item in batch if item["text"])
            files = list({f["id"]: f for item in batch for f in item["files"] if f.get("id")}.values())
            run = RunState(gid, text, read_only=read_only, round_id=user_msg["id"])
            if self.store.get_settings()["process_autolog"]:
                try:
                    run.process_feedback, run.process_ids = proclog.feedback(
                        proclog.read(proclog.unit(self.store.workspace_dir(gid))))
                except OSError as e:
                    print("the process feedback could not be read:", e)
            # Later user messages may be queued while this round is executing. Freeze
            # its input so workers cannot switch to the next round's request mid-plan.
            queued_ids = {item["id"] for item in pending}
            run.history = [m for m in self.store.list_messages(gid, int(self.store.get_settings()["history_limit"]))
                           if m["id"] not in queued_ids]
            run.files = list(files or [])
            workspace = self.workspace(gid)
            # Nothing of this group's own is copied into a knowledge base here any more. A round used
            # to start by bringing the group's own material up to date — the text read out of its
            # attachments, plus the documents in its workspace — and the reason it is gone is written
            # out above `Library.workspace_kb`: a library is a shelf somebody chose to build, and the
            # search a round makes is over what they put on it. The group's own files are still read
            # directly, through the working directory and the attachments.
            run.refs_block = self._refs_block(group, text, workspace)
            self._notify("round.start", gid, group,
                         {"sender": sender_name, "chars": len(text),
                          "read_only": read_only, "files": len(files or [])})
            try:
                await self._run_turns(group, text, emit, run)
                self._after_run(group, run)
            finally:
                # Keep measured failures even if a later operation is cancelled or raises.
                fresh = self._process_record(group, run)
            # The process engineer's first half: what the round measured about *itself*, written into
            # the group's ledger. Inside the group lock because it is a write to the group's own
            # workspace, and silent because the watcher does not speak (see `_process_record`).
        await self._announce(gid, run)
        if fresh:
            # …and its second half, outside the lock and off the critical path: a model outside the
            # group is asked for the cause and the fix of unresolved entries missing a review. This is the only
            # part that costs anything; each pass is bounded and never delays group work.
            self._spawn(self._process_review(group, fresh))
        self._notify("round.end", gid, group, {
            "sender": sender_name, "entries": len(run.steps),
            "seconds": round(time.time() - run.started, 2),
            "agents": [s.get("agent") for s in run.steps],
            "answer_chars": len(run.final_text or ""),
        })

    def workspace(self, gid: str) -> Any:
        """The group's workspace, created if it is somehow missing.

        A group always has one — it is made when the group is made, and `ensure_workspaces` fills
        in the ones that existed before that. This is the third guarantee, for the case that
        matters most: a member is about to run code, and there would be nowhere to run it.

        Which directory that is comes from the store, because the user may have picked one for
        this group; asking here rather than rebuilding `<base>/<gid>` is what keeps a run inside
        the folder the user chose.
        """
        return self.store.workspace_dir(gid)

    # ------------------------------------------------------------------ per-task folders
    def _task_dir(self, group: dict, task: Any, plan_id: str = "") -> str:
        """The folder this task delivers into, inside the group's workspace.

        A task is a unit of work, so it gets a place of its own: two tasks running in parallel write
        into different folders instead of overwriting each other's files, and the user can see
        which part of the result came from which part of the plan.
        """
        workspace = self.workspace(group["id"])
        # IDs come from model text; use a generated suffix, never a raw ID as a path.
        # A new plan also gets its own directory so a later round cannot overwrite it.
        name = coderun.safe_slug(task.title or task.id, new_id())
        rel = f"tasks/{plan_id}/{name}" if plan_id else f"tasks/{name}"
        try:
            coderun.make_dir(workspace, workspace / rel)
        except (OSError, ValueError) as e:
            raise RuntimeError(i18n.pick_now(f"Could not create the task folder: {e}",
                                             f"无法创建任务交付目录:{e}")) from e
        return rel

    def _task_dir_note(self, task: Any) -> str:
        """Tell the member where its own files go — and that everyone shares the workspace."""
        if not getattr(task, "dir", ""):
            return ""
        return i18n.pick_now(
            f"\n\n[Your folder] `{task.dir}/` inside the group workspace. Use it for intermediate files "
            "and outputs without an assigned path. When the deliverable specifies a path, that path takes precedence; do not move it into this folder. Report actual paths so others can reuse them.",
            f"\n\n【你的交付目录】群工作目录下的 `{task.dir}/`。中间文件及未指定路径的产物放这里。"
            "交付要求已指定路径时，以指定路径为准，不要改放到此目录。回答写明真实路径，方便其他人接手。")

    def _notify(self, event: str, gid: str, group: dict, payload: dict) -> None:
        """Tell the observers about a round. A no-op when no hook is switched on, so the hot path
        pays nothing for a feature nobody uses."""
        if self.hooks is None or not self.hooks.any_enabled():
            return
        self.hooks.notify(event, gid, {"group_name": group.get("name", ""), **payload})

    async def _announce(self, gid: str, run: RunState) -> None:
        """Hand the finished answer to the push channels bound to this group.

        Deliberately outside the group lock and never allowed to raise: forwarding is a
        side effect of a round, and a chat platform that is slow or broken must not hold
        the group hostage or turn a good answer into a failed round.
        """
        if not self.on_answer or not run.final_text.strip():
            return
        try:
            await self.on_answer(gid, run.final_text)
        except Exception as e:  # noqa: BLE001
            print("forwarding to a chat channel failed:", e)

    # ------------------------------------------------------------- the process engineer (hidden)
    def _process_record(self, group: dict, run: RunState) -> list[str]:
        """Merge measured defects and matching re-run evidence into this group's ledger.

        Called under the group lock, including when a round is cancelled. Returns unresolved
        entries still needing a review, so a previously unavailable reviewer can be retried.
        """
        cfg = self.store.get_settings()
        if not cfg["process_autolog"]:
            return []
        workspace = self.store.workspace_dir(group["id"])
        path = proclog.unit(workspace)
        try:
            entries = proclog.read(path)
        except OSError as e:
            print("the process log could not be read:", e)
            return []
        run.defects += proclog.auto_round_defects(tool_loops=run.tool_failures)
        failed: set[str] = set()
        for d in run.defects:
            key = str(d.get("key") or "")
            if key in failed:
                continue
            failed.add(key)
            proclog.report(
                entries, title=str(d.get("title") or ""), symptom=str(d.get("symptom") or ""),
                evidence=f"round={run.round_id}; {d.get('evidence') or ''}", severity=str(d.get("severity") or "minor"),
                stage=str(d.get("stage") or "other"), hint=str(d.get("hint") or ""),
                key=key, by="auto", check=d.get("check"))
        if not entries:
            return []
        proclog.settle(entries, run.observations, failed,
                       run.process_ids if run.process_used else [], run.round_id)
        try:
            proclog.write(path, entries, group.get("name") or "")
        except OSError as e:
            print("the process log could not be written:", e)
            return []
        # No library sync here, and none anywhere else in this class: the ledger stays a file in the
        # group's workspace, which is where it is written and where anybody can read it. (It used to
        # be handed to the group's own knowledge base as well; nothing enters a library by itself any
        # more — see the note above `Library.workspace_kb`.)
        # An unavailable reviewer or incomplete answer is retried on the next group round.
        return [e.id for e in entries if e.status == "open" and (not e.cause or not e.fix)][:8]

    async def _process_review(self, group: dict, ids: list[str]) -> None:
        async with self._process_locks.setdefault(group["id"], asyncio.Lock()):
            await self._process_review_once(group, ids)

    async def _process_review_once(self, group: dict, ids: list[str]) -> None:
        """The half a program cannot do: ask a model *why*, and write the answer into the ledger.

        Silent by design — the watcher does not speak, so this pass writes `cause` and `fix` onto the
        entries and nothing else. Three limits keep it honest and affordable:

          * it reviews only unresolved entries missing a cause or correction;
          * the model is chosen the way a grading judge is (`scoring.pick_judge`): never one of the
            group's own members, local first;
          * `proclog.apply_review` only fills fields that are **empty** — a cause somebody wrote is
            never overwritten, and an answer that does not parse leaves the file exactly as it was.
        """
        if not self.store.get_settings()["process_review"]:
            return
        workspace = self.store.workspace_dir(group["id"])
        path = proclog.unit(workspace)
        try:
            entries = proclog.read(path)
        except OSError:
            return
        wanted = set(ids)
        todo = [e for e in entries if e.id in wanted and e.status == "open" and (not e.cause or not e.fix)][:8]
        if not todo:
            return
        model = ""
        try:
            model = scoring.pick_judge(self.store, self.router, group)
        except Exception:  # noqa: BLE001 — no model to ask is not an error, it is a skipped pass
            model = ""
        async def commit(raw: str, state: str, note: str = "") -> None:
            # The model ran outside the group lock. Merge into the CURRENT ledger, not its snapshot.
            async with self._lock(group["id"]):
                current = proclog.read(path)
                allowed = {e.id for e in todo}
                proclog.apply_review(current, raw, allowed=allowed,
                    by=i18n.pick_now(f"process engineer · {model}", f"流程工程师 · {model}"))
                for e in current:
                    if e.id in allowed and e.status in ("open", "fixed"):
                        e.review_state = "reviewed" if e.cause and e.fix else state
                        e.review_note = "" if e.review_state == "reviewed" else note[:400]
                proclog.write(path, current, group.get("name") or "")

        if not model:
            await commit("", "unavailable", i18n.pick_now(
                "No independent reviewer is available; rule-based feedback remains active.",
                "暂无群外可用复盘模型;规则反馈仍在生效。"))
            return
        system = templates.process_engineer(self.store).get("prompt") or ""  # type: ignore[union-attr]
        for name in (templates.process_engineer(self.store).get("skills") or []):  # type: ignore[union-attr]
            sk = tools.skill_for(name)
            if sk:
                body = i18n.pick_now(str(sk.get("body") or ""), str(sk.get("body_zh") or ""))
                if body:
                    system += "\n\n" + body
        system += "\n\n" + i18n.pick_now(
            "You are looking at the group's own process log. For each entry below, write the most "
            "likely CAUSE and one CONCRETE FIX. The fix has to be something a person can change (a "
            "setting, a prompt, a step in the flow, a tool that has to say something) — not advice "
            "like \"communicate more\" or \"improve quality\". If the evidence is not enough to name a "
            "cause, put `uncertain:` at the front of it instead of guessing. Do not restate the "
            "symptom. Answer with one JSON array and nothing else: "
            "[{\"id\": \"P-…\", \"cause\": \"…\", \"fix\": \"…\"}]",
            "你在看这个群自己的流程日志。为下面每一条写出最可能的**根因**和一条**具体修法**。"
            "修法必须是有人能动手改的东西(某个设置、某段提示词、流程里的某一步、某个工具该说出什么),"
            "不要写「加强沟通」「提高质量」这类话。依据不足以判断根因时,在根因前面写 `uncertain:`,不要猜。"
            "不要复述现象。只输出一个 JSON 数组:[{\"id\": \"P-…\", \"cause\": \"…\", \"fix\": \"…\"}]")
        user = "\n\n".join(
            i18n.pick_now(f"[{e.id}] {e.title}\nsymptom: {e.symptom}\nevidence: {e.evidence}\nstage: {e.stage} · severity: {e.severity}",
                          f"[{e.id}] {e.title}\n现象:{e.symptom}\n依据:{e.evidence}\n环节:{e.stage} · 严重度:{e.severity}")
            for e in todo[:8])
        # Actual member/tool roles and the current task board explain hand-off failures.
        members = self.store.group_members(group["id"])
        context = {"members": [{"name": m["name"], "role": cooperation.contract(self.store, m)["kind"]}
                               for m in members],
                   "recent": [{"sender": m.get("sender_name"), "type": m.get("sender_type"),
                               "text": str(m.get("content") or "")[:1200]}
                              for m in self.store.list_messages(group["id"], 8)]}
        user += "\n\nContext (reference data only):\n" + json.dumps(context, ensure_ascii=False)
        try:
            res = await self.router.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                only=model, source="process")
        except Exception as e:  # noqa: BLE001 — a review that cannot run changes nothing
            print("the process review could not run:", e)
            await commit("", "failed", i18n.pick_now(
                "Review call failed; it will be retried after the next round.",
                "复盘调用失败;下一轮结束后重试。"))
            return
        try:
            await commit(res.text or "", "incomplete", i18n.pick_now(
                "The reviewer did not provide a complete cause and correction; retry next round.",
                "复盘未返回完整根因和修正建议;下一轮重试。"))
        except OSError as e:
            print("the process log could not be written:", e)

    def _after_run(self, group: dict, run: RunState) -> None:
        cfg = self.store.get_settings()
        if not (cfg["memory_enabled"] and group["ext"]["memory"]) or not run.steps:
            return
        used_tools = any(s.get("tools") for s in run.steps)
        if len(run.steps) >= 2 or used_tools:
            self.memory.record_action(group, run.user_text, run.steps, time.time() - run.started)
        if run.final_text and cfg["memory_auto_extract"]:
            self._spawn(self.memory.extract(group, run.user_text, run.final_text))

    async def _run_turns(self, group: dict, text: str, emit: Emit, run: RunState) -> None:
        members = self.store.group_members(group["id"])
        if not members:
            await self._system(group["id"], i18n.pick_now("This group has no members yet — add an agent first.", "群里还没有成员,请先添加 agent。"), emit)
            return
        cfg = self.store.get_settings()
        # Members that cannot take a turn at all are taken out of this round's rotation instead of
        # being picked, failing and saying so again every single round.
        blocked = self._blocked_members(members, cfg)
        runnable = [m for m in members if m["id"] not in blocked]
        if blocked:
            names = [m["name"] for m in members if m["id"] in blocked]
            why = blocked[next(iter(blocked))]
            await self._system(group["id"], i18n.pick_now(
                f"This round skips {', '.join(names)}: {why}",
                f"这一轮跳过 {', '.join(names)}:{why}"), emit)
            run.defects += proclog.auto_round_defects(
                member_blocked={m["name"]: blocked[m["id"]] for m in members if m["id"] in blocked})
            if not runnable:
                await self._system(group["id"], i18n.pick_now(
                    "Every member in this group is unable to take a turn right now — fix the members above and send the message again.",
                    "这个群里每个成员现在都跑不了 —— 请先按上面的提示修好成员,再发一次。"), emit)
                return
        host = self._pick_host(group, runnable or members)

        # 本机工具成员在旁听席上:不进**自动**队列,但点名就出列(见 `_quiet_members`)。
        quiet = self._quiet_members(runnable)
        auto = [m for m in runnable if m["id"] not in quiet]
        if mentions_all(text):
            queue = deque(auto)
            explicit = True
        else:
            asked = find_mentions(text, members)
            queue = deque(m for m in asked if m["id"] not in blocked)
            # Asked-for members that were skipped still make this an explicit round: the host must not
            # quietly answer in their place, because the user addressed somebody in particular.
            #
            # ⚠️⚠️ **点名群主不算「用户指定了要谁做」** —— 群主的活就是安排团队,点他正是让他去安排。
            # 实测(用户 2026-09-25):他在群里写「@主持 看看这个问题怎么做」「后面的事情谁做@主持」,
            # 两句都因为「有人被点名」而**跳过分工** → 那一轮退回接力:群主在聊天里点几个名字,
            # **没有任务板**,于是左栏那一行写着「还没有任务」,而聊天里明明出了图、出了 30 秒的视频。
            # 点名**别人**仍然不分工(用户指定了要谁做),所以这里只把**除群主以外**的点名算作 explicit。
            others = [m for m in asked if m["id"] != host["id"]]
            explicit = bool(others)
        hops = 0
        # Keep the actual handoff separate from chat history. A tool's result cannot @mention
        # its caller, so the scheduler owns the return trip (including failed executions).
        assignments: dict[str, dict] = {}
        receipts: dict[str, list[str]] = {}
        dispatched: set[tuple[str, str, str]] = set()

        def handoff(reply: str, sender: dict) -> None:
            queued = {m["id"] for m in queue}
            for member in find_mentions(reply, members, exclude_id=sender["id"]):
                aid = member["id"]
                if aid in blocked or aid in queued:
                    continue
                if aid in quiet:
                    signature = (sender["id"], aid, reply.strip())
                    if signature in dispatched:
                        continue
                    dispatched.add(signature)
                    assignments[aid] = {"caller": sender, "instruction": _tool_instruction(reply, members, member)}
                queue.append(member)
                queued.add(aid)

        mode = group["ext"]["plan"] if group["ext"]["plan"] != "inherit" else cfg["plan_mode"]
        # Planning is the host's job, and only a member that can hold a conversation can do it — an
        # external agent has its own tools, and a generating member has no judgement to add.
        if (not explicit and mode != "off" and len(members) >= 2 and int(cfg["plan_max_tasks"]) >= 2
                and media.may_host(host)):
            hops = 1
            out = await self._planning_turn(group, members, host, text, mode, emit, run)
            if out is None:
                return
            # ⚠️ 群主这一回合**已经被分工回合用掉了**。用户点名群主时他本来就在队列里,不去掉就会在
            # 计划之后再讲一遍(群里出现两条群主发言:一条开场、一条重复)。
            queue = deque(m for m in queue if m["id"] != host["id"])
            if out is not True:  # True = execution followed the plan; otherwise out is the owner's ordinary reply and hands off
                run.final_text = out.text
                handoff(out.text, host)
            else:
                return
        elif not queue and not explicit:
            queue.append(host)

        # How many turns one message may cause. The setting is the user's number, but it is floored at
        # one full round of the roster, because a cap smaller than the group cannot fit a round at all:
        # measured on a real fourteen-member group at the old default of 8, the members at the end of
        # the list were paused *every* round — "reached the limit of 8 turns, so WorkBuddy, 小助 was
        # paused" — and the steps they owned could never be reached, however many messages were sent.
        # A limit that makes a round impossible is not a limit, it is a truncation. The round can still
        # be stopped by hand (`POST /api/groups/{gid}/stop`), so nothing is taken away from the user.
        max_hops = max(int(cfg["max_hops"]), len(members) + 1)
        while queue and hops < max_hops:
            agent = queue.popleft()
            hops += 1
            assignment = assignments.pop(agent["id"], None)
            returned = receipts.pop(agent["id"], [])
            extra = assignment["instruction"] if assignment else None
            if returned:
                extra = i18n.pick_now(
                    "[Executor returns] Resume the work you assigned. Check these results and actual file paths, then finish or hand off the next distinct task. Report failures accurately; do not repeat the same generation or @mention completed tools in a summary.\n",
                    "【工具交回】继续处理你派出的任务。核对以下结果和实际文件路径,然后交付或派出下一项不同的任务。如实处理失败;不要重复同一次生成,总结时不要再 @ 已完成的工具。\n") + "\n".join(returned)
            run.last_error = ""
            out = await self._agent_turn(group, agent, members, emit, run, extra_user=extra)
            if assignment:
                caller = assignment["caller"]
                calls = ((out.message.get("meta") or {}).get("tools") or []) if out else []
                files = [f for call in calls if call.get("status") == "ok" for f in call.get("files", [])]
                result = {"member": agent["name"], "result": out.text if out else run.last_error or "Execution failed",
                          "files": files, "tools": [{"name": c.get("name"), "status": c.get("status")} for c in calls]}
                receipts.setdefault(caller["id"], []).append(json.dumps(result, ensure_ascii=False))
                if caller["id"] not in {a["id"] for a in queue}:
                    queue.append(caller)
            if out is None:
                continue
            run.final_text = out.text
            if agent["id"] in quiet:
                continue  # tool output and echoed instructions are never a new delegation
            if agent.get("engine") and not (agent.get("engine_cfg") or {}).get("handoff", True):
                continue   # this external agent is configured not to hand off: the @ in its reply is just text
            if (agent.get("origin") or "") == media.MEDIA_ORIGIN:
                # A generating member does not hand off either, and for a stronger reason: the text
                # it "said" was written by this app, not by a model that read the group — so an `@`
                # in it would not be an act of delegation at all.
                continue
            handoff(out.text, agent)
        if queue:
            names = i18n.pick_now(", ", "、").join(a["name"] for a in queue)
            await self._system(
                group["id"], i18n.pick_now(f"Reached the limit of {max_hops} turns for one message, so {names} was paused. Send another message to carry on.", f"已达到单次最大发言轮数({max_hops}),{names} 的发言被暂停。可再发一条消息继续。"), emit
            )
            # The process log keeps this one: a round that stopped because it ran out of turns still
            # has work in it, and the tasks it left unfinished are what the next round inherits. The
            # note above tells the user *now*; the entry is what makes it countable a week later.
            run.defects += proclog.auto_round_defects(
                exhausted=True, open_tasks=list(run.unfinished), tool_loops=run.tool_failures)

    # ------------------------------------------------------------- who can run
    @staticmethod
    def _quiet_members(members: list[dict]) -> set[str]:
        """本机工具成员:**在群里,但不主动发言**。

        用户 2026-09-25 的原话:「工具可以不发言,作为旁听,但是群员可以随时调用这些工具的」。
        这不是 bug 修补,是设计:一个渲染器/合成器没有话要说,它只产出文件 —— 让它进对话队列,
        就等于每轮请一个只会交文件的成员讲一段话,而它十条里有九条在报「我跑不起来」
        (实测:Remotion/HyperFrames/video-shotcraft 环境没配好时**每轮必败**,其中一次跑了 306 秒)。

        ⚠️ 「不主动」不等于「叫不动」,所以这只是把 **自动队列** 里的它拿掉:
          * 消息里 **@它** → 照常跑(用户点了名);
          * 主持把它**排进任务板** → 照常跑(`_execute_plan` 只看 `blocked`,不看这个);
          * 名册、@ 候选、成员栏 → 照旧看得见。
        """
        return {m["id"] for m in members if cooperation.is_listener(m)}

    def _blocked_members(self, members: list[dict], cfg: dict) -> dict[str, str]:
        """Members that cannot take a turn at all, with the reason — measured before the round.

        An external agent whose settings do not comply (no model picked, or the engine's own config
        rejected) fails identically every time it is picked: it burns a speaking slot and posts the
        same line into the chat, once per round, forever. Nothing in the round changes that, so it is
        not handed a turn at all — the reason is stated once and the plan marks its tasks `skipped`.

        Only *configuration* problems are caught here. A model that times out, a key that is out of
        quota, a tool that fails: those are runtime failures and still get their turn, because the
        next attempt may well work.
        """
        out: dict[str, str] = {}
        for m in members:
            if not m.get("engine"):
                continue
            if not cfg["external_agents_enabled"]:
                out[m["id"]] = i18n.pick_now("the external-agent master switch is off",
                                             "外部智能体总开关是关着的")
                continue
            try:
                external.clean_cfg(m.get("engine_cfg"), engine=str(m.get("engine") or "workbuddy"))
            except ValueError as e:
                out[m["id"]] = i18n.pick_now(f"external-agent settings are not valid: {e}",
                                             f"外部智能体设置不合规:{e}")
        return out

    @staticmethod
    def _pending_reviews(task: "planner.PlanTask", message: dict, workspace: Path) -> list[str]:
        pending = []
        calls = _delivery_tools(message)
        root = workspace.resolve()
        def paths(call):
            args = call.get("args") or {}
            raw = args.get("paths") or [args.get("path")]
            return [p for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []
        for review in sorted(set(planner.REVIEW_TOOLS).intersection(task.tools)):
            latest = {}
            for call in calls:
                if call.get("name") == review:
                    args = call.get("args") or {}
                    key = json.dumps(args.get("paths") or args.get("path") or "", ensure_ascii=False, sort_keys=True)
                    latest[key] = call
            checked = {(root / p).resolve() for c in latest.values() if c.get("status") == "ok" for p in paths(c)}
            extensions = ({".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"}
                          if review == "review_picture" else {".wav", ".mp3", ".m4a", ".mp4", ".mov", ".webm"})
            produced = {(root / str(f.get("path") or f.get("rel") or f.get("name") or "")).resolve()
                        for c in calls if c.get("status") == "ok" and c.get("name") in {
                            "generate_image", "generate_video", "assemble_video", "make_figure", "make_animation", "synthesize_speech"}
                        for f in (c.get("files") or []) if isinstance(f, dict)}
            produced = {p for p in produced if p.is_relative_to(root) and p.is_file() and p.suffix.lower() in extensions}
            def blocks(call):
                if call.get("status") == "ok":
                    return False
                # A typo naming a nonexistent extra file is not a failed review
                # of the delivered media. Keep its trace, but only disregard it
                # once ALL actual media outputs have successful review receipts.
                raw = paths(call)
                typo = (call.get("status") == "failed" and raw and produced and produced <= checked
                        and all("/" in p and not Path(p).is_absolute() and ".." not in Path(p).parts
                                and (root / p).resolve().is_relative_to(root) and not (root / p).exists() for p in raw))
                return not typo
            if not latest or any(blocks(c) for c in latest.values()):
                pending.append(review)
        return pending

    def _task_shortfall(self, task: "planner.PlanTask", message: dict, workspace: Path, *, check_reviews: bool = True) -> str:
        """Require reported, nonempty workspace files of the promised types and counts.

        Renaming is allowed (report-v2.docx can satisfy report.docx); a successful search
        cannot satisfy a promised video, and an empty file is not a delivery.
        """
        interrupted = (message.get("meta") or {}).get("execution_error")
        if interrupted:
            return str(interrupted)
        # ⚠️ A message the program has already stamped as undelivered must not be re-judged from its
        # own text: `_stamp_failure` quotes the missing file's name, and the engine branch below
        # reads file names **out of the content** — so the verdict would end up arguing for the
        # claim it was written to contradict.
        verdict = (message.get("meta") or {}).get("delivery_verdict")
        if verdict:
            return str(verdict)
        calls = [c for c in _delivery_tools(message) if c.get("status") != "running"]
        ok = [c for c in calls if c.get("status") == "ok"]
        # Rendering proves that bytes exist, not that the requested review ran.
        # A failed visual/audio check must not release a dependent assembly task.
        pending = self._pending_reviews(task, message, workspace) if check_reviews else []
        if pending:
            return i18n.pick_now(
                f"required verification {pending[0]} did not succeed; generated media still needs review.",
                f"要求的验收 {pending[0]} 未成功，生成的媒体仍待核验。")
        named = _FILE_IN_PLAN(task.deliverable)
        if named:
            from collections import Counter
            reported = [_file_label(f) for call in ok for f in call.get("files", []) if isinstance(f, dict)]
            if (message.get("meta") or {}).get("engine") and not (message.get("meta") or {}).get("denied"):
                reported += _FILE_IN_PLAN(message.get("content"))
            actual: set[Path] = set()
            root = workspace.resolve()
            for name in reported:
                if not name:
                    continue
                candidates = [workspace / name]
                if "/" not in name and "\\" not in name:
                    candidates += list(workspace.rglob(name))
                for path in candidates:
                    try:
                        real = path.resolve()
                        if real.is_relative_to(root) and real.is_file() and real.stat().st_size > 0:
                            actual.add(real)
                    except OSError:
                        continue
            expected = Counter(Path(n).suffix.lower() for n in named)
            available = Counter(p.suffix.lower() for p in actual)
            if any(available[k] < count for k, count in expected.items()):
                return i18n.pick_now(
                    f"the deliverable ({', '.join(named[:3])}) was never written: its reported files are missing, empty or of the wrong type. A successful search or a text reply is not a file delivery.",
                    f"交付物({', '.join(named[:3])})没有落盘:本次报告的文件不存在、为空或类型/数量不符。搜索成功或聊天回复不能代替文件交付。")
        if calls and not ok:
            why = ", ".join(f"{c.get('name')}({c.get('status')})" for c in calls[:4])
            return i18n.pick_now(f"every tool call failed: {why}", f"工具调用全都失败了:{why}")
        return ""

    async def _stamp_failure(self, message: dict, reason: str, emit: Emit) -> str:
        """Put the program's verdict **onto the claim**, and store it there.

        A task that did not deliver leaves its member's prose in the transcript, and that prose is
        confident, detailed and wrong:

            ## t1 真实交付：视觉规范 v2.3 已落盘
            **交付物**：`tasks/f4feec93da49/分镜表v2/视觉规范-v2.3.md`

        Measured 2026-09-26 (group 介入术式科): the file did not exist — the member had made **zero
        tool calls**, twice — and the host, planning the next round, read exactly that paragraph and
        built on a file that was never written. The task board *said* `t1 [failed]`; the host still did
        it, because the claim and the verdict lived in different messages and only the claim read like
        an answer. So the verdict is attached to the claim: prefixed (so a history clip cannot lose
        it), stored (so the next round's planning reads it), and emitted (so the user sees both).

        Idempotent through `meta.delivery_verdict`, not through the text — a message is judged once,
        but a retry re-reads it, and matching on a translated sentence would be a trap.
        """
        meta = dict(message.get("meta") or {})
        if meta.get("delivery_verdict"):
            return message.get("content") or ""
        calls = _delivery_tools(message)
        head = i18n.pick_now(
            f"⚠️ Verified by the program: **this task did NOT deliver.** {reason}\n"
            f"(Tool calls this turn: {len(calls)}. Anything below that says a file was written, read or "
            "verified is **unverified** — do not use it as an existing artefact.)\n\n",
            f"⚠️ 程序核验:**本任务未交付** —— {reason}\n"
            f"(本回合工具调用 {len(calls)} 次。下面正文里任何「已落盘 / 已读完 / 已核验」都**未经工具证实**,"
            "**不得当作既成事实引用**。)\n\n")
        stamped = head + (message.get("content") or "")
        meta["delivery_verdict"] = reason[:400]
        try:
            fixed = self.store.update_message(message["id"], content=stamped, meta=meta)
        except Exception:  # noqa: BLE001 — a stamp that cannot be stored still travels this round
            return stamped
        message["content"] = stamped
        message["meta"] = meta
        if fixed:
            await emit({"type": "message_end", "message": fixed})
        return stamped

    @staticmethod
    def _pick_host(group: dict, members: list[dict]) -> dict:
        """The owner must be a member that can hold a conversation: an external agent does not
        delegate through the <plan> protocol, and a generating member has nothing to decide — so
        neither should be handed the job of deciding what the others do.

        The rule itself is `media.may_host`, asked rather than restated: the API refuses to *give*
        one of them the chair, and a second copy here would be free to drift away from that answer.
        """
        host = next((m for m in members if m["id"] == group.get("host_agent_id")), None)
        if host is None or not media.may_host(host):
            host = next((m for m in members if media.may_host(m)), host or members[0])
        return host

    # ------------------------------------------------------------------- plan
    async def _planning_turn(
        self, group: dict, members: list[dict], host: dict, text: str, mode: str, emit: Emit, run: RunState
    ) -> "TurnOut | bool | None":
        cfg = self.store.get_settings()
        past = ""
        if cfg["memory_enabled"] and group["ext"]["memory"]:
            acts = [m for m in self.memory.recall(group["id"], host["id"], text, 8) if m["kind"] == "action"][:3]
            past = "\n".join(f"- {m['content']}" for m in acts)
        instruction = planner.planning_instruction(int(cfg["plan_max_tasks"]), mode, past,
                                                   planner.owner_list(members))
        capabilities: dict[str, set[str]] = {}
        capability_lines = []
        host_execution_tools: set[str] = set()
        for member in members:
            context = await self.toolhub.context(group, member, connect=False, read_only=run.read_only)
            capabilities[member["id"]] = set(context.tools)
            if member["id"] == host["id"]:
                _, deferred = planning_tools(context.tools)
                host_execution_tools = set(deferred)
            worker = cooperation.contract(self.store, member)
            if worker["kind"] == "local_tool":
                capabilities[member["id"]] = set(worker["tools"]) if not run.read_only and cfg["external_agents_enabled"] else set()
                detail = ", ".join(worker["tools"]) + "; " + worker["summary"] + " " + worker["preparation"]
            elif member.get("engine"):
                detail = i18n.pick_now("external engine: its own tools only, not these app tools",
                                        "外部引擎:只能使用自身工具,不能直接调用本程序的内置工具")
            else:
                detail = ", ".join(sorted(context.tools)) or i18n.pick_now("no available tools", "无可用工具")
            capability_lines.append(f"- {member['name']}: {detail}")
        capability_text = "\n".join(capability_lines)
        instruction += "\n" + i18n.pick_now("Executable capabilities:\n", "实际可执行能力:\n") + capability_text
        if cfg.get("host_auto_recruit") and not run.read_only:
            instruction += i18n.pick_now(
                "\nIf a required skill or tool is missing, use find_team_resources and invite_team_resource before writing the plan. Recruit only for a specific gap, reuse existing members, and name each new member's input, job and acceptance check. Do not recruit duplicate roles or treat installed tools as already tested. The returned roster supersedes this snapshot.",
                "\n缺少能力或工具时，先用find_team_resources查找、invite_team_resource拉入，再给出分工。只针对具体缺口补人，复用已有成员；写明新增成员的输入、职责和验收。不要重复拉同类角色，不把已配置当已验证。工具返回的新名册优先于本段旧快照。")
        instruction += i18n.pick_now(
            "\nFor a generating member, assign one image or one clip per task and put tool parameters in arguments (prompt, seconds, aspect_ratio, first_frame, size, reference_images). Image references must be explicit workspace paths in reference_images and require an OpenAI-compatible image editing endpoint; the MetaChat media adapter does not upload local references. Multi-shot production and final assembly belong to a chat member with the corresponding tools. Reuse actual upstream file paths; do not invent a public URL for a local file.",
            "\n生成成员每个任务只生成一张图或一个片段,工具参数写进 arguments(prompt、seconds、aspect_ratio、first_frame、size、reference_images)。绘图参考图须在 reference_images 中明确填写本群路径，需 OpenAI 兼容图片编辑接口；MetaChat 媒体适配器尚未上传本地参考图。多镜头批量制作和成片装配交给具备相应工具的对话成员。接力使用上游实际文件路径,不得把本地文件假装成公网地址。")
        # Snapshot before the turn: the point is whether *this* turn's tools broke, not how many
        # times some member failed earlier in the round.
        failures_before = sum(run.tool_failures.values())
        out = await self._agent_turn(
            group, host, members, emit, run, extra_user=instruction,
            extra_meta={"phase": "planning"},
            empty_fallback=plan_fallback(),
        )
        if out is None:
            return None
        if cfg.get("host_auto_recruit"):
            group.update(self.store.get_group(group["id"]) or {})
            members[:] = self.store.group_members(group["id"])
            for member in members:
                context = await self.toolhub.context(group, member, connect=False, read_only=run.read_only)
                worker = cooperation.contract(self.store, member)
                capabilities[member["id"]] = (set(worker["tools"]) if worker["kind"] == "local_tool"
                    and not run.read_only and cfg["external_agents_enabled"] else set(context.tools))
            capability_text = "\n".join(f"- {m['name']}: {', '.join(sorted(capabilities[m['id']]))}" for m in members)
        # A host turn whose tool calls were all rejected is not a decision to answer — it is a
        # damaged turn. Measured: the host opened four calls, none closed its tag, the parser read
        # them as one malformed call, and the host then wrote a paragraph saying so ("工具调用格式
        # 出问题,我重新发一次") and stopped mid-thought. It had been about to lay out the work and
        # never got there. Asking once for just the plan recovers the round; without this the round
        # ended after a single message that produced nothing, and the user had to prod by hand —
        # which is exactly what "the flow does not run" looked like from outside.
        damaged = sum(run.tool_failures.values()) > failures_before
        # The owner already connected MCP this round; unknown tool names in the plan are simply
        # ignored rather than treated as an error. The same restriction the members' own tool lists
        # get: on a read-only round the host must not plan work that needs run_code or another exec
        # tool, or it would hand out tasks whose tool has already been withheld.
        known = set().union(*capabilities.values())

        def parse_and_build(raw: str) -> tuple["planner.Plan | None", str]:
            """(plan, why-it-failed). Both halves of "the plan did not run": JSON that does not parse,
            and a plan that parses but is not valid (unknown owner, dependency loop, too many tasks)."""
            try:
                obj = planner.extract_plan_json(raw)
            except planner.PlanError as e:
                return None, str(e)
            if obj is None:
                return None, i18n.pick_now("there was no plan", "没有给出计划")
            try:
                return planner.build_plan(obj, members, int(cfg["plan_max_tasks"]), known, capabilities), ""
            except planner.PlanError as e:
                return None, str(e)

        plan, why = parse_and_build(out.raw)
        if plan is not None:
            await self._execute_plan(group, members, host, plan, run, emit)
            return True
        missing = i18n.pick_now("there was no plan", "没有给出计划")
        if why == missing and mode != "on" and any(
            c.get("name") in host_execution_tools
            for c in (out.message.get("meta") or {}).get("tools", [])
        ):
            # Auto mode also serves ordinary one-step requests. Production calls
            # were deferred, not refused by the user: end planning explicitly
            # and give that single step one normal execution turn.
            return await self._agent_turn(group, host, members, emit, run,
                extra_user=i18n.pick_now(
                    "Planning is over. No team plan was chosen. Execute the user's original single-step request now using the available tools, then report the actual result. Do not output a plan.",
                    "规划已结束，本次没有采用团队计划。现在执行用户原始的单步请求，调用实际工具后报告真实结果，不再输出计划。"),
                extra_meta={"phase": "execution"}, empty_fallback=plan_fallback())
        if why == missing and mode != "on" and not damaged:
            # The host decided to answer on its own, which is allowed in every mode but "always split".
            return out
        # One hidden retry before giving up, because this is the commonest way a task dies before it
        # starts: the host writes the thinking and the plan, the JSON has one wrong bracket, the plan
        # is dropped and the round falls back to turn-taking — nobody is assigned, nothing is produced
        # and the chat shows only "the plan was malformed". Live evidence on this machine: an
        # asked-for 报告.docx ended as a single chat message with an empty workspace. The retry costs
        # one short call and the model has already done the thinking.
        fixed = await self._repair_plan(host, members, why, original=out.raw, request=text,
                                        capabilities=capability_text, failed_model=out.message.get("model_id") or "")
        if fixed:
            plan, why2 = parse_and_build(fixed)
            if plan is not None:
                await self._execute_plan(group, members, host, plan, run, emit)
                return True
            # A narrow instruction is sometimes answered with something unrelated ("好的"): the
            # *first* reason is still the true one, and replacing it with "there was no plan" would
            # both hide what actually went wrong and silence the notice below.
            if why2 and why2 != missing:
                why = why2
        if why == missing:
            if mode == "on":
                await self._system(group["id"], i18n.pick_now("This group is set to always split the work, but the host produced no plan; treating its reply as an ordinary answer.", "本群设为「总是先分工」,但群主没有给出计划,已按普通回复处理。"), emit)
            elif damaged:
                # Nothing was delegated and nothing was produced, so silence here is the worst
                # option: from the chat it looks like the group simply refused to work. The reason
                # is a fact this round measured, not a guess, and the fix is usually one word from
                # the user — so it is said out loud, next to what it cost.
                await self._system(group["id"], i18n.pick_now(
                    f"{host['name']} tried to look things up before splitting the work, but its tool "
                    "calls failed (see the calls above), so no plan came out of this turn and nothing "
                    "was assigned. Send the message again to give it another turn; if the same tool "
                    "keeps failing, fix that tool first.",
                    f"{host['name']} 这一轮先查了资料想再分工,但它的工具调用失败了(见上面那几次调用),"
                    "所以没给出计划、也没有派活。再发一条消息让它重来一次;"
                    "如果同一个工具反复失败,请先修好那个工具。"), emit)
                run.defects += proclog.auto_round_defects(
                    invalid_plan=i18n.pick_now(
                        "the host's tools failed before it could split the work, so the round ended "
                        "with nothing assigned",
                        "群主在分工之前工具就失败了,于是这一轮什么都没派出去"),
                    tool_loops=run.tool_failures)
            return out
        malformed = "JSON" in why or "no tasks array" in why or "tasks 数组" in why or "没有给出计划" in why
        # ⚠️ 这一句里「已改用普通接力模式」是**不准确的**:计划被拒时 queue 里没有别人、host 也已被移出,
        # 于是这一轮**什么都没派出去**(2026-09-26 实测:视频群连丢两轮,群里只剩这一条系统提示)。
        # 后半句把真实代价说出来,否则用户以为团队在普通模式下干活。
        await self._plan_failed(group["id"], out, i18n.pick_now(
            f"The host's plan was {'malformed' if malformed else 'not valid'} ({why}); falling back to "
            "ordinary turn-taking, in which nobody was assigned this round — send the message again to "
            "give the host another turn.",
            f"群主的分工计划{'格式不对' if malformed else '不合规'}({why}),已改用普通接力模式。"
            "本轮实际没有派出去任何任务,再发一条消息让群主重排。")
            + _plan_excerpt(out.raw, focus=_bad_task_id(why)), emit)
        # The process log wants this one: a plan nobody could use means the round fell back to plain
        # turn-taking, which is the commonest way a request ends with nothing to show for it. The
        # reason travels with it, because "malformed" and "named a member who is not here" need
        # different repairs.
        run.defects += proclog.auto_round_defects(invalid_plan=why)
        return out

    async def _repair_plan(self, host: dict, members: list[dict], why: str, *, original: str = "",
                           request: str = "", capabilities: str = "", failed_model: str = "") -> str:
        """One hidden retry for a plan that did not parse.

        Deliberately not a normal turn: nothing is emitted, so a repair that works leaves the chat
        exactly as a working plan would (the host's reasoning, then the task board) and a repair that
        fails leaves the round where it was — the caller posts the same "falling back" notice. The
        context is built fresh and narrow, because the model has already done the deciding: what is
        wanted is the same plan in a shape that parses, and a fresh context is both cheaper and less
        likely to repeat whatever broke the JSON the first time.
        """
        system = i18n.pick_now(
            "You are the host of a multi-member AI group. You output the delegation plan as strict JSON "
            "and nothing else.",
            "你是一个多成员 AI 群的群主。你只输出严格 JSON 格式的分工计划,不输出别的。")
        user = (i18n.pick_now(f"The plan you gave was rejected: {why}.", f"你给出的计划被拒了:{why}.")
                + "\n"
                + i18n.pick_now(
                    "Answer with the plan again, and nothing else: a JSON object inside <plan>...</plan>, "
                    "with a \"tasks\" array. No explanation, no apology, no prose repetition. Keep the "
                    "same requested outcome, correcting incompatible assignments.",
                    "请只重新输出计划:把 JSON 对象放在 <plan>…</plan> 里,并带 \"tasks\" 数组。"
                    "不要解释、不要道歉、不要用文字复述。保留交付目标,纠正不合适的分工。")
                + "\n"
                + i18n.pick_now(f"Members you may assign to (owner must be one of these, exactly as written): "
                                f"{planner.owner_list(members)}",
                                f"可以指派的成员(owner 必须原样写成下面之一):{planner.owner_list(members)}"))
        user += (f"\nOriginal request / 原始需求:\n{request[:6000]}\n"
                 f"Original plan / 待修复计划:\n{original[:20000]}\n"
                 f"Capabilities / 可执行能力:\n{capabilities}\n"
                 "Preserve the requested deliverables. Correct any incompatible assignment. / "
                 "保留原始交付目标,修正能力不匹配的分工。")
        user += ("\nExact schema / 严格结构: <plan>{\"goal\":\"...\",\"conventions\":\"...\","
                 "\"tasks\":[{\"id\":\"t1\",\"owner\":\"EXACT MEMBER NAME\",\"title\":\"...\","
                 "\"instruction\":\"...\",\"needs\":[],\"strengths\":[],\"tools\":[],"
                 "\"deliverable\":\"result.md\",\"arguments\":{}}]}</plan>\n"
                 f"At most / 最多 {int(self.store.get_settings()['plan_max_tasks'])} tasks. "
                 "Use owner, never assignee/agent. Dependencies are success-only; no conditional standby "
                 "tasks or when/condition/on_failure/fallback_for fields. Generator tasks need an explicit "
                 "arguments.prompt. / 负责人字段必须为owner；依赖仅表示上游成功，不支持条件备用；"
                 "生成任务须明确arguments.prompt。")
        preferred = host.get("model_id")
        options = {}
        if failed_model and self.store.get_settings().get("route_auto_match"):
            alternatives = [m["id"] for m in self.router.rank_by_tags(["reasoning", "tool-use"], 8)
                            if m["id"] != failed_model]
            if alternatives:
                preferred = alternatives[0]
                options["allowed_ids"] = alternatives
        try:
            res = await self.router.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                preferred=preferred, tags=["reasoning", "tool-use"], source="plan", max_tokens=8192, **options)
        except Exception:  # noqa: BLE001 — a repair that cannot run leaves the old behaviour in place
            return ""
        return res.text or ""

    async def _execute_plan(
        self, group: dict, members: list[dict], host: dict, plan: planner.Plan, run: RunState, emit: Emit
    ) -> None:
        gid = group["id"]
        plan.integration = {"status": "pending", "message_id": "", "error": ""}
        by_id = {m["id"]: m for m in members}
        outputs: dict[str, str] = {}
        blocked = self._blocked_members(members, self.store.get_settings())
        workspace = self.store.workspace_dir(gid)

        async def push() -> None:
            m = self.store.update_message(plan.message_id, content=planner.summarize(plan), meta=plan.to_meta())
            await emit({"type": "plan", "message": m})

        # ⚠️ The board's own first emit belongs **inside** this try, and so does the `add_message`
        # that precedes it. The board is written to the database with `status="running"` and every
        # task `pending` before that await, while the two handlers that rewrite running/pending into
        # stopped/skipped live at the bottom of this try. A cancel landing on that first emit used to
        # escape both, leaving a board that says "in progress" for ever: `planner.headline`'s `live`
        # true for that group until some later round happened to produce another board, and nothing
        # anywhere recording that the round was stopped (measured 2026-09-27).
        try:
            pm = self.store.add_message(gid, "plan", None, i18n.pick_now("Task board", "任务板"), planner.summarize(plan), meta=plan.to_meta())
            plan.message_id = pm["id"]
            await emit({"type": "message", "message": pm})
            for i, task in enumerate(plan.tasks, 1):
                if task.status == "skipped" and task.error:
                    await push()  # unavailable verification does not stop unrelated production
                    continue
                blocked_by = [n for n in task.needs if plan.by_id(n) is None or plan.by_id(n).status != "done"]
                if blocked_by:
                    # Name *what* is missing, not only which task IDs failed to produce it. The
                    # ledger's own words for why a 180-second film never existed: "装配任务被建成
                    # 最下游的串行节点,只有全部素材齐备才启动;素材不全时它既不产出部分成片,也不留下
                    # 『缺哪几段』的中间记录,成片因此彻底不存在" — and the same for the acceptance
                    # note, so nothing in the round said what was missing. The board is where that
                    # record has to be, because it is the only artefact every round leaves behind.
                    gone = []
                    for n in blocked_by:
                        up = plan.by_id(n)
                        what = f"（缺 {up.deliverable}）" if up and up.deliverable else ""
                        gone.append(f"{n} {up.title}{what}" if up else n)
                    task.status = "skipped"
                    task.error = i18n.pick_now(
                        "Upstream tasks did not finish, so this one has nothing to work from: "
                        + "; ".join(gone),
                        "上游任务没完成,这一步没有可用的输入:" + "; ".join(gone))
                    await push()
                    continue
                agent = by_id.get(task.owner_id)
                if agent is None:
                    task.status, task.error = "failed", i18n.pick_now("the member has left the group", "成员已不在群里")
                    await push()
                    continue
                if agent["id"] in blocked:
                    # Handing the task over would only produce the same failure once more. The board
                    # says why it was never attempted, which is the thing the user has to fix.
                    task.status = "skipped"
                    task.error = i18n.pick_now(f"{agent['name']} cannot take a turn: {blocked[agent['id']]}",
                                               f"{agent['name']} 这一轮跑不了:{blocked[agent['id']]}")
                    await push()
                    continue
                task.dir = await asyncio.to_thread(self._task_dir, group, task, plan.message_id)
                task.status = "running"
                await push()
                out = await self._agent_turn(
                    group, agent, members, emit, run,
                    extra_user=planner.task_prompt(plan, task, outputs, i) + self._task_dir_note(task),
                    exclude_plan_id=plan.message_id,
                    extra_meta={"plan_id": plan.message_id, "task_id": task.id, "task_title": task.title,
                                "routing_tags": task.strengths,
                                "task_instruction": task.instruction,
                                "arguments": task.arguments,
                                "input_files": [f for n in task.needs if plan.by_id(n)
                                                for f in plan.by_id(n).artifacts]},
                )
                if out is None:
                    # The reason the piece that failed actually gave, not a generic one. A local tool
                    # refuses with a sentence its own table states ("it renders a Remotion project,
                    # and there is nothing like that in its folder yet"), an external agent with what
                    # its settings are missing — and the board used to overwrite every one of them
                    # with the same fixed line, so a user could see that a task had failed and never
                    # learn the one thing that would fix it. The generic wording is the fallback for
                    # the cases where nothing recorded a reason at all (a turn that was cancelled).
                    task.status = "failed"
                    task.error = run.last_error or i18n.pick_now("the model call failed", "模型调用失败")
                else:
                    reason = self._task_shortfall(task, out.message, workspace)
                    denied = any(c.get("status") == "denied"
                                 for c in ((out.message.get("meta") or {}).get("tools") or []))
                    if (reason and not denied and not (out.message.get("meta") or {}).get("execution_error")
                            and agent.get("origin") != media.MEDIA_ORIGIN
                            and _FILE_IN_PLAN(task.deliverable)):
                        review_only = bool(self._pending_reviews(task, out.message, workspace)) and not self._task_shortfall(
                            task, out.message, workspace, check_reviews=False)
                        # The deliverable names a file and the file is not there. Almost always the
                        # same shape: the member wrote the script / the report / the caption into the
                        # chat instead of calling the tool that writes it. One day's ledger carries
                        # it three times — 科普脚本.md, 中秋散文（配图）.docx, 中秋细节核查.md — and
                        # each time the task failed and everything downstream was skipped, so a
                        # 180-second film became a paragraph explaining why there is no film.
                        #
                        # So: one narrow second turn, asked for the file and nothing else. It is the
                        # same bargain as the plan repair above — the thinking is already done, and a
                        # delivery recovered is worth far more than an accurate failure notice.
                        # Bounded to one, and skipped entirely when the user *refused* a tool call (a
                        # `denied` call means the retry would only raise the same prompt again).
                        nudge = i18n.pick_now(
                            f"\n\n⚠️ The delivery {task.deliverable} did not pass verification: {reason}. Writing "
                            "the content in the chat does not count: this task is finished only when "
                            "the file is in the group's working directory. Call the tool that writes "
                            "it now (`write_document` for a document, a deck, a workbook or a text "
                            "file) with the content you already produced, then answer with the file's "
                            "path in one line. Do not repeat the content.",
                            f"\n\n⚠️ 交付物 {task.deliverable} 未通过核验:{reason}。把内容写在聊天里不算交付 —— "
                            "只有当文件真的落在本群工作目录里,这项任务才算完成。"
                            "现在就用你已经写好的内容调用写文件的工具(`write_document` 可写文档、"
                            "演示、表格或文本文件),然后用一行回答给出文件路径。不要重复内容。")
                        if review_only:
                            nudge = i18n.pick_now(
                                "\n\nThe promised files already exist. Only required verification is missing: "
                                + reason + ". Inspect the existing outputs using the required review tools. "
                                "Do not regenerate, reassemble or synthesize anything. Keep actual paths and report unresolved defects.",
                                "\n\n承诺的文件已真实落盘，只缺必需的验收：" + reason
                                + "。使用要求的验收工具检查已有产物，禁止重新生成、装配或合成。沿用真实路径，如实记录仍未解决的问题。")
                        # ⚠️⚠️ A member that called **no tools at all** must be told so in words. It
                        # cannot be left to infer it from an empty evidence list, and it must not be
                        # handed its own paragraph back as something to build on. Measured 2026-09-26
                        # (group 介入术式科, member Project manager): two turns in a row wrote 「hyperframes
                        # 入口 SKILL.md 已读完」 and 「本轮仅一次 write_document（.md）把视觉规范 v2.3 八节
                        # 内容写进交付路径」 with `meta.tools == []` — nothing was ever read or written —
                        # and the retry fed that same paragraph back labelled 「沿用，不要重新检索」, which
                        # reinforced the claim instead of correcting it.
                        calls_before = _delivery_tools(out.message)
                        silent = not calls_before and not (out.message.get("meta") or {}).get("tools")
                        if silent:
                            nudge += i18n.pick_now(
                                "\n\n⚠️⚠️ Verified by the program: **you made no tool calls at all this "
                                "turn.** The tool log for your previous answer is empty, so every "
                                "sentence of it that says something was read, searched or written is "
                                "false — you only wrote those words. Answering in prose again will fail "
                                "exactly the same way. **Call the tool first, then quote what it "
                                "returned.**",
                                "\n\n⚠️⚠️ 程序核验:**你上一轮一次工具调用都没有发生** —— 你上一条回答的工具记录是"
                                "空的,所以那里面任何「已读完 / 已检索 / 已落盘 / 已调用」都不是事实,你只是写了"
                                "那些话。再用文字回答一次会以完全同样的方式失败。"
                                "**先调用工具,再引用它返回的结果。**")
                        # ⚠️ The label matters as much as the content: 「沿用，不要重新检索」 is an
                        # instruction, and giving it for a turn that produced nothing tells the member
                        # to keep its unverified prose.
                        previous = (i18n.pick_now(
                                        "\n\nPrevious attempt / your last answer (⚠️ nothing was "
                                        "written to disk — it is material to be written out, not a "
                                        "finished deliverable):\n",
                                        "\n\n上次正文（⚠️ 未落盘,不能当作已完成,只能当素材重新落盘）:\n")
                                    if silent else
                                    i18n.pick_now(
                                        "\n\nPrevious attempt / material already prepared "
                                        "(reuse it, do not search again):\n",
                                        "\n\n上次已整理内容（沿用,不要重新检索）:\n"))
                        again = await self._agent_turn(
                            group, agent, members, emit, run,
                            extra_user=planner.task_prompt(plan, task, outputs, i)
                                       + self._task_dir_note(task) + nudge + previous
                                       + out.raw[-24000:]
                                       + "\n\nPrevious tool evidence / 上次工具执行证据（只补缺口，复用已有文件，不重复生成）:\n"
                                       + json.dumps([{"name": c.get("name"), "status": c.get("status"),
                                                      "preview": str(c.get("preview") or "")[:800],
                                                      "files": c.get("files") or []}
                                                     for c in _delivery_tools(out.message)], ensure_ascii=False),
                            exclude_plan_id=plan.message_id,
                            extra_meta={"plan_id": plan.message_id, "task_id": task.id,
                                        "routing_tags": task.strengths, "task_instruction": task.instruction,
                                        "task_title": task.title, "retry": "verification" if review_only else "delivery",
                                        "delivery_previous_message_id": out.message["id"],
                                        "delivery_previous_tools": _delivery_tools(out.message)},
                        )
                        if again is not None:
                            reason2 = self._task_shortfall(task, again.message, workspace)
                            # ⚠️ `out = again` in **both** branches. The board's `error` and the
                            # message it is attached to have to come from the same attempt, or the
                            # user reads a reason written by the retry next to the first turn's
                            # output — and the ledger's tool-call evidence then describes a turn that
                            # is not the one being judged.
                            reason = reason2 or ""
                            out = again
                    # Files from an interrupted/failed node remain discoverable,
                    # but cannot satisfy a downstream dependency until verified.
                    task.artifacts = [f for c in _delivery_tools(out.message)
                                      if c.get("status") == "ok" for f in (c.get("files") or [])]
                    task.files = [_file_label(f) for f in task.artifacts]
                    # The host/judge needs the partial result and its defects too.
                    # Dependency checks still prevent failed work being consumed.
                    if reason:
                        # A reply is not a delivery. Until the audit that produced this rule, a task
                        # counted as done the moment its owner spoke — so a turn whose every tool call
                        # failed, or one that was supposed to write a file and wrote none, was handed
                        # downstream and reported to the user as finished.
                        #
                        # ⚠️ and the text that travels must be the **stamped** one: the unmarked
                        # paragraph is what the host re-planned on top of, one round later.
                        outputs[task.id] = await self._stamp_failure(out.message, reason, emit)
                        task.status, task.error = "failed", reason
                        task.message_id = out.message["id"]
                    else:
                        outputs[task.id] = out.text
                        task.status, task.message_id = "done", out.message["id"]
                await push()
            plan.status = "integrating"
            plan.integration = {"status": "running", "message_id": "", "error": ""}
            await push()
            final = await self._agent_turn(
                group, host, members, emit, run,
                # How much of each task's output reaches the host is a setting rather than a
                # constant in this file: a long report gets truncated at the default, and the
                # number that is right for a five-line brief is wrong for a research dossier.
                extra_user=planner.integration_prompt(plan, outputs,
                                                      int(self.store.get_settings()["integration_budget"])),
                exclude_plan_id=plan.message_id,
                extra_meta={"plan_id": plan.message_id, "task_id": "final", "task_title": i18n.pick_now("Consolidate", "整合")},
            )
            final_task = planner.PlanTask("final", host["name"], host["id"],
                                          i18n.pick_now("Consolidate", "整合"), plan.goal)
            final_error = (self._task_shortfall(final_task, final.message, workspace) if final is not None
                           else run.last_error or i18n.pick_now("the model call failed", "模型调用失败"))
            plan.integration = {"status": "failed" if final_error else "done", "error": final_error,
                                "message_id": final.message["id"] if final is not None else ""}
            plan.status = "done" if not final_error and not plan.dropped and all(t.status == "done" for t in plan.tasks) else "failed"
            if final is not None:
                run.final_text = final.text
            if final_error:
                await self._system(gid, i18n.pick_now(f"Consolidation did not finish: {final_error}",
                                                     f"最终整合未完成:{final_error}"), emit)
            await push()
            # Grade the hand-offs only after the answer exists: the user gets the result first, and
            # a judge that is slow, unreachable or nonsensical can add a note but can never change
            # or delay what the group produced (see scoring.py — it never raises).
            try:
                plan.scorecard = await scoring.score_round(self.store, self.router, group, plan, outputs, run.steps)
            except Exception:  # grading is optional; it cannot undo completed work
                plan.scorecard = {}
            if plan.scorecard.get("tasks"):
                await push()                                  # the board carries the scores
                note = scoring.summarize_card(plan.scorecard)
                if note:
                    await self._system(gid, note, emit)
        except asyncio.CancelledError:
            if plan.integration.get("status") == "running":
                plan.integration["status"] = "stopped"
            if plan.status not in ("done", "failed"):
                plan.status = "stopped"
            for t in plan.tasks:
                if t.status == "running":
                    t.status = "stopped"
                elif t.status == "pending":
                    t.status = "skipped"
            try:
                await push()
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as e:  # noqa: BLE001  —— an error part-way through delegation: mark the task board as failed
# instead of showing "in progress" forever
            plan.status = "failed"
            if plan.integration.get("status") == "running":
                plan.integration.update(status="failed", error=str(e))
            for t in plan.tasks:
                if t.status in ("running", "pending"):
                    t.status, t.error = "failed", t.error or i18n.pick_now("the plan failed while running", "分工执行出错")
            try:
                await push()
            except Exception:  # noqa: BLE001
                pass
            await self._system(gid, i18n.pick_now(f"The plan failed while running: {e}", f"分工执行出错:{e}"), emit)
        finally:
            run.unfinished = [f"{t.id} {t.title}" for t in plan.tasks if t.status != "done"]
            if plan.integration:
                integration = plan.integration
                if integration["status"] != "done":
                    run.unfinished.append(i18n.pick_now("Final consolidation", "最终整合"))
                if integration["status"] in ("done", "failed"):
                    run.observations.append({"check": "plan:integration", "ok": integration["status"] == "done",
                                             "evidence": str(integration)})
                    final_msg = self.store.get_message(integration["message_id"]) if integration.get("message_id") else None
                    snapshot = {"id": "final", "title": i18n.pick_now("Consolidate", "整合"),
                                "owner": host["name"], "owner_id": host["id"], "instruction": plan.goal,
                                **integration}
                    run.defects += proclog.auto_task_defects(snapshot, ((final_msg or {}).get("meta") or {}).get("tools") or [], set())
            for task in plan.tasks:
                # A user stopping pending work is not evidence of a broken hand-off.
                if task.status not in ("done", "failed", "skipped") or (plan.status == "stopped" and task.status == "skipped"):
                    continue
                msg = self.store.get_message(task.message_id) if task.message_id else None
                calls = ((msg or {}).get("meta") or {}).get("tools") or []
                shortfall = self._task_shortfall(task, msg or {}, workspace)
                ok = task.status == "done" and msg is not None and not shortfall
                snapshot = task.to_dict()
                if task.status == "done" and shortfall:
                    snapshot.update(status="failed", error=shortfall)
                # Use this task's validated delivery, never unrelated/stale workspace filenames.
                files = set(proclog.FILE_IN_TEXT(task.deliverable)) if ok else set()
                run.defects += proclog.auto_task_defects(snapshot, list(calls), files)
                run.observations.append({"check": proclog.task_check(snapshot), "ok": ok,
                    "evidence": f"task={task.id} {task.title}; message={task.message_id}; "
                                f"deliverable={task.deliverable}; files={task.files}; status={task.status}"})

    # -------------------------------------------------------------- one turn
    async def _agent_turn(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *,
        extra_user: str | None = None, extra_meta: dict | None = None, exclude_plan_id: str | None = None,
        empty_fallback: str = "",
    ) -> TurnOut | None:
        start = len(run.steps)
        out = await self._agent_turn_impl(group, agent, members, emit, run, extra_user=extra_user,
                                          extra_meta=extra_meta, exclude_plan_id=exclude_plan_id,
                                          empty_fallback=empty_fallback)
        if out is not None and run.history is not None:
            run.history.append(out.message)
        for step in run.steps[start:]:
            step.update({k: v for k, v in (extra_meta or {}).items() if k in ("plan_id", "task_id")})
        calls = ((out.message.get("meta") or {}).get("tools") or []) if out else []
        for call in calls:
            sig = call.get("process_signature")
            check = {"kind": "tool", "scope": sig} if sig else {}
            if call.get("status") == "failed":
                run.defects.append({"key": f"tool-failed:{agent['id']}:{call.get('name')}:{sig or ''}",
                    "title": i18n.pick_now(f"Tool failed: {agent['name']} / {call.get('name')}",
                                            f"工具执行失败:{agent['name']} / {call.get('name')}"),
                    "symptom": str(call.get("preview") or "")[:500],
                    "evidence": f"message={out.message['id']}; {call.get('name')}: failed; {call.get('preview', '')}",
                    "stage": "tool", "severity": "major", "check": check,
                    "hint": i18n.pick_now("The assigning member must fix the input or dependency before retrying, and inspect the actual output.",
                                           "派工成员先修正输入或依赖再重试,并检查实际产出。")})
            if check:
                run.observations.append({"check": check, "ok": call.get("status") == "ok",
                    "evidence": f"message={out.message['id']}; {call.get('name')}: {call.get('status')}"})
        if out is None and run.last_error:
            run.defects += proclog.auto_round_defects(member_blocked={agent["name"]: run.last_error})
        return out

    async def _agent_turn_impl(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *,
        extra_user: str | None = None, extra_meta: dict | None = None, exclude_plan_id: str | None = None,
        empty_fallback: str = "",
    ) -> TurnOut | None:
        cfg = self.store.get_settings()
        mid = new_id()
        # Cleared per turn: a reason left over from an earlier member would be attributed to this one.
        run.last_error = ""
        base = {"id": mid, "group_id": group["id"], "sender_type": "agent",
                "sender_id": agent["id"], "sender_name": agent["name"]}
        await emit({"type": "message_start", "message": {**base, "content": "", "meta": extra_meta or {}}})
        if run.read_only and (agent.get("engine") or agent.get("origin") == media.MEDIA_ORIGIN):
            # External engines have their own tools and media members bypass the conversational
            # tool list. Enforce the channel boundary before entering either execution path.
            run.last_error = i18n.pick_now(
                f"{agent['name']} cannot run in a read-only chat-channel round. Start this task in the local app.",
                f"「{agent['name']}」不能在聊天通道触发的只读回合中执行。请在本地应用中发起此任务。")
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(group["id"], run.last_error, emit)
            run.steps.append({"agent": agent["name"], "ok": False, "tools": []})
            return None
        # A member that is a generator takes a different road again: it has no conversation and no
        # tools to pick from, only the one call it exists to make. Both branches are decided by the
        # member's own row rather than by anything the caller passed, so no path into this function
        # can forget to route it.
        if (agent.get("origin") or "") == media.MEDIA_ORIGIN:
            return await self._media_turn(
                group, agent, members, emit, run, mid=mid, extra_user=extra_user,
                extra_meta=extra_meta, empty_fallback=empty_fallback,
            )
        if agent.get("engine"):
            # A local tool (`engine` kind "cmd") also carries an engine, so it is separated here
            # rather than inside `_external_turn`: it has no transcript to read and no reply to
            # stream, and the two master switches that gate an external agent (an outbound call to
            # a cloud model) do not describe it — what it runs is a program already on this machine.
            engine = str(agent.get("engine") or "")
            if localcmd.row(engine) is not None:
                return await self._local_tool_turn(
                    group, agent, members, emit, run, mid=mid, extra_user=extra_user,
                    extra_meta=extra_meta, empty_fallback=empty_fallback,
                )
            return await self._external_turn(
                group, agent, members, emit, run, mid=mid, extra_user=extra_user, extra_meta=extra_meta,
                exclude_plan_id=exclude_plan_id, empty_fallback=empty_fallback,
            )

        visible_parts: list[str] = []
        raws: list[str] = []
        trace: list[dict] = []
        attempts: list[dict] = []
        res = None
        route_tags = strengths.clean_tags((extra_meta or {}).get("routing_tags") or [])
        if cfg.get("route_auto_match") and not route_tags:
            task_text = str((extra_meta or {}).get("task_instruction") or run.user_text)
            route_tags = (["reasoning", "tool-use"] if (extra_meta or {}).get("phase") == "planning"
                          else teamrec.task_tags(task_text))
        route_tags = route_tags or agent.get("tags") or []
        filt = TagFilter()

        pending: list[str] = []
        pending_len = 0
        last_emit = time.monotonic()

        async def flush_delta() -> None:
            """Merge the buffered deltas and send them out as one delta."""
            nonlocal pending_len, last_emit
            if not pending:
                return
            text = "".join(pending)
            pending.clear()
            pending_len = 0
            last_emit = time.monotonic()
            await emit({"type": "delta", "message_id": mid, "text": text})

        async def on_delta(d: str) -> None:
            nonlocal pending_len
            vis = filt.feed(d)
            if not vis:
                return
            pending.append(vis)
            pending_len += len(vis)
            if pending_len >= DELTA_BATCH_CHARS or time.monotonic() - last_emit >= DELTA_BATCH_SECONDS:
                await flush_delta()

        # The model's own working, when it is a reasoning model: shown above the answer and kept in
        # the message, because "how it got there" is most of what a user needs to judge the answer.
        # Batched coarser than the answer text (it arrives in a long unbroken run and is read, not
        # watched character by character).
        think_pending: list[str] = []
        think_all: list[str] = []
        think_len = 0
        think_last = time.monotonic()

        async def flush_thinking() -> None:
            nonlocal think_len, think_last
            if not think_pending:
                return
            text = "".join(think_pending)
            think_pending.clear()
            think_len = 0
            think_last = time.monotonic()
            await emit({"type": "thinking", "message_id": mid, "text": text})

        async def on_reasoning(d: str) -> None:
            nonlocal think_len
            think_pending.append(d)
            think_all.append(d)
            think_len += len(d)
            if think_len >= THINK_BATCH_CHARS or time.monotonic() - think_last >= THINK_BATCH_SECONDS:
                await flush_thinking()

        async def on_reset() -> None:
            nonlocal filt, pending_len, last_emit, think_len
            pending.clear()   # reset clears the screen immediately, so buffered content need not be sent again
            pending_len = 0
            filt = TagFilter()
            # The reasoning belongs to the attempt that just failed, so it goes with it — leaving it
            # on screen would attribute one model's working to the model that actually answered.
            think_pending.clear()
            think_all.clear()
            think_len = 0
            await emit({"type": "reset", "message_id": mid})
            await emit({"type": "thinking", "message_id": mid, "text": ""})
            earlier = "\n\n".join(visible_parts)
            if earlier:  # content already shown to the user in earlier rounds has to be added back
                await emit({"type": "delta", "message_id": mid, "text": earlier + "\n\n"})
            last_emit = time.monotonic()

        async def preserve_interrupted(reason: str) -> TurnOut | None:
            """Keep completed tool evidence even if the next model request fails."""
            run.last_error = redact(reason)
            if not trace:
                await emit({"type": "message_discard", "message_id": mid})
                return None
            kept = [dict(t) for t in trace]
            for entry in kept:
                if entry.get("status") == "running":
                    entry.update(status="failed", preview=i18n.pick_now(
                        "Interrupted; outcome is unknown. Check existing files before retrying.",
                        "执行中断，结果未知；重试前先检查已有文件。"))
            content = i18n.pick_now(
                "Execution interrupted. Completed tool results and files are retained below; the task is not complete and still needs verification.\n",
                "执行中断。已执行的工具结果和文件保留如下；任务尚未完成，仍须核验。\n") + run.last_error
            saved = self.store.add_message(
                group["id"], "agent", agent["id"], agent["name"], content,
                model_id=res.model_id if res else None,
                meta={"attempts": attempts, **(extra_meta or {}), "tools": kept,
                      "execution_error": run.last_error, "interrupted": True}, mid=mid)
            await emit({"type": "message_end", "message": saved})
            return TurnOut(content, content, saved)

        try:
            ctx = await self.toolhub.context(group, agent, read_only=run.read_only)
            if (extra_meta or {}).get("phase") == "planning":
                # A planner must not execute its own future tasks. In production
                # it wrote a complete manuscript, then overwrote it when task 1
                # executed again. Keep inspection available, production deferred.
                ctx.tools, _ = planning_tools(ctx.tools)
            if (extra_meta or {}).get("retry") == "delivery":
                # The retry exists to deliver the work already done. Repeating
                # broad searches consumes the same budget and loses the same file.
                ctx.tools = {name: spec for name, spec in ctx.tools.items()
                             if name not in {"search_literature", "library_search", "memory_search", "list_figures"}}
            if (extra_meta or {}).get("retry") == "verification":
                ctx.tools = {name: spec for name, spec in ctx.tools.items()
                             if name in {"review_picture", "review_audio", "library_read", "list_workspace_files", "write_document"}}
            for p in ctx.problems:
                if p not in run.warned:
                    run.warned.add(p)
                    await self._system(group["id"], p, emit)
            rounds = int(cfg["tool_rounds"]) if ctx.tools else 0
            # The tool names this member may actually call, for the parser's "was that a call?"
            # test. A model that sends its call with no `<tool_call>` tags around it (measured
            # 2026-09-25: a whole `write_document` with a storyboard in its body) is only
            # recognisable by shape, and shape alone would also match JSON that someone merely
            # quoted — the list is what separates them.
            known_tools = {t["name"] for t in ctx.specs()}
            memory_block = ""
            if cfg["memory_enabled"] and group["ext"]["memory"]:
                memory_block = self.memory.block(
                    self.memory.recall(group["id"], agent["id"], run.user_text + " " + (extra_user or "")[:300])
                )
            extra_system = "\n\n".join(s for s in (run.refs_block, run.process_feedback) if s)
            run.process_used = run.process_used or bool(run.process_feedback)
            # The inject side of the hooks: extra lines for this prompt only. Added here rather
            # than to the conversation so it reads as context, and add-only so a hook can never
            # take the group's own rules out of the prompt.
            if self.hooks:
                added = await self.hooks.gate_prompt(group["id"], {
                    "agent": agent["name"], "role": agent.get("role") or "",
                    "model": agent.get("model_id") or "", "scene": _scene(extra_meta),
                })
                extra_system = f"{extra_system}\n\n{added}".strip() if extra_system else added
            messages = self.build_messages(
                group, agent, members, memory_block=memory_block,
                tools_block=tools_prompt(ctx.specs()) if ctx.tools else "", extra_system=extra_system,
                extra_user=extra_user, exclude_plan_id=exclude_plan_id,
                files=await self._files_for_turn(group, agent, run),
                history=run.history,
            )

            for rnd in range(rounds + 1):
                if rounds and rnd == rounds and (extra_meta or {}).get("phase") == "planning":
                    messages.append({"role": "user", "content": i18n.pick_now(
                        "[Planning preparation finished] No further tools can run in this planning turn. If the request requires execution, output the strict <plan> JSON now using the available evidence. Put remaining investigation and production into assigned tasks; do not stop at a list of suggestions. Do not write or generate deliverables during planning.",
                        "【规划准备结束】本次规划不能再调用工具。需求需要执行时，现在依据已有证据输出严格的 <plan> JSON，将剩余调查和制作分配给成员；不能只列建议就结束。规划阶段不要写文件或生成交付物。")})
                elif rounds and rnd == rounds - 1 and (extra_meta or {}).get("phase") == "planning":
                    messages.append({"role": "user", "content": i18n.pick_now(
                        "[Last planning tool round] Finish essential inspection or recruitment, then output the <plan> JSON. Delegate further investigation to the team. This is still planning: do not write or generate deliverables.",
                        "【最后一轮规划工具】完成必要的检查或补齐成员，然后输出 <plan> JSON；后续调查交给团队。当前仍是规划阶段，不要写文件或生成交付物。")})
                elif rounds and rnd == rounds:
                    messages.append({"role": "user", "content": i18n.pick_now(
                        "[Tool budget exhausted] No further tool calls can run in this turn. Summarize the evidence already obtained, give real source/file references, and state missing deliverables. Do not claim an unexecuted write or generation succeeded.",
                        "【工具额度已用完】本回合不能再执行工具调用。请汇总已经取得的证据，给出真实来源/文件路径，并说明尚未完成的交付物。不要再调用工具，不得声称尚未执行的写文件或生成已经成功。")})
                elif rounds and rnd == rounds - 1:
                    messages.append({"role": "user", "content": i18n.pick_now(
                        "[Last tool round] This is the last opportunity to execute tools. If your task requires files, write the deliverable now with the evidence already collected; label gaps honestly instead of starting another broad search.",
                        "【最后一轮工具】这是本回合最后一次执行工具的机会。任务要求交付文件时，请现在把已取得的材料写成交付文件；缺口如实注明，不要继续泛泛检索。")})
                filt = TagFilter()
                if rnd and visible_parts:
                    await flush_delta()   # flush to disk before switching segments, so buffered text from the previous round cannot
# end up after the separator
                    await emit({"type": "delta", "message_id": mid, "text": "\n\n"})
                res = await self.router.complete(
                    messages, preferred=agent["model_id"], tags=route_tags,
                    on_delta=on_delta, on_reset=on_reset, on_reasoning=on_reasoning,
                    **({"tools": [{"type": "function", "function": {
                        "name": spec["name"], "description": spec.get("description", ""),
                        "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
                    }} for spec in ctx.specs()], "tool_choice": "required" if (extra_meta or {}).get("retry") in {"delivery", "verification"} and rnd == 0 else "auto"} if rnd < rounds else {}),
                )
                await flush_delta()   # wrap up: send the last incomplete batch of deltas, then handle a trailing tag
                await flush_thinking()
                tail = filt.flush()
                if tail:
                    await emit({"type": "delta", "message_id": mid, "text": tail})
                raws.append(res.text)
                attempts += [a.to_dict() for a in res.attempts]
                visible, calls = parse_tool_calls(res.text, known=known_tools)
                visible = strip_hidden(visible, known_tools)
                if visible:
                    visible_parts.append(visible)
                if calls and rounds and rnd >= rounds:
                    notice = i18n.pick_now(
                        "Tool budget exhausted; these requested calls were not executed: ",
                        "工具额度已用完，以下调用未执行：") + ", ".join(c.name for c in calls)
                    visible_parts.append(notice)
                    await emit({"type": "delta", "message_id": mid, "text": "\n\n" + notice})
                    for call in calls:
                        entry = {"name": call.name, "args": _short_args(call.arguments),
                                 "status": "failed", "preview": notice, "ms": 0}
                        trace.append(entry)
                        await emit({"type": "tool", "message_id": mid, "index": len(trace) - 1, "call": dict(entry)})
                if not calls or rnd >= rounds:
                    break
                results = []
                for call in calls:
                    entry = {"name": call.name or i18n.pick_now("(malformed)", "(格式错误)"), "args": _short_args(call.arguments), "status": "running",
                             "process_signature": proclog.signature([agent["id"], call.name, call.arguments])}
                    trace.append(entry)
                    idx = len(trace) - 1
                    await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})

                    async def approve(spec: dict, args: dict, entry: dict = entry, idx: int = idx) -> bool:
                        entry["status"] = "waiting"   # the bubble shows "waiting for your confirmation"
                        entry["args"] = _short_args(args)
                        await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})
                        allowed = await self.approvals.ask(group=group, message_id=mid, agent=agent, spec=spec, args=args, emit=emit)
                        entry["status"] = "running"
                        if allowed:
                            await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})
                        return allowed

                    denied = False
                    if call.error:
                        # `parse_failure` rather than `call.error`: the member is shown what it
                        # actually sent, so a retry has something to correct. See the docstring —
                        # this round's seven repeated failures were one member guessing at a reason.
                        text, ok, ms, files = parse_failure(call), False, 0, []
                    else:
                        # What the call is doing *while* it does it: `run_code` streams its output
                        # (`entry["live"]`), everything else has nothing to say mid-flight and
                        # simply never calls it. Coalesced on a timer, because the point is to show
                        # the newest lines, not to send one frame per line of a build log.
                        live: dict[str, float] = {"at": 0.0}
                        tail = ""

                        async def on_progress(chunk: str, entry: dict = entry, idx: int = idx,
                                              live: dict = live) -> None:
                            nonlocal tail
                            tail = (tail + chunk)[-LIVE_OUTPUT_CHARS:]
                            entry["live"] = tail
                            now = time.monotonic()
                            if now - live["at"] < LIVE_OUTPUT_SECONDS:
                                return
                            live["at"] = now
                            await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})

                        oc = await self.toolhub.call(ctx, call.name, call.arguments, approve, on_progress)
                        text, ok, ms, denied, files = oc.text, oc.ok, oc.ms, oc.denied, oc.files
                        if oc.effective_args is not None:
                            entry["args"] = _short_args(oc.effective_args)
                            entry["process_signature"] = proclog.signature([agent["id"], call.name, oc.effective_args])
                    entry.pop("live", None)     # the finished call shows its result, not its tail
                    entry.update(status="denied" if denied else "ok" if ok else "failed", ms=ms, preview=text[:300])
                    if not ok and not denied:
                        # Counted for the process log: the same call failing twice in one round is a
                        # member going round in circles, and a refusal by the user is not a failure.
                        name = str(entry.get("name") or "?")
                        run.tool_failures[name] = run.tool_failures.get(name, 0) + 1
                    if files:
                        entry["files"] = files      # what the call produced, so the bubble can offer it
                    await emit({"type": "tool", "message_id": mid, "index": len(trace) - 1, "call": dict(entry)})
                    if ok and call.name == "invite_team_resource":
                        group.update(self.store.get_group(group["id"]) or {})
                        members[:] = self.store.group_members(group["id"])
                        ctx = await self.toolhub.context(group, agent, read_only=run.read_only)
                        if (extra_meta or {}).get("phase") == "planning":
                            ctx.tools, _ = planning_tools(ctx.tools)
                        known_tools = set(ctx.tools)
                        await emit({"type": "group_updated", "group": group})
                    results.append(format_result(call.name or "error", ok, text, int(cfg["tool_output_limit"])))
                messages.append({"role": "assistant", "content": res.text})
                messages.append({"role": "user", "content": "\n\n".join(results) + i18n.pick_now("\n\nCarry on based on the tool results.", "\n\n请基于工具结果继续。")})
        except AllRoutesFailed as e:
            detail = "; ".join(f"{a.model_id}:{a.detail}" for a in e.attempts) or i18n.pick_now("no model available", "没有可用模型")
            # Why it is empty matters, because the two causes need different actions and the
            # wording above only fits one of them. A model has to be named in the priority chain to
            # be used at all, so "everything is unusable" can be true while a key is perfectly fine.
            unchained = self.router.unchained_usable()
            hint = ""
            if unchained:
                names = ", ".join(m["model_name"] for m in unchained[:4])
                more = "" if len(unchained) <= 4 else i18n.pick_now(f" and {len(unchained) - 4} more", f" 等 {len(unchained)} 个")
                hint = i18n.pick_now(
                    f" {len(unchained)} other models are usable but none of them is in the priority "
                    f"chain, which is the allow-list for what may be called ({names}{more}). Add one "
                    "under Settings → Routing & fallback → Priority chain.",
                    f" 另有 {len(unchained)} 个模型可用,但都不在「优先级链」里——链是「允许被调用的名单」"
                    f"({names}{more})。到 「设置 → 路由与回退 → 优先级链」里加一个即可。",
                )
            attempts.extend(a.to_dict() for a in e.attempts)
            interrupted = await preserve_interrupted(i18n.pick_now(
                f"No model is available: {detail}", f"所有模型均不可用:{detail}"))
            await self._system(
                group["id"], i18n.pick_now(f"{agent['name']} cannot reply right now: no model is available ({detail}).{hint}", f"「{agent['name']}」暂时无法回复,所有模型均不可用({detail})。{hint}") + self._repeat_failure_note(group["id"], agent), emit
            )
            run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
            return interrupted
        except asyncio.CancelledError:
            await preserve_interrupted(i18n.pick_now("Execution was cancelled.", "执行已中止。"))
            raise
        except Exception as e:  # noqa: BLE001
            interrupted = await preserve_interrupted(str(e))
            await self._system(group["id"], i18n.pick_now(f"{agent['name']} failed while replying: {e}", f"「{agent['name']}」发言出错:{e}"), emit)
            run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
            return interrupted

        assert res is not None
        content = "\n\n".join(visible_parts).strip()
        if not content:
            content = empty_fallback or (i18n.pick_now("(a tool was called; there was no further explanation)", "(已调用工具,没有额外说明)") if trace else res.text.strip())
        # The reply side of the hooks, before it becomes part of the record: the last moment at
        # which a group can be stopped from keeping something it should not keep.
        if self.hooks:
            reason, content = await self.hooks.gate_reply(
                group["id"], {"name": agent["name"], "model": res.model_id}, content)
            if reason:
                await emit({"type": "message_discard", "message_id": mid})
                await self._system(group["id"], reason, emit)
                run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
                return None
        meta = {"attempts": attempts, **(extra_meta or {}), "routing_tags": route_tags,
                "routing_auto": bool(cfg.get("route_auto_match"))}
        if trace:
            meta["tools"] = trace
        elif any("tool_call" in raw or '"arguments"' in raw for raw in raws):
            meta["tool_protocol_diagnostic"] = "\n\n".join(raws)[-2400:]
        thinking_text = "".join(think_all).strip()
        if thinking_text:
            # Kept with the reply so the working behind an answer can be read again later — that is
            # the difference between a reply that can be checked and one that has to be trusted.
            # Only the tail: a reasoning model can produce tens of thousands of characters, and this
            # is a record of how the answer was reached, not a transcript of an inner monologue.
            meta["thinking"] = thinking_text[-THINK_STORE_CHARS:] if len(thinking_text) > THINK_STORE_CHARS else thinking_text
        try:
            saved = self.store.add_message(
                group["id"], "agent", agent["id"], agent["name"], content,
                model_id=res.model_id, fallback_from=res.fallback_from, meta=meta, mid=mid,
            )
        except Exception as e:  # noqa: BLE001  —— even when saving fails the UI has to wrap up, leaving no bubble that
# spins forever
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(group["id"], i18n.pick_now(f"{agent['name']}'s reply could not be saved: {e}", f"「{agent['name']}」的回复没能保存:{e}"), emit)
            return None
        await emit({"type": "message_end", "message": saved})
        run.steps.append({
            "agent": agent["name"], "model": res.model_id, "ok": True,
            "tools": [t["name"] for t in trace if t.get("status") == "ok"], "fallback": bool(res.fallback_from),
        })
        self._notify("agent.reply", group["id"], group, {
            "agent": agent["name"], "model": res.model_id, "fallback_from": res.fallback_from or "",
            "chars": len(content), "tools": [t["name"] for t in trace],
            "latency_ms": (attempts[-1].get("latency_ms") if attempts else 0),
        })
        return TurnOut(content, "\n".join(raws), saved)

    # ------------------------------------------------------- local tool / external agent turns
    async def _local_tool_turn(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *, mid: str,
        extra_user: str | None, extra_meta: dict | None, empty_fallback: str,
    ) -> TurnOut | None:
        """A member that IS a command line on this machine (HyperFrames, Remotion, …) takes its turn.

        Three things make this unlike every other member, and all three come from the same fact —
        there is no model here, only a program:

        * **it does not read the conversation.** A renderer has no use for the transcript, so the
          prompt-building half of `_external_turn` is not merely skipped, it is wrong: handing a
          renderer a chat log as an argument is how a stray word changes a command.
        * **the command is ours** (`localcmd.TOOLS`), and the member's sentence reaches it as *one
          argv element*, or not at all. Nothing the user or a model writes is re-parsed by a shell.
        * **what it produces is the point.** The files a run leaves behind are collected and put on
          the message in the same `meta.tools` shape a tool call uses, so the clip appears as the
          same pill and the same player. Without this the turn would look like it worked while the
          film sat unseen in a folder — which is the failure this whole path exists to avoid.
        """
        cfg = self.store.get_settings()
        name = agent["name"]
        gid = group["id"]
        engine = str(agent.get("engine") or "")
        row = localcmd.row(engine)
        assert row is not None, "the caller only routes known local tools here"

        async def fail(msg: str) -> None:
            await emit({"type": "message_discard", "message_id": mid})
            # The specific reason is kept for the caller. A local tool refuses for a reason its own
            # table states ("it renders a Remotion project, and there is nothing like that in its
            # folder yet"), and the task board used to overwrite it with "the model call failed" —
            # which is not only less useful, it is false: no model was involved.
            run.last_error = msg
            await self._system(gid, msg + self._repeat_failure_note(gid, agent), emit)
            run.steps.append({"agent": name, "ok": False, "tools": []})
            return None

        if run.read_only:
            return await fail(i18n.pick_now("This is a read-only round; local execution tools cannot run.", "本轮只读,不能运行本地执行工具。"))
        if not cfg["external_agents_enabled"]:
            return await fail(i18n.pick_now(
                f"{name} is a local tool member, and the switch that lets outside programs run for "
                "a group is off, so it was skipped. Turn it on under Settings → External agents.",
                f"「{name}」是本机工具成员,而「允许外部程序为群服务」的开关是关着的,已跳过。"
                "请到「设置 → 外部智能体」里打开。"))

        ws = self.store.workspace_dir(gid)
        # The instruction is what the triggering message said. Only some tools put it anywhere (a
        # speech synthesizer speaks it); for the rest it is what the member reports back, and the
        # run is the project sitting in its folder.
        metadata = extra_meta or {}
        args = metadata.get("arguments") or {}
        if row.get("voice_engine") and metadata.get("task_id") and not str(args.get("text") or "").strip():
            return await fail(i18n.pick_now(
                "Speech task has no arguments.text. A chat member must prepare the exact narration and pass ref_audio for cloning before this tool can run. Task instructions are not narration.",
                "配音任务缺少 arguments.text。请由对话成员先整理逐字旁白，克隆时同时传 ref_audio，再交给工具执行。任务说明不能作为朗读内容。"))
        source = metadata.get("task_instruction") or extra_user \
            or self._summoning_line(group, members, agent, run.history) or run.user_text or ""
        instruction = args["text"] if isinstance(args.get("text"), str) and args["text"].strip() else _tool_instruction(str(source), members, agent)
        try:
            timeout = int((agent.get("engine_cfg") or {}).get("timeout") or localcmd.DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = localcmd.DEFAULT_TIMEOUT
        # Where the program is, if the user had to say. A local tool is normally installed by a
        # package manager and found by name, but one whose own instruction is "clone it and run
        # `uv sync`" ends up in that clone's virtualenv, on nobody's `PATH` — and then this field is
        # the only thing that can find it (`localcmd.exe_for` says why, and the probe uses the same
        # value, so what the settings page reported is the program this starts).
        # The member's own value wins; failing that, whatever this app was told on the engine's
        # behalf — one program serves every member of one engine (`ExternalRunner.engine_path`).
        exe = (str((agent.get("engine_cfg") or {}).get("cli_path") or "").strip()
               or self.external.engine_path(engine))

        # A tool that renders *a project* and has none is told so before the machine spends three
        # minutes starting a browser. `probe` already knows the sentence; it is not re-written here.
        if row.get("project"):
            folder = Path(ws) / localcmd.folder_name(engine)
            if not folder.is_dir() or not any(folder.iterdir()):
                return await fail(i18n.pick_now(
                    f"\"{name}\" renders {row['project']}, and there is nothing like that in its "
                    f"folder yet ({folder}), so nothing was run. Put it there and ask again.",
                    f"「{name}」渲染的是{row['project_zh']},而它那个目录里现在还没有东西"
                    f"({folder}),所以没有执行。放进去之后再让它跑一次。"))

        entry = {"name": f"local:{engine}", "status": "running",
                 "args": {"instruction": instruction[:200]}, "files": [],
                 "process_signature": proclog.signature([agent["id"], engine, instruction, args])}
        await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})
        try:
            got = await localcmd.run(engine, workspace=ws, instruction=instruction, timeout=timeout,
                                     cli_path=exe, ref_audio=str(args.get("ref_audio") or ""),
                                     ref_text=str(args.get("ref_text") or ""))
        except localcmd.LocalToolError as e:
            entry["status"] = "failed"
            await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})
            run.tool_failures[engine] = run.tool_failures.get(engine, 0) + 1
            # `e` is already in the request language (`localcmd` builds it with `pick_now`), so it
            # is only prefixed with whose turn it was.
            return await fail(f"{name}: {e}")
        except asyncio.CancelledError:
            await emit({"type": "message_discard", "message_id": mid})
            raise

        files = [{"kind": localcmd.artifact_kind(f["name"]), "name": f["name"], "bytes": f["bytes"],
                  "rel": f["rel"], "path": f["rel"]} for f in got["files"]]
        entry.update(status="ok" if got["ok"] else "failed", ms=int(got["seconds"] * 1000),
                     preview=got["text"][-300:], files=files)
        await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})
        if not got["ok"]:
            run.tool_failures[engine] = run.tool_failures.get(engine, 0) + 1

        content = self._local_tool_said(row, got)
        if self.hooks:
            reason, content = await self.hooks.gate_reply(
                gid, {"name": name, "model": f"local:{engine}"}, content)
            if reason:
                return await fail(reason)
        meta = {"tools": [entry], "local_tool": {"engine": engine, "command": " ".join(got["command"]),
                                                "seconds": got["seconds"], "exit": got["exit"]},
                **(extra_meta or {})}
        try:
            saved = self.store.add_message(
                gid, "agent", agent["id"], name, content, model_id=f"local:{engine}", meta=meta, mid=mid,
            )
        except Exception as e:  # noqa: BLE001 — the UI must wrap up either way, leaving no spinning bubble
            return await fail(i18n.pick_now(f"{name}'s result could not be saved: {e}",
                                            f"「{name}」的产出没能保存:{e}"))
        await emit({"type": "message_end", "message": saved})
        run.steps.append({"agent": name, "model": f"local:{engine}", "ok": bool(got["ok"]),
                          "tools": [engine] if got["ok"] else []})
        self._notify("agent.reply", gid, group, {
            "agent": name, "model": f"local:{engine}", "local": True,
            "chars": len(content), "tools": [engine], "ok": got["ok"],
            "seconds": got["seconds"], "files": [f["name"] for f in files],
        })
        return TurnOut(content, content, saved)

    def _local_tool_said(self, row: dict, got: dict) -> str:
        """What a local tool's turn says.

        A program's stdout is a build log — hundreds of progress lines ending in the one line that
        matters. So the reply is a sentence *we* write from the facts (file names, sizes, seconds),
        with the tail of the output quoted underneath: the member's own voice is not available, and
        letting the log stand in for it would read as if the renderer had spoken to the group.
        """
        lines = [ln for ln in (got["text"] or "").splitlines() if ln.strip()]
        tail = "\n".join(lines[-8:])[:600]
        if got["ok"] and got["files"]:
            listing = "\n".join(
                f"- {f['name']} · {media.size_label(f['bytes'])}" for f in got["files"][:6])
            said = i18n.pick_now(
                f"\"{row['name']}\" finished in {int(got['seconds'])}s and wrote:\n{listing}",
                f"「{row['name']}」用时 {int(got['seconds'])} 秒,写出了:\n{listing}")
        elif got["ok"]:
            said = i18n.pick_now(
                f"\"{row['name']}\" finished in {int(got['seconds'])}s but wrote no film or audio "
                f"file, so there is nothing to hand back. Its output is quoted below.",
                f"「{row['name']}」用时 {int(got['seconds'])} 秒跑完了,但没有写出片子或音频文件,"
                "所以没有东西可以交回。它的输出引在下面。")
        else:
            said = i18n.pick_now(
                f"\"{row['name']}\" failed (exit {got['exit']}) after {int(got['seconds'])}s, so "
                f"nothing was produced. Install it with: {row['install']}",
                f"「{row['name']}」在 {int(got['seconds'])} 秒后失败(退出码 {got['exit']}),没有产出。"
                f"安装:{row['install_zh']}")
        return f"{said}\n\n```\n{tail}\n```" if tail else said

    # ------------------------------------------------------- external agent turn
    async def _external_turn(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *, mid: str,
        extra_user: str | None, extra_meta: dict | None, exclude_plan_id: str | None, empty_fallback: str,
    ) -> TurnOut | None:
        """An external agent (WorkBuddy) speaks: the group chat history is fed to its command-line
        engine and the reply is streamed back. It has its own tools, so this path skips model
        routing and does not parse <tool_call>/<plan>; the reply counts only as chat text."""
        cfg = self.store.get_settings()
        name = agent["name"]
        gid = group["id"]

        async def fail(msg: str) -> None:
            await emit({"type": "message_discard", "message_id": mid})
            # Every failure path that goes through here gets the same tail: a member that keeps
            # failing is told how many times, and that taking it out of the group is allowed.
            # The reason itself is kept for the caller too — see `RunState.last_error`.
            run.last_error = msg
            await self._system(gid, msg + self._repeat_failure_note(gid, agent), emit)
            run.steps.append({"agent": name, "ok": False, "tools": []})
            return None

        if not cfg["external_agents_enabled"]:
            return await fail(i18n.pick_now(f"{name} is an external agent, but the external-agent master switch is off, so it was skipped. Turn it on under Settings → External agents and try again.", f"「{name}」是外部智能体,而外部智能体总开关是关着的,已跳过。到「设置 → 外部智能体」里打开后再试。"))
        if not cfg["external_calls_enabled"]:
            return await fail(i18n.pick_now(f"{name} needs a cloud model, but outbound calls are switched off, so it was skipped.", f"「{name}」要连接云端模型,而「禁止外呼」正开着,已跳过。"))
        engine = str(agent.get("engine") or "workbuddy")
        try:
            ecfg = external.clean_cfg(agent.get("engine_cfg"), engine=engine)    # validate once more right before running (a restored backup or a hand-edited database can
# bring in settings that do not comply)
        except ValueError as e:
            return await fail(i18n.pick_now(f"{name}'s external-agent settings are not valid: {e}", f"「{name}」的外部智能体设置不合规:{e}"))

        memory_block = ""
        if cfg["memory_enabled"] and group["ext"]["memory"]:
            memory_block = self.memory.block(
                self.memory.recall(gid, agent["id"], run.user_text + " " + (extra_user or "")[:300])
            )
        extra_system = "\n\n".join(s for s in (run.refs_block, run.process_feedback) if s)
        run.process_used = run.process_used or bool(run.process_feedback)
        # An external agent is a member too, so the same injections apply: a group's house style
        # or today's date must not depend on which kind of member is answering.
        if self.hooks:
            added = await self.hooks.gate_prompt(group["id"], {
                "agent": name, "role": agent.get("role") or "", "model": f"ext:{engine}",
                "scene": _scene(extra_meta),
            })
            extra_system = f"{extra_system}\n\n{added}".strip() if extra_system else added
        messages = self.build_messages(
            group, agent, members, memory_block=memory_block, extra_system=extra_system,
            extra_user=extra_user, exclude_plan_id=exclude_plan_id,
            # An external engine is a command line, not a vision endpoint: it gets descriptions of
            # the pictures rather than the pictures themselves (its own model chain decides, and
            # `_may_see_images` says no for an agent without a routed model).
            files=await self._files_for_turn(group, agent, run),
            history=run.history,
        )
        # The addendum explains a working directory and a permission level, neither of which a chat
        # gateway has — asking for its workspace would even create that directory for nothing.
        system = messages[0]["content"]
        if external.kind_of(engine) == "cli":
            system += "\n\n" + external.addendum(
                name, group["name"], external.level_view(ecfg["level"])["label"],
                str(self.external.workspace(agent)), native=bool(ecfg.get("native")),
            )
        prompt = external.flatten_convo(messages[1:])
        trace: list[dict] = []

        ext_pending: list[str] = []
        ext_len = 0
        ext_last = time.monotonic()

        async def flush_ext_delta() -> None:
            nonlocal ext_len, ext_last
            if not ext_pending:
                return
            text = "".join(ext_pending)
            ext_pending.clear()
            ext_len = 0
            ext_last = time.monotonic()
            await emit({"type": "delta", "message_id": mid, "text": text})

        async def on_delta(text: str) -> None:
            nonlocal ext_len
            ext_pending.append(text)
            ext_len += len(text)
            if ext_len >= DELTA_BATCH_CHARS or time.monotonic() - ext_last >= DELTA_BATCH_SECONDS:
                await flush_ext_delta()

        async def on_tool(idx: int, entry: dict) -> None:
            await flush_ext_delta()   # tool pills are displayed in order of appearance, so flush the buffered text first and do
# not let a pill jump in front of the text
            while len(trace) <= idx:
                trace.append({})
            trace[idx] = entry
            await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})

        # The engine's own working, when it streams any (a "thinking" block). Same treatment as a
        # reasoning model's: shown above the reply, stored with the message, cleared on a failure —
        # the engines write their plan out before acting on it, and that plan is what explains the
        # tool calls that follow.
        think_pending: list[str] = []
        think_all: list[str] = []
        think_len = 0
        think_last = time.monotonic()

        async def flush_thinking() -> None:
            nonlocal think_len, think_last
            if not think_pending:
                return
            text = "".join(think_pending)
            think_pending.clear()
            think_len = 0
            think_last = time.monotonic()
            await emit({"type": "thinking", "message_id": mid, "text": text})

        async def on_reasoning(text: str) -> None:
            nonlocal think_len
            think_pending.append(text)
            think_all.append(text)
            think_len += len(text)
            if think_len >= THINK_BATCH_CHARS or time.monotonic() - think_last >= THINK_BATCH_SECONDS:
                await flush_thinking()

        try:
            res = await self.external.run(agent, system=system, prompt=prompt, on_delta=on_delta,
                                          on_tool=on_tool, on_reasoning=on_reasoning)
        except ExternalError as e:
            return await fail(i18n.pick_now(f"{name} could not reply: {e}", f"「{name}」没能回复:{e}"))
        except asyncio.CancelledError:
            await emit({"type": "message_discard", "message_id": mid})
            raise
        except Exception as e:  # noqa: BLE001
            return await fail(i18n.pick_now(f"{name} failed while replying: {e}", f"「{name}」发言出错:{e}"))

        await flush_ext_delta()   # wrap up: send the last incomplete batch of deltas
        await flush_thinking()
        content = res.text.strip() or empty_fallback or i18n.pick_now("(no reply content)", "(没有回复内容)")
        if self.hooks:
            reason, content = await self.hooks.gate_reply(
                gid, {"name": name, "model": f"ext:{agent['engine']}"}, content)
            if reason:
                return await fail(reason)
        meta: dict = {"engine": agent["engine"], "level": ecfg["level"], **(extra_meta or {})}
        thinking_text = "".join(think_all).strip()
        if thinking_text:
            meta["thinking"] = thinking_text[-THINK_STORE_CHARS:] if len(thinking_text) > THINK_STORE_CHARS else thinking_text
        info = {k: v for k, v in (("cost_usd", res.cost_usd), ("duration_ms", res.duration_ms),
                                  ("num_turns", res.num_turns), ("model", res.model)) if v not in (None, "")}
        if info:
            meta["external"] = info
        if res.denials:
            meta["denied"] = res.denials
        if trace:
            meta["tools"] = trace
        try:
            saved = self.store.add_message(
                gid, "agent", agent["id"], name, content, model_id=f"ext:{agent['engine']}", meta=meta, mid=mid,
            )
        except Exception as e:  # noqa: BLE001
            return await fail(i18n.pick_now(f"{name}'s reply could not be saved: {e}", f"「{name}」的回复没能保存:{e}"))
        await emit({"type": "message_end", "message": saved})
        run.steps.append({"agent": name, "model": f"ext:{agent['engine']}", "ok": True,
                          "tools": [t["name"] for t in trace if t.get("status") == "ok"], "fallback": False})
        self._notify("agent.reply", gid, group, {
            "agent": name, "model": f"ext:{agent['engine']}", "external": True,
            "chars": len(content), "tools": [t["name"] for t in trace],
            "cost_usd": res.cost_usd, "num_turns": res.num_turns,
        })
        return TurnOut(content, res.text, saved)

    # ------------------------------------------------------- generating member turn
    async def _media_turn(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *, mid: str,
        extra_user: str | None, extra_meta: dict | None, empty_fallback: str,
    ) -> TurnOut | None:
        """A member that *is* a generator speaks: its one tool is run with a prompt built from the
        conversation.

        The prompt is written by a chat model that reads what the group has been saying — the point
        of the member being *in* the group is that you can discuss a clip with everybody first and
        then say "make that". Handing the generator only the last sentence would throw away exactly
        the part the discussion produced, and members do not write their conclusions in a form a
        video model can consume.

        It used to be the opposite: the literal sentence, with no model in between, on the argument
        that anything else rewrites what the user already wrote. That argument holds for a complete,
        hand-written prompt and fails for "按刚才说的做" — so the writer is told to leave a prompt
        that is already complete alone, and the raw sentence is still the fallback when no chat model
        can be reached (the generation goes ahead either way, and the message says what was used).

        Everything else is the same as any other member's turn, because it runs the same tool: the
        engine's own switches gate it, the argument validation is the one in `video.py`, the file
        lands in the group's workspace, and the approval policy applies unchanged. What is
        deliberately *not* shared is the reading of the result — the tool's text is written for a
        model that must not narrate a clip it never watched, and here the reader is the user.
        """
        cfg = self.store.get_settings()
        name = agent["name"]
        gid = group["id"]

        async def fail(msg: str) -> None:
            await emit({"type": "message_discard", "message_id": mid})
            # Every failure path that goes through here gets the same tail: a member that keeps
            # failing is told how many times, and that taking it out of the group is allowed.
            # The reason itself is kept for the caller too — see `RunState.last_error`.
            run.last_error = msg
            await self._system(gid, msg + self._repeat_failure_note(gid, agent), emit)
            run.steps.append({"agent": name, "ok": False, "tools": []})
            return None

        target = media.member_target(self.store, agent)
        if target is None:
            return await fail(i18n.pick_now(
                f"\"{name}\" is a generating member, but the model it was made from is gone, so it "
                "cannot do anything. Take it out of the group, or add the model back and make the "
                "member again.",
                f"「{name}」是生成成员,但它所依据的模型已经不在了,所以什么也做不了。"
                "请把它移出群聊,或重新添加那个模型再建一次。"))
        use, prov = target["use"], target["provider"]
        tool = "generate_video" if use == "video" else "generate_image"
        # The master switch is the user's "may this machine spend money on this at all", and it
        # governs here for the same reason it governs a tool call: a member you can address must not
        # be a way around a switch you turned off.
        if not cfg["video_enabled" if use == "video" else "image_enabled"]:
            what = i18n.pick_now("video generation", "视频生成") if use == "video" \
                else i18n.pick_now("drawing", "绘画")
            return await fail(i18n.pick_now(
                f"\"{name}\" is a {what} member, but {what} is switched off under Permissions & "
                "control, so it did nothing. Turn it on there and send the message again.",
                f"「{name}」是{what}成员,但「权限与操控」里的{what}是关着的,所以它没有动手。"
                "到那里打开后再发一次。"))
        blocked = (video if use == "video" else imagegen).blocked_by_offline(prov, cfg)
        if blocked:
            return await fail(blocked)

        # What caused this turn. `extra_user` is what a plan handed this member; with none, it is
        # what the user typed. Mentions come out because "@Seedance 画一只猫" is not an instruction —
        # "画一只猫" is, and leaving the name in gets it written into the picture.
        #
        # The middle step exists because of a real, expensive failure: a generating member summoned by
        # **another member's** @ has no instruction of its own, and the fallback used to be the user's
        # whole last message. So a picture service was handed「上面是我的发音，其他是需要内容进行配音的…
        # 是 clone 这个声音」as a drawing prompt — twice — and produced nothing. When a member is the
        # one doing the summoning, the sentence that summoned it is the instruction.
        source = (extra_user or "").strip() or self._summoning_line(group, members, agent, run.history) \
            or (run.user_text or "").strip()
        instruction = _strip_mentions(source, members)
        if not instruction:
            return await fail(i18n.pick_now(
                f"\"{name}\" had nothing to work from: address it with what you want, in the same "
                "message (\"@name what to make\").",
                f"「{name}」没有可用的内容:请在点名它的同一条消息里写清楚要生成什么"
                "(「@名字 要生成什么」)。"))

        # References first: the writer is told what the generator will receive, so it can name the
        # material by position (@图片1) instead of describing pictures it has never seen.
        args = self._media_arguments(group, run, target, instruction, extra_meta or {})
        if str(args.get("prompt") or "").strip():
            note = i18n.pick_now("Used the task's explicit generation prompt.", "使用任务中明确提供的生成提示词。")
        else:
            prompt, note = await self._media_prompt(group, members, run, instruction,
                                                    use=use, target=target, refs=args, agent=agent)
            args["prompt"] = prompt

        ctx = await self.toolhub.context(group, agent, read_only=run.read_only)
        for p in ctx.problems:
            if p not in run.warned:
                run.warned.add(p)
                await self._system(gid, p, emit)

        entry: dict = {"name": tool, "args": _short_args(args), "status": "running",
                       "process_signature": proclog.signature([agent["id"], tool, args])}

        async def approve(s: dict, a: dict) -> bool:
            entry["status"] = "waiting"          # the pill shows "waiting for your confirmation"
            entry["args"] = _short_args(a)
            await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})
            allowed = await self.approvals.ask(group=group, message_id=mid, agent=agent, spec=s, args=a, emit=emit)
            entry["status"] = "running"
            if allowed:
                await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})
            return allowed

        await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})
        oc = await self.toolhub.call(ctx, tool, args, approve)
        if oc.effective_args is not None:
            entry["args"] = _short_args(oc.effective_args)
            entry["process_signature"] = proclog.signature([agent["id"], tool, oc.effective_args])
        entry.update(status="denied" if oc.denied else "ok" if oc.ok else "failed", ms=oc.ms,
                     preview=oc.text[:300])
        if not oc.ok and not oc.denied:
            run.tool_failures[tool] = run.tool_failures.get(tool, 0) + 1
        if oc.files:
            entry["files"] = oc.files
        await emit({"type": "tool", "message_id": mid, "index": 0, "call": dict(entry)})

        content = oc.text.strip() or empty_fallback or i18n.pick_now("(nothing was generated)", "(没有生成内容)")
        if note:
            # Said out loud rather than logged: which prompt was used is the difference between "the
            # clip ignored the discussion" and "there was no model available to read it".
            content = f"{content}\n\n{note}"
        # The same reply gate every other member answers to: a bad prompt can be stopped from
        # becoming part of the record whatever produced it.
        if self.hooks:
            reason, content = await self.hooks.gate_reply(
                gid, {"name": name, "model": agent.get("model_id") or ""}, content)
            if reason:
                return await fail(reason)
        meta = {"tools": [entry], **(extra_meta or {})}
        try:
            saved = self.store.add_message(
                gid, "agent", agent["id"], name, content, model_id=agent.get("model_id"), meta=meta, mid=mid,
            )
        except Exception as e:  # noqa: BLE001 — the UI must wrap up either way, leaving no spinning bubble
            return await fail(i18n.pick_now(f"{name}'s result could not be saved: {e}",
                                            f"「{name}」的产出没能保存:{e}"))
        await emit({"type": "message_end", "message": saved})
        run.steps.append({"agent": name, "model": agent.get("model_id") or "", "ok": bool(oc.ok),
                          "tools": [tool] if oc.ok else []})
        self._notify("agent.reply", gid, group, {
            "agent": name, "model": agent.get("model_id") or "", "media": use,
            "chars": len(content), "tools": [tool], "ok": oc.ok, "ms": oc.ms,
        })
        return TurnOut(content, content, saved)

    # How much of the knowledge base may enter one generation prompt. A generation prompt is a
    # sentence or two for a diffusion model: what its writer needs from the library is the facts its
    # wording rests on, not a report. Per-hit first, then across all hits.
    MEDIA_KB_CHARS = 700
    MEDIA_KB_TOTAL = 3500

    def _media_kb_hits(self, group: dict, query: str) -> list[tuple[str, str]]:
        """The knowledge base, searched for the thing this generation is about.

        Every other member searches the library with a tool and decides for itself what to read. A
        generating member has no tools and no turn to spare — it produces a prompt and runs — so the
        search happens here, on the same scope (`scope_kbs`/`scope_ids`: nothing reaches outside what
        the group may read) and the same knob (`library_top_k`) as everywhere else.

        One chunk per document: the ranking already puts the best match first, and five chunks of one
        long document crowd a prompt that only has room for the gist.
        """
        ext = (group.get("ext") or {}).get("library") or {}
        if ext.get("mode") == "off":
            return []
        ids = self.library.scope_ids(ext, group["id"])
        if not ids or not query.strip():
            return []
        k = max(1, int(self.store.get_settings()["library_top_k"]))
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        used = 0
        for hit in self.library.search(query, k, ids):
            if hit["doc_id"] in seen:
                continue
            seen.add(hit["doc_id"])
            text = clip_middle(str(hit["text"] or ""), self.MEDIA_KB_CHARS)
            if used + len(text) > self.MEDIA_KB_TOTAL:
                break
            used += len(text)
            out.append((str(hit["title"] or hit["doc_id"]), text))
        return out

    async def _media_search_terms(self, instruction: str, context: str) -> str:
        """Search terms for the knowledge base, in both languages — the one step a token index
        cannot do for itself.

        Measured on a real library: a knowledge base of surgical atlases is usually **English**
        ("Internal Carotid Artery Segments") while the group talks Chinese, and BM25 over bigrams
        shares almost no tokens between the two — the subject's English name found the right cases
        immediately, while the group's own words around them found collection listings. Naming the
        subject in both languages is exactly what the model about to write the prompt is good at, and
        it costs one short call; with the writer unavailable there is no answer to add, and the
        search falls back to the words the group used.
        """
        ask = i18n.pick_now(
            "Below is a request to generate a picture, and what a group said around it. Write the "
            "search terms that would find the material it should rest on in a knowledge base: the "
            "subject in the language used **and** its standard professional name in English, one term "
            "per line, at most 10 lines, nothing else.",
            "下面是「要求生成一张画面」的要求,以及群里围绕它说过的话。请写出用来在知识库里检索"
            "「它可以依据的材料」的检索词:把主题用**中文**和**标准英文专业名**都写出来,"
            "一行一个,最多 10 行,不要写别的内容。")
        body = "\n\n".join(x for x in (instruction, clip_middle(context, 1500)) if x.strip())
        try:
            res = await self.router.complete([{"role": "user", "content": f"{ask}\n\n{body}"}])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — an unavailable writer only costs the terms, not the search
            return ""
        return " ".join(line.strip() for line in str(res.text or "").splitlines() if line.strip())[:400]

    async def _media_material(self, group: dict, run: "RunState", instruction: str,
                              agent: dict) -> tuple[str, list[str], int]:
        """The real material a generation prompt may be built on, the names of what went in, and how
        many documents the group's knowledge base holds.

        The writer used to see the chat and nothing else, which is how a group working on a medical
        clip got prompts that invented anatomy: the discussion said what the clip was about, and every
        structure in the picture was then filled in by the writer. The material this group actually
        has was already here — attachment text extracted at upload, `@`/`#` references, and the
        knowledge bases it may search — it simply never reached this one step. So it does now, in the
        same shape every other member receives (see `_files_for_turn`, `_refs_items`), plus a library
        search nobody else has to do for it (`_media_kb_hits`, reached through `_media_search_terms`
        so a Chinese request can find an English document).

        The document count comes back with it so the note can tell "the library found nothing" from
        "there is no library to search": the first is a search to reword, the second is material to
        add, and they look identical from the outside.
        """
        workspace = self.workspace(group["id"])
        parts: list[str] = []
        cited: list[str] = []

        files = await self._files_for_turn(group, agent, run)
        names = [str(f.get("name") or "") for f in run.files if str(f.get("name") or "")]
        if files.block:
            parts.append(files.block)
            if names:
                cited.append(i18n.pick_now(
                    f"the {len(names)} attached file(s): {', '.join(names)}",
                    f"消息里附带的 {len(names)} 个文件:{', '.join(names)}"))

        refs = self._refs_items(group, run.user_text, workspace)
        if refs:
            head = i18n.pick_now("[What the user referenced]\n", "【用户引用的内容】\n")
            parts.append(head + "\n\n".join(chunk for _, chunk in refs))
            cited.append(i18n.pick_now(
                f"the {len(refs)} referenced item(s): {', '.join(name for name, _ in refs)}",
                f"引用的 {len(refs)} 项:{', '.join(name for name, _ in refs)}"))

        ext = (group.get("ext") or {}).get("library") or {}
        kb_docs = len(self.library.scope_ids(ext, group["id"])) if ext.get("mode") != "off" else 0
        query = f"{instruction}\n{run.user_text}"
        if kb_docs:
            # Always worth the one short call, even when the group's own words already match
            # something: measured on a real library, they picked up loosely related clips, and the
            # same search with the subject named in both languages landed on the two coiling cases
            # that segment was actually about. With the writer down there are no terms to add and the
            # search falls back to the group's words — which is what it did before this existed.
            terms = await self._media_search_terms(instruction, run.user_text)
            query = " ".join(x for x in (query, terms) if x.strip())
        hits = self._media_kb_hits(group, query)
        if hits:
            head = i18n.pick_now(
                "[From the knowledge base — the material to stay inside]\n",
                "【知识库里找到的相关内容 —— 事实以这些为准】\n")
            parts.append(head + "\n\n".join(f"《{title}》\n{text}" for title, text in hits))
            cited.append(i18n.pick_now(
                f"{len(hits)} knowledge-base excerpt(s): {', '.join(f'《{t}》' for t, _ in hits)}",
                f"知识库命中的 {len(hits)} 段:{', '.join(f'《{t}》' for t, _ in hits)}"))

        block = "\n\n".join(parts)
        budget = int(self.store.get_settings()["refs_budget"])
        if len(block) > budget:
            keep = max(0, budget - 200)
            block = block[:keep] + "\n\n" + i18n.pick_now(
                "(the rest was cut to keep this prompt within its budget)",
                "(其余内容因超出预算已截断)")
        return block, cited, kb_docs

    def _repeat_failure_note(self, gid: str, agent: dict) -> str:
        """A note for a member that keeps failing, saying that taking it out is an option.

        Why this exists: in one session a member failed **twelve times** and was assigned the audio and
        assembly work, so a whole workstream produced nothing — and the app said "could not reply" each
        time, identically, as if it were the first. A single failure is noise; a member that keeps
        failing is occupying a slot in every round and doing nothing with it, and there are exactly two
        real fixes: repair it, or take it out of the group. The second one has exactly one place in this
        app (the member's card), so saying so costs a sentence and hides nothing.

        The count is read back out of the transcript rather than kept in a column, for the same reason
        the task board is derived — a number that is computed from what happened cannot drift away from
        it. It counts failures across the recent window rather than a strict run, because a member
        failing every other turn is just as useless as one failing every turn.
        """
        name = str(agent.get("name") or "")
        if not name:
            return ""
        try:
            rows = self.store.list_messages(gid)[-40:]
        except Exception:  # noqa: BLE001 — a note must never be the reason a turn breaks
            return ""
        markers = ("无法回复", "没能回复", "发言出错", "cannot reply", "could not reply",
                   "failed while replying")
        mark = f"「{name}」"
        seen = 0
        for row in rows:
            if str(row.get("sender_type") or "") != "system":
                continue
            text = str(row.get("content") or "")
            if mark in text and any(m in text for m in markers):
                seen += 1
        if seen < 2:          # this failure is only the second: not a pattern yet
            return ""
        return i18n.pick_now(
            f" That is the {seen + 1}th time this member has failed here. Either fix it as described "
            "above, or take it out of this group — the button is on the member's own card in the member "
            "list, and the member itself is kept.",
            f" 这已经是它在本群第 {seen + 1} 次无法发言了。要么按上面说的修好它,要么把它移出本群 —— "
            "成员列表里那张成员卡片上就有这个按钮,成员本身不会被删掉。")

    def _summoning_line(self, group: dict, members: list[dict], agent: dict,
                        history: list[dict] | None = None) -> str:
        """The message that actually summoned this member, when it was not the user.

        Walks back through the transcript and stops at the first **user** message: the user's own words
        are already the last fallback, and rewriting them here would only lose the case where the user
        names a member but the sentence is not about a picture at all. Only a *member's* sentence is
        taken, because that is the one that delegated work — and the newest such sentence wins, which
        is what a relay of several rounds needs.

        Empty means "nobody summoned it in words", which the caller turns into a refusal rather than a
        guess. That is the point: a generator asked to work from nothing must say so, not generate.
        """
        try:
            rows = (self.store.list_messages(group["id"]) if history is None else history)[-30:]
        except Exception:  # noqa: BLE001 — a lookup that fails must not break the turn
            return ""
        for row in reversed(rows):
            if str(row.get("sender_type") or "") == "user":
                break
            text = str(row.get("content") or "")
            if not text.strip():
                continue
            if any(m["id"] == agent["id"]
                   for m in find_mentions(text, members, exclude_id=row.get("sender_id"))):
                return text
        return ""

    async def _media_prompt(
        self, group: dict, members: list[dict], run: RunState, instruction: str, *,
        use: str, target: dict, refs: dict, agent: dict,
    ) -> tuple[str, str]:
        """(the prompt to generate with, a note to show when it is not what we wanted).

        The conversation is turned into a prompt by a chat model, because that is the step between
        "the group discussed a clip" and "a video model can make it" — and because a group's
        conclusions are written for people, not for a diffusion model. The writer sees the messages
        (clipped with the same budgets as any other turn), the instruction that addressed this
        member, and an inventory of what the generator is about to receive, so it can point at a
        reference by position instead of describing a picture it cannot see.

        It also sees the group's **material** (`_media_material`): the text of what was attached,
        what was referenced, and what the knowledge base holds on the subject. That is the substance
        of "make the picture true to something" — a chat tells you what the clip is about, not what
        a structure looks like, so a writer with no material fills every detail in itself and the
        result is a confident picture of nothing in particular. The note that comes back says which
        material was handed over, and which parts the writer could not ground at all.

        It is told to leave an already-complete prompt alone: someone who pasted a hand-written,
        shot-by-shot description means it, and paraphrasing that would be the same bug in the other
        direction. When nothing can write one — every model down, or all of them answering with
        nothing (the router raises rather than returning an empty string) — the raw instruction is
        still good enough to generate with, so the run continues and the second element of the pair
        says which one was used. There is deliberately no separate branch for "the answer was
        empty": it cannot happen, and a branch that no input can reach is a branch no test covers.
        """
        provider = target["provider"]
        model = target["model"]
        # Gathered before the writer is told anything, because whether there is any decides what it
        # is allowed to do: with material it may state what the material states, without it may only
        # describe a look.
        material, cited, kb_docs = await self._media_material(group, run, instruction, agent)
        rows = self.store.list_messages(group["id"]) if run.history is None else run.history
        cfg = self.store.get_settings()
        budget = int(cfg["history_clip"])
        limit = max(1, int(cfg["history_limit"]))
        # Oldest-first, most recent kept: a discussion's conclusion is at the end. The plan board and
        # system notes stay in — the board is where a plan's assigned wording lives, and "some tasks
        # could not be done" is exactly what a generator should not be told to guess at.
        recent = rows[-limit:]
        convo = "\n\n".join(
            f"[{m['sender_name'] or m['sender_type']}] {clip_middle(str(m['content'] or ''), budget)}"
            for m in recent if str(m.get("content") or "").strip()
        ) or i18n.pick_now("(no earlier messages)", "(前面没有其它消息)")
        system = i18n.pick_now(
            "You write one generation prompt for an AI model, from a group chat. "
            f"The model is {model['model_name']} ({provider['name']}), and it makes "
            f"{'video' if use == 'video' else 'an image'}. "
            "Answer with the prompt itself and nothing else: no preamble, no explanation, no quotes, "
            "no markdown, no code fences, no alternatives. Write it in the language of the "
            "conversation. "
            "The last thing in the message below is the instruction that asked for this; the rest is "
            "what the group said before it. Build the prompt on what the conversation established — "
            "the subject, the style, the mood, the length, and above all the conclusions the members "
            "arrived at, including any wording they agreed on. If the instruction is already a "
            "complete description, keep it as it is instead of rewriting it. "
            "Everything factual in the picture — anatomical structures, instruments, places, numbers, "
            "text that appears on screen, organisations, people, the order of a procedure — has to "
            "come from the material given below. What that material does not state must not be "
            "written as if it were known: leave it out, or describe only how the shot looks. Matters "
            "of taste stay yours: framing, camera movement, light, colour, texture, rhythm, duration "
            "and overall style. "
            + ("That material is the only source of facts here; the discussion above may be wrong "
               "about them and is not one. "
               if material else
               "There is no material for this one at all: write about how the shot looks and nothing "
               "else — no named structures, no numbers, no on-screen text, and no claim about how "
               "anything works. ")
            + "Then, on its own line after the prompt, write “Ungrounded: ” followed by what you "
            "could not establish from the material and therefore left out of the picture (write "
            "“none” if there was nothing). That last line is not part of the prompt. "
            f"{video.prompt_note(provider['kind']) if use == 'video' else imagegen.PROMPT_NOTE}",
            "你要根据一段群聊,为一个人工智能模型写一条生成提示词。"
            f"那个模型是 {model['model_name']}({provider['name']}),它生成{'视频' if use == 'video' else '图片'}。"
            "只回答提示词本身:不要开场白、不要解释、不要引号、不要 markdown、不要代码块、不要给多个备选。"
            "用这段对话所用的语言来写。"
            "下面最后一段是「要求这次生成」的那句话,前面的都是群里之前说过的内容。"
            "提示词要建立在讨论已经确立的东西上 —— 主体、风格、氛围、时长,尤其是成员们商定的结论,"
            "包括他们已经定下来的措辞。如果那句话本身就已经是一份完整的描述,那就原样保留,不要改写。"
            "画面里一切**事实性**的东西 —— 解剖结构、器械、部位、数字、画面上要出现的文字、机构、人物、"
            "操作顺序 —— 都必须来自下面给出的素材;素材没有写到的,不要写成已知的既定事实:"
            "要么不写,要么只写「这个镜头看起来是什么样」。属于审美取舍的部分仍然由你决定:"
            "构图、镜头运动、光线、色彩、质感、节奏、时长和整体风格。"
            + ("在这些素材之外没有别的事实来源;上面的讨论可能把事实说错,不算依据。"
               if material else
               "这一条**完全没有素材**:只写画面与风格,不要写任何具体结构、数字、画面文字,"
               "也不要写任何关于「原理是什么」的断言。")
            + "然后在提示词之后**另起一行**写「未确证: 」,后面写你无法从素材确证、因此没有写进画面的东西"
            "(没有就写「无」)。这一行不属于提示词本身。"
            f"{video.prompt_note(provider['kind'], 'zh') if use == 'video' else imagegen.PROMPT_NOTE_ZH}",
        )
        inventory = self._refs_inventory(refs)
        # The material is put in front of the writer as its own section, and when there is none that
        # is said in as many words: an empty section reads like "nothing relevant", and the one answer
        # that must never come back is a confident description of material nobody provided.
        asked = i18n.pick_now(
            f"[Material you may build the prompt on]\n{material}" if material else
            "[Material you may build the prompt on]\n(None — no file came with the message, nothing "
            "was referenced, and the knowledge base found nothing for this.)",
            f"【你可以据以写提示词的素材】\n{material}" if material else
            "【你可以据以写提示词的素材】\n(没有 —— 这条消息没有带文件,没有引用任何内容,"
            "知识库里也没有检索到相关材料。)")
        user = i18n.pick_now(
            f"[Conversation so far]\n{convo}\n\n"
            f"[The instruction that asked for this]\n{instruction}\n\n"
            f"{asked}\n\n"
            f"[What the generator will receive alongside your prompt]\n{inventory}",
            f"【到目前为止的对话】\n{convo}\n\n"
            f"【要求这次生成的那句话】\n{instruction}\n\n"
            f"{asked}\n\n"
            f"【生成器会和你写的提示词一起收到的东西】\n{inventory}",
        )
        try:
            res = await self.router.complete([{"role": "system", "content": system},
                                              {"role": "user", "content": user}])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a missing writer must not cost the generation itself
            self._notify("media.prompt", group["id"], group, {"ok": False, "error": type(e).__name__})
            return instruction, i18n.pick_now(
                "(No chat model was available to read the conversation, so the instruction above was "
                f"used as the prompt as written. {type(e).__name__}: {e})",
                f"(没有可用的对话模型来读这段讨论,所以直接把上面那句话当提示词用了。{type(e).__name__}: {e})",
            )
        raw, ungrounded = split_ungrounded(res.text)
        text = _clean_prompt(raw)
        self._notify("media.prompt", group["id"], group,
                     {"ok": True, "model": res.model_id, "chars": len(text),
                      "cited": cited, "grounded": bool(material),
                      "fallback_from": res.fallback_from or ""})
        return text, self._grounding_note(cited, ungrounded, use, kb_docs)

    @staticmethod
    def _grounding_note(cited: list[str], ungrounded: str, use: str, kb_docs: int = 0) -> str:
        """The two lines that go under a generated clip: what it was built on, and what was not.

        Both are shown to the group rather than logged, because "the picture invented anatomy" and
        "there was nothing in here to build it on" look identical in the artefact and call for
        opposite actions from the user: the first is a generation to redo, the second is material to
        add. Written from what was actually handed over (`cited`, which the gathering step fills in),
        never from what the writer claims to have used.

        Within "nothing to build on" the two causes are named apart as well: nothing to search, or a
        library of `kb_docs` that matched none of the words used. The second is usually a search to
        reword, and saying "nothing in the knowledge base" about it would send the user looking for a
        document that is already there.

        The advice to add material is for video only: a clip is usually asked for *because* something
        is known about the subject, while a drawing is often asked for with nothing to go on and no
        claim to make, and a line telling that user to go and find documents is noise.
        """
        if cited:
            first = i18n.pick_now(
                f"(This prompt was built on: {'; '.join(cited)})",
                f"(这条提示词依据的材料:{'; '.join(cited)})")
        else:
            nothing = (i18n.pick_now(
                f"no attachment, no reference, and none of the {kb_docs} documents in the knowledge "
                "base matched", f"没有附件、没有引用,知识库里那 {kb_docs} 份文档也都没有命中")
                if kb_docs else
                i18n.pick_now("no attachment, no reference, and nothing to search in the knowledge "
                              "base", "没有附件、没有引用,知识库里也没有可检索的内容"))
            if use == "video":
                first = i18n.pick_now(
                    f"(No material was found for this prompt — {nothing}, so it describes the look "
                    "only. Add the material (or @ a document) and ask again to have the picture rest "
                    "on something.)",
                    f"(这条提示词没有找到可依据的材料 —— {nothing},所以它只描述了画面本身。"
                    "把资料加进来(或在消息里 @ 上文档)再让它生成一次,画面才有依据。)")
            else:
                first = i18n.pick_now(
                    f"(No material was found for this drawing — {nothing}, so it follows your sentence "
                    "as written.)",
                    f"(这幅图没有找到可依据的材料 —— {nothing},所以它只按你这句话展开。)")
        lines = [first]
        if ungrounded:
            lines.append(i18n.pick_now(
                f"(Not established from the material, so left out of the picture: {ungrounded})",
                f"(没能从材料确证、因此没有写进画面的:{ungrounded})"))
        return "\n".join(lines)

    @staticmethod
    def _refs_inventory(refs: dict) -> str:
        """What the generator is handed besides the prompt, described so the writer can name it.

        Only what is true: the count, the kind, and the file names. The writer cannot see these
        pictures, which is exactly why it is told it cannot — otherwise it will happily describe
        them, and those invented words end up steering the generation.
        """
        order = {"reference_images": ("pictures", "图片"), "reference_videos": ("video clips", "视频"),
                 "reference_audios": ("audio clips", "音频")}
        lines = []
        for key, (en, zh) in order.items():
            got = list(refs.get(key) or [])
            if got:
                names = ", ".join(Path(str(g)).name for g in got)
                lines.append(i18n.pick_now(
                    f"{len(got)} {en}: {names} — name them by position (@图片1 / @视频1 / @音频1) "
                    "if that helps.",
                    f"{len(got)} 段{zh}:{names} —— 需要时按位置称它们为 @图片1 / @视频1 / @音频1。"))
        return "\n".join(lines) or i18n.pick_now(
            "Nothing — only your prompt. You cannot see any of the material the user may have "
            "attached; do not describe it.",
            "没有别的 —— 只有你写的提示词。用户可能附上的素材你看不到,不要描述它们。")

    def _media_arguments(self, group: dict, run: RunState, target: dict,
                         instruction: str, meta: dict) -> dict:
        """Carry actual parameters and dependency files into a generator's single call."""
        use, prov = target["use"], target["provider"]
        allowed = {"prompt", "duration_seconds", "aspect_ratio", "first_frame", "last_frame",
                   "reference_images", "reference_videos", "reference_audios", "seed", "generate_audio"} \
            if use == "video" else {"prompt", "size", "model_id", "reference_images", "reference_files",
                                    "reference_image", "images", "image", "first_frame"}
        supplied = dict(meta.get("arguments") or {})
        if "seconds" in supplied and "duration_seconds" not in supplied:
            supplied["duration_seconds"] = supplied.pop("seconds")
        args = {k: v for k, v in supplied.items() if k in allowed}
        if use != "video":
            # Direct image requests may use the files attached to that request. In a team
            # plan the host selects references explicitly: another task's pictures or a
            # user's portrait must not silently become an anatomical drawing's input.
            if "reference_images" not in args and not meta.get("plan_id"):
                attached = self._media_refs(group, run).get("reference_images")
                if attached:
                    args["reference_images"] = attached
            return args
        seconds = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:秒|seconds?\b|s\b)", instruction, re.I)
        ratio = re.search(r"\b(16:9|9:16|1:1|4:3|3:4|21:9)\b", instruction)
        if seconds:
            args.setdefault("duration_seconds", float(seconds[1]))
        if ratio:
            args.setdefault("aspect_ratio", ratio[1])
        refs = self._media_refs(group, run)
        inputs = [f for f in meta.get("input_files", []) if isinstance(f, dict)]
        images = [str(f.get("path") or f.get("rel") or f.get("name") or "") for f in inputs
                  if f.get("kind") in ("image", "figure")
                  or Path(str(f.get("name") or "")).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        if images:
            refs.setdefault("reference_images", []).extend(images)
        if prov["kind"] == video.ARK_KIND:
            for key, values in refs.items():
                args.setdefault(key, values)
        elif prov["kind"] == video.META_KIND:
            # Hosted I2V needs the original provider URL, never a made-up URL for our disk.
            first = str(args.get("first_frame") or "")
            candidates = [f for f in inputs if f.get("source_url")
                          and (not first or first in (f.get("path"), f.get("name"), f.get("rel")))]
            if candidates:
                args["first_frame"] = candidates[0]["source_url"]
            elif not first and refs.get("reference_images"):
                args["first_frame"] = refs["reference_images"][0]
        else:
            takes_image = prov["kind"] != comfyui.KIND or "image" in comfyui.wants(
                comfyui.workflow_of(target["model"]["model_name"]))
            if takes_image and refs.get("reference_images"):
                args.setdefault("first_frame", refs["reference_images"][0])
        return args

    def _media_refs(self, group: dict, run: RunState) -> dict:
        """The files on the user's message, as the reference arrays Ark takes.

        This is the one place a group can get reference material to a generator without hosting it
        anywhere: a picture it drew, or one the user dragged in, is a file in its own workspace. The
        role each one gets is decided by what it *is* rather than by what it is called, because that
        is what the bytes said at upload time.

        Deliberately not `first_frame`: a keyframe task pins the output ratio to `adaptive`, and
        somebody who attached a picture and asked for 16:9 meant both. Ark's omni-reference path is
        the one that keeps their ratio, and the prompt can still say "首帧为该图".
        """
        out: dict[str, list[str]] = {"reference_images": [], "reference_videos": [], "reference_audios": []}
        workspace = self.workspace(group["id"])
        for meta in run.files:
            row = self.store.get_attachment(str(meta.get("id") or ""))
            if not row or row["group_id"] != group["id"]:
                continue
            path = attachments_lib.path_for_row(self.store, row)
            if path is None:
                continue          # already reported by `_files_for_turn`
            key = {"image": "reference_images", "video": "reference_videos",
                   "audio": "reference_audios"}.get(row.get("kind") or "")
            if key and path.resolve().is_relative_to(workspace.resolve()):
                # A path inside the workspace is what `ark_asset` knows how to inline; anything that
                # somehow is not is left out rather than read from outside the group's own folder.
                out[key].append(str(path.relative_to(workspace.resolve())))
        return {k: v for k, v in out.items() if v}

    async def _plan_failed(self, gid: str, out: "TurnOut", note: str, emit: Emit) -> None:
        """When the plan could not be executed, rewrite the owner's message that only says
"delegation is ready, see the task board", so it does not contradict the system note below."""
        # Preserve enough of the failed protocol to debug it after hidden plan
        # tags are stripped from the chat. This is draft data, never executable.
        meta = {**(out.message.get("meta") or {}), "plan_error": note,
                "plan_draft": out.raw[:40000]}
        self.store.update_message(out.message["id"], meta=meta)
        if out.text in PLAN_FALLBACK:
            try:
                fixed = self.store.update_message(out.message["id"], content=i18n.pick_now("(the plan did not take effect — see the system notice below)", "(分工计划没能生效,见下方系统提示)"))
                if fixed:
                    await emit({"type": "message_end", "message": fixed})
            except Exception:  # noqa: BLE001 — failing to rewrite the copy does not affect the notes that follow
                pass
        await self._system(gid, note, emit)

    async def _system(self, gid: str, text: str, emit: Emit) -> None:
        msg = self.store.add_message(gid, "system", None, i18n.pick_now("System", "系统"), text)
        await emit({"type": "message", "message": msg})


def clip_middle(text: str, limit: int) -> str:
    """Overly long history messages: keep the beginning and the end, drop the middle (the
beginning sets the scene, the end usually carries the conclusion)."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return i18n.pick_now(f"{text[:head]}\n...({len(text) - limit} characters omitted in the middle)...\n{text[-tail:]}", f"{text[:head]}\n…(中间省略 {len(text) - limit} 字)…\n{text[-tail:]}")


def _short_args(args: dict) -> dict:
    out = {}
    for k, v in list(args.items())[:6]:
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s if not isinstance(s, str) or len(s) <= 120 else s[:120] + "…"  # i18n-keep: U+2026 ellipsis; correct in English too
    return out
