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
import re
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import external, planner, scoring, vision
from . import attachments as attachments_lib
from . import coderun
from .approvals import Approvals
from .external import ExternalError, ExternalRunner
from .library import Library
from .mcp_client import McpManager
from .memory import MemoryService
from .presets import twin_name
from .prompting import PromptBuilder
from .router import AllRoutesFailed, ModelRouter
from .store import Store, new_id
from .toolcall import TagFilter, format_result, parse_tool_calls, strip_hidden, tools_prompt
from .toolhub import ToolHub
from .tools import ToolRegistry

Emit = Callable[[dict], Awaitable[None]]

# Streaming delta coalescing: models usually emit one chunk per 1-3 characters, so
# forwarding them as-is turns a 1000-character answer into several hundred WebSocket frames
# and as many front-end re-renders. Send only once DELTA_BATCH_CHARS characters have
# accumulated, or DELTA_BATCH_SECONDS has passed since the last send; the concatenation is
# identical to the original, while frame count and re-render count drop by an order of magnitude.
DELTA_BATCH_CHARS = 24
DELTA_BATCH_SECONDS = 0.06

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


# The host says this when it produced a plan but no prose. Kept as a pair so the value
# follows the request language instead of freezing at import time.
PLAN_FALLBACK = ("The work is split — see the task board.", "已做好分工,见任务板。")


def plan_fallback() -> str:
    """The stand-in line for a host turn that only produced a plan."""
    return i18n.pick_now(*PLAN_FALLBACK)


@dataclass
class RunState:
    """Record of one user message from start to finish (used for memory and statistics)."""
    gid: str
    user_text: str
    started: float = field(default_factory=time.time)
    steps: list[dict] = field(default_factory=list)
    final_text: str = ""
    refs_block: str = ""
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
        self.external = external_runner or ExternalRunner(store.data_dir)
        self.router = router
        self.library = library or Library(store)
        self.memory = memory or MemoryService(store, router)
        self.registry = registry or ToolRegistry()
        self.mcp = mcp or McpManager()
        self.toolhub = toolhub or ToolHub(store, self.registry, self.mcp, self.library, self.memory, hooks=hooks)
        self.prompts = prompts or PromptBuilder(store, router)
        self.approvals = approvals or Approvals(store)
        # The user's own hooks (see app/hooks.py). Optional everywhere so a test can build an
        # orchestrator without one; the channel layer reads it from here.
        self.hooks = hooks
        self._locks: dict[str, asyncio.Lock] = {}
        self._bg: set[asyncio.Task] = set()
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
    ) -> list[dict]:
        cfg = self.store.get_settings()
        history = self.store.list_messages(group["id"], int(cfg["history_limit"]))
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
            body = clip_middle(text, self.REF_FILE_CHARS) if text else ""
            tail = i18n.pick_now(" (excerpt; read the whole file in the workspace if you need it)",
                                 "(节选,需要完整内容可在工作目录里读)")
            return f"{header}\n{body}{tail if text else ''}".strip(), []
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
        if not text:
            return f"{header} — {vision.reason_missing(self.store, self.router)}", []
        return f"{header}\n{text}", []

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

    def _refs_block(self, group: dict, text: str, workspace: Path) -> str:
        """What the user asked for by reference, as text.

        Documents the knowledge base can reach, files and folders in this group's workspace, and
        earlier messages of this conversation. Everything is clipped into a budget, and whatever is
        clipped says so together with the path, because the member can go and read the rest.
        """
        budget = int(self.store.get_settings()["refs_budget"])
        out: list[str] = []
        used = 0

        def take(chunk: str) -> bool:
            nonlocal used
            if used + len(chunk) > budget:
                return False
            out.append(chunk)
            used += len(chunk)
            return True

        allowed = None
        if group["ext"]["library"]["mode"] != "off":
            allowed = self.library.scope_ids(group["ext"]["library"], group["id"])
            for m in re.finditer(r"#([^\s#@,,。;;::!!??]{2,40})", text):   # i18n-keep: hashtag regex; the CJK punctuation set is the delimiter list
                doc = self.library.find_by_title(m.group(1))
                if doc and doc["enabled"] and (allowed is None or doc["id"] in allowed):
                    take(self._library_chunk(doc))
                if len(out) >= 3:
                    break

        for ref in self._parse_refs(text):
            if ref["kind"] == "doc":
                doc = self.store.get_doc(ref["value"])
                if doc and doc["enabled"] and (allowed is None or doc["id"] in allowed):
                    take(self._library_chunk(doc))
            elif ref["kind"] == "msg":
                chunk = self._message_chunk(group, ref["value"])
                if chunk:
                    take(chunk)
            elif ref["kind"] == "dir":
                chunk = self._folder_chunk(workspace, ref["value"])
                if chunk:
                    take(chunk)
        if not out:
            return ""
        head = i18n.pick_now("[What the user referenced]\n", "【用户引用的内容】\n")
        return head + "\n\n".join(out)

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
        # The name the user is labelled with in the transcript; resolved per request
        # rather than as a default argument, which is evaluated once at import time.
        sender_name = sender_name or i18n.pick_now("me", "我")
        group = self.store.get_group(gid)
        if not group:
            return
        user_msg = self.store.add_message(gid, "user", "user", sender_name, text,
                                          meta={"files": files} if files else None)
        await emit({"type": "message", "message": user_msg})
        # `add_message` and the broadcast stay outside the lock so the sender sees their own message
        # immediately — which means another message can land while this round waits for the group
        # lock. That later round reads both (the history is the whole group) and answers both, so this
        # one stands down: answering as well would answer the same question twice, and whichever round
        # grabbed the lock first would be replying to the *other* message's text.
        async with self._lock(gid):
            group = self.store.get_group(gid) or group
            if self.store.has_later_user_message(gid, user_msg["id"]):
                return
            run = RunState(gid, text, read_only=read_only)
            run.files = list(files or [])
            workspace = self.workspace(gid)
            run.refs_block = self._refs_block(group, text, workspace)
            self._notify("round.start", gid, group,
                         {"sender": sender_name, "chars": len(text),
                          "read_only": read_only, "files": len(files or [])})
            await self._run_turns(group, text, emit, run)
            self._after_run(group, run)
        await self._announce(gid, run)
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
        """
        return coderun.workspace_dir(self.store.data_dir, self.store.get_settings(), gid)

    # ------------------------------------------------------------------ per-task folders
    def _task_dir(self, group: dict, task: Any) -> str:
        """The folder this task delivers into, inside the group's workspace.

        A task is a unit of work, so it gets a place of its own: two tasks running in parallel write
        into different folders instead of overwriting each other's files, and the user can see
        which part of the result came from which part of the plan.
        """
        workspace = self.workspace(group["id"])
        rel = f"tasks/{coderun.safe_slug(task.title or task.id, task.id)}"
        try:
            coderun.make_dir(workspace, workspace / rel)
        except (OSError, ValueError) as e:
            print("could not create the task folder:", e)
            return ""
        return rel

    def _task_dir_note(self, task: Any) -> str:
        """Tell the member where its own files go — and that everyone shares the workspace."""
        if not getattr(task, "dir", ""):
            return ""
        return i18n.pick_now(
            f"\n\n[Your folder] `{task.dir}/` inside the group workspace. Put anything you produce "
            "there, and mention the paths in your answer so the others can pick them up.",
            f"\n\n【你的交付目录】群工作目录下的 `{task.dir}/`。你产出的文件放这里,并在回答里写明路径,"
            "方便其他人接手。")

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
        host = self._pick_host(group, members)

        if mentions_all(text):
            queue = deque(members)
            explicit = True
        else:
            queue = deque(find_mentions(text, members))
            explicit = bool(queue)
        hops = 0

        mode = group["ext"]["plan"] if group["ext"]["plan"] != "inherit" else cfg["plan_mode"]
        if not explicit and mode != "off" and len(members) >= 2 and int(cfg["plan_max_tasks"]) >= 2 and not host.get("engine"):
            hops = 1
            out = await self._planning_turn(group, members, host, text, mode, emit, run)
            if out is None:
                return
            if out is not True:  # True = execution followed the plan; otherwise out is the owner's ordinary reply and hands off
                run.final_text = out.text
                for m in find_mentions(out.text, members, exclude_id=host["id"]):
                    queue.append(m)
            else:
                return
        elif not queue:
            queue.append(host)

        max_hops = int(cfg["max_hops"])
        while queue and hops < max_hops:
            agent = queue.popleft()
            hops += 1
            out = await self._agent_turn(group, agent, members, emit, run)
            if out is None:
                continue
            run.final_text = out.text
            queued = {a["id"] for a in queue}
            if agent.get("engine") and not (agent.get("engine_cfg") or {}).get("handoff", True):
                continue   # this external agent is configured not to hand off: the @ in its reply is just text
            for m in find_mentions(out.text, members, exclude_id=agent["id"]):
                if m["id"] not in queued:
                    queue.append(m)
                    queued.add(m["id"])
        if queue:
            names = i18n.pick_now(", ", "、").join(a["name"] for a in queue)
            await self._system(
                group["id"], i18n.pick_now(f"Reached the limit of {max_hops} turns for one message, so {names} was paused. Send another message to carry on.", f"已达到单次最大发言轮数({max_hops}),{names} 的发言被暂停。可再发一条消息继续。"), emit
            )

    @staticmethod
    def _pick_host(group: dict, members: list[dict]) -> dict:
        """The owner must be a model member: an external agent does not delegate through the <plan>
protocol and should not decide what the others do."""
        host = next((m for m in members if m["id"] == group.get("host_agent_id")), None)
        if host is None or host.get("engine"):
            host = next((m for m in members if not m.get("engine")), host or members[0])
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
        instruction = planner.planning_instruction(int(cfg["plan_max_tasks"]), mode, past)
        out = await self._agent_turn(
            group, host, members, emit, run, extra_user=instruction,
            empty_fallback=plan_fallback(),
        )
        if out is None:
            return None
        try:
            obj = planner.extract_plan_json(out.raw)
        except planner.PlanError as e:
            await self._plan_failed(group["id"], out, i18n.pick_now(f"The host's plan was malformed ({e}); falling back to ordinary turn-taking.", f"群主的分工计划格式不对({e}),已改用普通接力模式。"), emit)
            return out
        if obj is None:
            if mode == "on":
                await self._system(group["id"], i18n.pick_now("This group is set to always split the work, but the host produced no plan; treating its reply as an ordinary answer.", "本群设为「总是先分工」,但群主没有给出计划,已按普通回复处理。"), emit)
            return out
        # the owner already connected MCP this round; unknown tool names in the plan are simply
# ignored rather than treated as an error
        # The same restriction the members' own tool lists get: on a read-only round the host
        # must not plan work that needs run_code or another exec tool, or it would hand out
        # tasks whose tool has already been withheld.
        known = {t["name"] for t in (await self.toolhub.context(group, host, connect=False,
                                                               read_only=run.read_only)).specs()}
        try:
            plan = planner.build_plan(obj, members, int(cfg["plan_max_tasks"]), known)
        except planner.PlanError as e:
            await self._plan_failed(group["id"], out, i18n.pick_now(f"The host's plan was not valid ({e}); falling back to ordinary turn-taking.", f"群主的分工计划不合规({e}),已改用普通接力模式。"), emit)
            return out
        await self._execute_plan(group, members, host, plan, run, emit)
        return True

    async def _execute_plan(
        self, group: dict, members: list[dict], host: dict, plan: planner.Plan, run: RunState, emit: Emit
    ) -> None:
        gid = group["id"]
        pm = self.store.add_message(gid, "plan", None, i18n.pick_now("Task board", "任务板"), planner.summarize(plan), meta=plan.to_meta())
        plan.message_id = pm["id"]
        await emit({"type": "message", "message": pm})
        by_id = {m["id"]: m for m in members}
        outputs: dict[str, str] = {}

        async def push() -> None:
            m = self.store.update_message(plan.message_id, content=planner.summarize(plan), meta=plan.to_meta())
            await emit({"type": "plan", "message": m})

        try:
            for i, task in enumerate(plan.tasks, 1):
                agent = by_id.get(task.owner_id)
                if agent is None:
                    task.status, task.error = "failed", i18n.pick_now("the member has left the group", "成员已不在群里")
                    await push()
                    continue
                task.dir = await asyncio.to_thread(self._task_dir, group, task)
                task.status = "running"
                await push()
                out = await self._agent_turn(
                    group, agent, members, emit, run,
                    extra_user=planner.task_prompt(plan, task, outputs, i) + self._task_dir_note(task),
                    exclude_plan_id=plan.message_id,
                    extra_meta={"plan_id": plan.message_id, "task_id": task.id, "task_title": task.title},
                )
                if out is None:
                    task.status, task.error = "failed", i18n.pick_now("the model call failed", "模型调用失败")
                else:
                    task.status, task.message_id = "done", out.message["id"]
                    outputs[task.id] = out.text
                await push()
            plan.status = "integrating"
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
            plan.status = "done" if final is not None else "failed"
            if final is not None:
                run.final_text = final.text
            await push()
            # Grade the hand-offs only after the answer exists: the user gets the result first, and
            # a judge that is slow, unreachable or nonsensical can add a note but can never change
            # or delay what the group produced (see scoring.py — it never raises).
            plan.scorecard = await scoring.score_round(self.store, self.router, group, plan, outputs, run.steps)
            if plan.scorecard.get("tasks"):
                await push()                                  # the board carries the scores
                note = scoring.summarize_card(plan.scorecard)
                if note:
                    await self._system(gid, note, emit)
        except asyncio.CancelledError:
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
            for t in plan.tasks:
                if t.status in ("running", "pending"):
                    t.status, t.error = "failed", t.error or i18n.pick_now("the plan failed while running", "分工执行出错")
            try:
                await push()
            except Exception:  # noqa: BLE001
                pass
            await self._system(gid, i18n.pick_now(f"The plan failed while running: {e}", f"分工执行出错:{e}"), emit)

    # -------------------------------------------------------------- one turn
    async def _agent_turn(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *,
        extra_user: str | None = None, extra_meta: dict | None = None, exclude_plan_id: str | None = None,
        empty_fallback: str = "",
    ) -> TurnOut | None:
        cfg = self.store.get_settings()
        mid = new_id()
        base = {"id": mid, "group_id": group["id"], "sender_type": "agent",
                "sender_id": agent["id"], "sender_name": agent["name"]}
        await emit({"type": "message_start", "message": {**base, "content": "", "meta": extra_meta or {}}})
        if agent.get("engine"):
            return await self._external_turn(
                group, agent, members, emit, run, mid=mid, extra_user=extra_user, extra_meta=extra_meta,
                exclude_plan_id=exclude_plan_id, empty_fallback=empty_fallback,
            )

        visible_parts: list[str] = []
        raws: list[str] = []
        trace: list[dict] = []
        attempts: list[dict] = []
        res = None
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

        async def on_reset() -> None:
            nonlocal filt, pending_len, last_emit
            pending.clear()   # reset clears the screen immediately, so buffered content need not be sent again
            pending_len = 0
            filt = TagFilter()
            await emit({"type": "reset", "message_id": mid})
            earlier = "\n\n".join(visible_parts)
            if earlier:  # content already shown to the user in earlier rounds has to be added back
                await emit({"type": "delta", "message_id": mid, "text": earlier + "\n\n"})
            last_emit = time.monotonic()

        try:
            ctx = await self.toolhub.context(group, agent, read_only=run.read_only)
            for p in ctx.problems:
                if p not in run.warned:
                    run.warned.add(p)
                    await self._system(group["id"], p, emit)
            rounds = int(cfg["tool_rounds"]) if ctx.tools else 0
            memory_block = ""
            if cfg["memory_enabled"] and group["ext"]["memory"]:
                memory_block = self.memory.block(
                    self.memory.recall(group["id"], agent["id"], run.user_text + " " + (extra_user or "")[:300])
                )
            extra_system = run.refs_block
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
            )

            for rnd in range(rounds + 1):
                filt = TagFilter()
                if rnd and visible_parts:
                    await flush_delta()   # flush to disk before switching segments, so buffered text from the previous round cannot
# end up after the separator
                    await emit({"type": "delta", "message_id": mid, "text": "\n\n"})
                res = await self.router.complete(
                    messages, preferred=agent["model_id"], tags=agent.get("tags"),
                    on_delta=on_delta, on_reset=on_reset,
                )
                await flush_delta()   # wrap up: send the last incomplete batch of deltas, then handle a trailing tag
                tail = filt.flush()
                if tail:
                    await emit({"type": "delta", "message_id": mid, "text": tail})
                raws.append(res.text)
                attempts += [a.to_dict() for a in res.attempts]
                visible, calls = parse_tool_calls(res.text)
                visible = strip_hidden(visible)
                if visible:
                    visible_parts.append(visible)
                if not calls or rnd >= rounds:
                    break
                results = []
                for call in calls:
                    entry = {"name": call.name or i18n.pick_now("(malformed)", "(格式错误)"), "args": _short_args(call.arguments), "status": "running"}
                    trace.append(entry)
                    idx = len(trace) - 1
                    await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})

                    async def approve(spec: dict, args: dict, entry: dict = entry, idx: int = idx) -> bool:
                        entry["status"] = "waiting"   # the bubble shows "waiting for your confirmation"
                        await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})
                        allowed = await self.approvals.ask(group=group, message_id=mid, agent=agent, spec=spec, args=args, emit=emit)
                        entry["status"] = "running"
                        if allowed:
                            await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})
                        return allowed

                    denied = False
                    if call.error:
                        text, ok, ms, files = call.error, False, 0, []
                    else:
                        oc = await self.toolhub.call(ctx, call.name, call.arguments, approve)
                        text, ok, ms, denied, files = oc.text, oc.ok, oc.ms, oc.denied, oc.files
                    entry.update(status="denied" if denied else "ok" if ok else "failed", ms=ms, preview=text[:300])
                    if files:
                        entry["files"] = files      # what the call produced, so the bubble can offer it
                    await emit({"type": "tool", "message_id": mid, "index": len(trace) - 1, "call": dict(entry)})
                    results.append(format_result(call.name or "error", ok, text, int(cfg["tool_output_limit"])))
                messages.append({"role": "assistant", "content": res.text})
                messages.append({"role": "user", "content": "\n\n".join(results) + i18n.pick_now("\n\nCarry on based on the tool results.", "\n\n请基于工具结果继续。")})
        except AllRoutesFailed as e:
            detail = "; ".join(f"{a.model_id}:{a.detail}" for a in e.attempts) or i18n.pick_now("no model available", "没有可用模型")
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(
                group["id"], i18n.pick_now(f"{agent['name']} cannot reply right now: no model is available ({detail}).", f"「{agent['name']}」暂时无法回复,所有模型均不可用({detail})。"), emit
            )
            run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
            return None
        except asyncio.CancelledError:
            await emit({"type": "message_discard", "message_id": mid})
            raise
        except Exception as e:  # noqa: BLE001
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(group["id"], i18n.pick_now(f"{agent['name']} failed while replying: {e}", f"「{agent['name']}」发言出错:{e}"), emit)
            run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
            return None

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
        meta = {"attempts": attempts, **(extra_meta or {})}
        if trace:
            meta["tools"] = trace
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
            await self._system(gid, msg, emit)
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
        extra_system = run.refs_block
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

        try:
            res = await self.external.run(agent, system=system, prompt=prompt, on_delta=on_delta, on_tool=on_tool)
        except ExternalError as e:
            return await fail(i18n.pick_now(f"{name} could not reply: {e}", f"「{name}」没能回复:{e}"))
        except asyncio.CancelledError:
            await emit({"type": "message_discard", "message_id": mid})
            raise
        except Exception as e:  # noqa: BLE001
            return await fail(i18n.pick_now(f"{name} failed while replying: {e}", f"「{name}」发言出错:{e}"))

        await flush_ext_delta()   # wrap up: send the last incomplete batch of deltas
        content = res.text.strip() or empty_fallback or i18n.pick_now("(no reply content)", "(没有回复内容)")
        if self.hooks:
            reason, content = await self.hooks.gate_reply(
                gid, {"name": name, "model": f"ext:{agent['engine']}"}, content)
            if reason:
                return await fail(reason)
        meta: dict = {"engine": agent["engine"], "level": ecfg["level"], **(extra_meta or {})}
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

    async def _plan_failed(self, gid: str, out: "TurnOut", note: str, emit: Emit) -> None:
        """When the plan could not be executed, rewrite the owner's message that only says
"delegation is ready, see the task board", so it does not contradict the system note below."""
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
