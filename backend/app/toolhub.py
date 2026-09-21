"""Unified tool dispatch: built-in tools + plugin tools + MCP tools, giving each member a list
of what is usable based on "what this group has enabled".

Built-in tools: current_time / library_search / library_read / memory_search / memory_save /
run_code (only when "let members run code" is on) / generate_video (only when video generation is
on and a video provider is reachable). Whether they are available depends on the group settings
(library switch, memory switch), while plugins and MCP tools only become usable once ticked for
the group — ticking is your authorization for "let members of this group call it on their own".
Every call is recorded in the message's tool trace and is visible below the bubble.
"""

from __future__ import annotations

from . import coderun, i18n, video

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .approvals import policy_for
from .library import Library
from .mcp_client import McpManager, pick_transport, slug
from .memory import MemoryService, looks_sensitive
from .store import Store
from .tools import ToolRegistry


@dataclass
class ToolOutcome:
    text: str
    ok: bool = True
    ms: int = 0
    denied: bool = False       # blocked by permissions (denied by the user, timed out unconfirmed, or forbidden):
# never really executed
    # Files the call produced, so the UI can offer them instead of only printing a path.
    # [{"kind": "video", "name": "...", "bytes": 123}] — where they live is the group's workspace,
    # which the UI already knows how to reach.
    files: list[dict] = field(default_factory=list)


@dataclass
class ToolContext:
    group: dict
    agent: dict
    tools: dict[str, dict] = field(default_factory=dict)   # name -> spec (including source / server_id)
    problems: list[str] = field(default_factory=list)      # connection failures and the like, shown to the user
    # At least one MCP server has merely "not been connected for the first time yet", which is
# not a real error. The UI shows one extra note based on this;
    # a flag is used rather than matching on wording, because the text in problems follows
# the request language.
    mcp_deferred: bool = False

    def specs(self) -> list[dict]:
        return list(self.tools.values())


BUILTIN_SPECS: dict[str, dict] = {
    # English is the canonical text and `<field>_zh` the Chinese wording; the specs are run
    # through `i18n.localize()` where they are turned into a tool list, so the description the
    # model and the UI see follows the request language. (Calling pick_now in here would be
    # evaluated once at import and freeze whichever language was current then.)
    "current_time": {
        "description": "Get the current local date and time",
        "description_zh": "获取当前本地日期和时间",
        "risk": "read",
        "parameters": {"type": "object", "properties": {}},
    },
    "library_search": {
        "description": "Search the library for passages relevant to a question (returns document "
                       "titles and the original text). Reach for this first whenever you need to "
                       "cite a fact, a figure or a rule.",
        "description_zh": "在资料库里检索与问题相关的片段(返回文档标题和原文)。需要引用事实、数据、"
                          "规定时先用它。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords or a question to search for",
                      "description_zh": "检索关键词或问题"},
            "top_k": {"type": "integer", "description": "How many passages to return; 5 by default",
                      "description_zh": "返回片段数,默认 5"}}, "required": ["query"]},
    },
    "library_read": {
        "description": "Read a document from the library by title, one chunk at a time.",
        "description_zh": "按标题读取资料库中某份文档的原文(分段读取)。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {
            "doc": {"type": "string", "description": "Document title or ID",
                    "description_zh": "文档标题或 ID"},
            "start": {"type": "integer", "description": "Character offset to start from; 0 by default",
                      "description_zh": "从第几个字符开始,默认 0"}}, "required": ["doc"]},
    },
    "memory_search": {
        "description": "Search long-term memory (user preferences, earlier decisions, past practice).",
        "description_zh": "检索长期记忆(用户偏好、以往决定、过往做法)。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    "memory_save": {
        "description": "Record one durably useful piece of information in this group's memory (a "
                       "preference, a decision, a lesson). Never record keys, passwords or personal "
                       "data.",
        "description_zh": "把一条长期有用的信息记入本群记忆(偏好、决定、教训)。不要记密钥、密码和个人隐私。",
        # The risk lives with the spec so that every consumer — dispatch and the Permissions
        # page alike — sees the same value. Building a spec by hand and letting risk_of infer
        # from the name is how a new built-in ends up advertised as read-only.
        "risk": "write",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "One sentence, at most 120 characters",
                        "description_zh": "一句话,不超过 120 字"},
            "kind": {"type": "string", "enum": ["preference", "fact", "decision", "lesson"]}},
            "required": ["content"]},
    },
    "run_code": {
        "description": "Write a program and run it, then read its output. Use it to calculate "
                       "something, transform a file, or try a snippet out. The working directory "
                       "is a private workspace: files you write there stay there. There is no "
                       "interaction and no long-running process — the run is killed when it "
                       "times out. Python is the usual choice; shell is for short commands.",
        "description_zh": "写一段程序并运行,再读它的输出。适合做计算、转换文件、试一小段代码。工作目录是"
                          "专属的:你在里面写的文件就留在里面。不支持交互和长时间运行的进程,超时会被终止。"
                          "一般用 python,shell 适合短命令。",
        # Every built-in carries its own `risk` (see BUILTIN_SPECS); this is the one place
        # that copies it into the per-call spec, so dispatch and the Permissions page agree.
        "risk": "exec",
        "parameters": {"type": "object", "properties": {
            "language": {"type": "string", "enum": ["python", "shell"],
                         "description": "python or shell", "description_zh": "python 或 shell"},
            "code": {"type": "string", "description": "The program text",
                     "description_zh": "程序正文"},
            "cwd": {"type": "string",
                    "description": "Optional subdirectory of the workspace to run in",
                    "description_zh": "可选:工作目录下的子目录,在这里运行"}},
            "required": ["language", "code"]},
    },
    "generate_video": {
        "description": "Generate a short video with sound through the video-generation model this "
                       "machine serves (MiniMax H3). Describe what should happen, and write it the "
                       "way H3 was trained to read it: name the shots, then the sound — for "
                       "example \"[Shot 1] ... [Shot 2] ... overall_soundscape: ... "
                       "non_diegetic_music: ...\". A clip is 4-15 seconds and rendering takes a "
                       "while; the file lands in this group's workspace. Pass the path of an image "
                       "from the workspace as first_frame to turn it into image-to-video. You "
                       "cannot watch or hear the result: say what you asked for, never describe "
                       "what came out.",
        "description_zh": "用本机所服务的视频生成模型(MiniMax H3)生成一段带声音的短视频。描述要发生的事,"
                          "并写成 H3 被训练来读的格式:先分镜,再说声音 —— 例如「[Shot 1] … [Shot 2] … "
                          "overall_soundscape: … non_diegetic_music: …」。一段 4-15 秒,渲染需要等一会儿,"
                          "文件会落在本群工作目录里。把工作目录里某张图片的路径填到 first_frame 可以变成"
                          "图生视频。你看不到也听不到结果:只说你要求了什么,绝不要描述生成出来的画面。",
        # Every built-in carries its own `risk`: it runs something outside this app, so it asks.
        "risk": "exec",
        # Its own budget. Rendering is minutes, while `tool_timeout` is sized for a tool call that
        # answers quickly — without this the call would be killed mid-render.
        "timeout_key": "video_timeout",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "What to generate: shots, then soundscape and music",
                       "description_zh": "要生成什么:分镜,再写声音环境与配乐"},
            "duration_seconds": {"type": "integer",
                                 "description": f"Clip length, {video.MIN_SECONDS}-{video.MAX_SECONDS} seconds; the longest this group allows is used when omitted",
                                 "description_zh": f"时长,{video.MIN_SECONDS}-{video.MAX_SECONDS} 秒;不填则用本群允许的最长时长"},
            "aspect_ratio": {"type": "string", "enum": list(video.ASPECT_RATIOS),
                             "description": f"One of: {', '.join(video.ASPECT_RATIOS)}; 16:9 by default",
                             "description_zh": f"可选:{', '.join(video.ASPECT_RATIOS)},默认 16:9"},
            "first_frame": {"type": "string",
                            "description": "Optional image to start from: a path inside the workspace, or an http(s)/file URL",
                            "description_zh": "可选:起始图片,工作目录内的路径,或 http(s)/file 地址"},
            "last_frame": {"type": "string",
                           "description": "Optional image to end on, same forms as first_frame",
                           "description_zh": "可选:结束图片,写法同 first_frame"},
            "seed": {"type": "integer", "description": "0 for a random result",
                     "description_zh": "填 0 表示随机"}},
            "required": ["prompt"]},
    },
}

# Every spec is handed out through here so the descriptions follow the request language.
BUILTIN_TOOL_NAMES: tuple[str, ...] = tuple(BUILTIN_SPECS)


def builtin_specs() -> dict[str, dict]:
    """The built-in tool specs, described in the request language."""
    return i18n.localize(BUILTIN_SPECS)


def timeout_budget(cfg: dict, spec: dict) -> float:
    """How long this tool may run for.

    A spec may name its own setting (`timeout_key`): rendering a video takes minutes, while
    `tool_timeout` is sized for a call that answers quickly. Without the override the video tool
    would be killed halfway through a render the GPU has already done the work for.
    """
    key = spec.get("timeout_key")
    return float(cfg[key]) if key else float(cfg["tool_timeout"])



class ToolHub:
    def __init__(self, store: Store, registry: ToolRegistry, mcp: McpManager, library: Library, memory: MemoryService):
        self.store, self.registry, self.mcp, self.library, self.memory = store, registry, mcp, library, memory

    # ----------------------------------------------------------- listing
    def _mcp_name(self, server: dict, tool: str, taken: set[str]) -> str:
        base = f"mcp__{slug(server['name'])}__{tool}"
        name = base if base not in taken else f"{base}_{server['id'][:4]}"
        taken.add(name)
        return name

    async def context(self, group: dict, agent: dict, connect: bool = True) -> ToolContext:
        cfg = self.store.get_settings()
        ctx = ToolContext(group, agent)
        if int(cfg["tool_rounds"]) <= 0:
            return ctx
        ext = group["ext"]

        specs = builtin_specs()          # descriptions in the request language

        def add(name: str, spec: dict, **extra: Any) -> None:
            ctx.tools[name] = {"name": name, "description": spec["description"], "parameters": spec["parameters"],
                               "risk": spec.get("risk"), **extra}

        add("current_time", specs["current_time"], source="builtin")
        # "The library is not empty for this group" now means "at least one knowledge base is in
        # scope and has an enabled document", not "the database has documents".
        if self.library.scope_ids(ext["library"], group["id"]):
            add("library_search", specs["library_search"], source="builtin")
            add("library_read", specs["library_read"], source="builtin")
        if cfg["memory_enabled"] and ext["memory"]:
            add("memory_search", specs["memory_search"], source="builtin")
            add("memory_save", specs["memory_save"], source="builtin")
        if cfg["code_enabled"]:
            add("run_code", specs["run_code"], source="builtin")
        if cfg["video_enabled"]:
            # Offered only when there is somewhere to generate. A member handed the tool without a
            # reachable server would keep retrying and report a failure that looks like its own
            # fault; the user gets the actual reason instead.
            vprov, why = video.pick_provider(self.store, cfg)
            blocked = video.blocked_by_offline(vprov, cfg) if vprov else ""
            if why or blocked:
                ctx.problems.append(why or blocked)
            else:
                add("generate_video", specs["generate_video"], source="builtin")
        for t in self.registry.plugin_tools(ext["plugins"]):
            if t.name in ctx.tools or t.name in BUILTIN_TOOL_NAMES:   # a plugin cannot displace a built-in tool (permission checks go by name)
                ctx.problems.append(i18n.pick_now(f"The plugin tool \"{t.name}\" has the same name as a built-in tool, so it was ignored.", f"插件工具「{t.name}」和内置工具重名,已忽略。"))
                continue
            ctx.tools[t.name] = {**t.spec(), "source": "plugin"}
        taken = set(ctx.tools)
        for sid in ext["mcp"]:
            server = self.store.get_mcp(sid)
            if not server or not server["enabled"]:
                continue
            if pick_transport(server) != "stdio" and not cfg["external_calls_enabled"]:   # remote MCP counts as outbound communication too
                ctx.problems.append(i18n.pick_now(f"MCP \"{server['name']}\" is a remote service and outbound calls are switched off, so it was not used this time.", f"MCP「{server['name']}」是远程服务,而「允许外呼」是关的,本次没有使用。"))
                continue
            st = self.mcp.state(sid)
            if (not st or st.status != "ready") and connect:
                st = await self.mcp.connect(server, timeout=30)
            if not st or st.status != "ready":
                if not (st and st.error):                              # simply never connected yet, not an error
                    ctx.mcp_deferred = True
                ctx.problems.append(i18n.pick_now(f"MCP \"{server['name']}\" is not connected: {(st.error if st else '') or 'not connected yet'}", f"MCP「{server['name']}」未连接:{(st.error if st else '') or '尚未连接'}"))
                continue
            for t in st.tools:
                name = self._mcp_name(server, t["name"], taken)
                ctx.tools[name] = {"name": name, "description": f"[{server['name']}] {t['description']}",
                                   "parameters": t["parameters"], "source": "mcp", "server_id": sid,
                                   "tool": t["name"], "read_only": t.get("read_only", False)}
        return ctx

    # ----------------------------------------------------------- calls
    def policy(self, spec: dict) -> str:
        return policy_for(self.store.get_settings(), spec)

    async def call(
        self, ctx: ToolContext, name: str, args: dict[str, Any],
        approve: Callable[[dict, dict], Awaitable[bool]] | None = None,
    ) -> ToolOutcome:
        """approve: calls that need confirmation are handed to it to ask the user (True = allowed).
When it is not supplied, calls needing confirmation are always denied."""
        t0 = time.time()
        spec = ctx.tools.get(name)
        if not spec:
            return ToolOutcome(i18n.pick_now(f"There is no tool called {name} (or it is not enabled for this group). Available tools: {', '.join(ctx.tools) or 'none'}", f"没有名为 {name} 的工具(或本群未启用)。可用工具:{', '.join(ctx.tools) or '无'}"), False)
        req = (spec["parameters"] or {}).get("required") or []
        missing = [r for r in req if r not in args]
        if missing:
            return ToolOutcome(i18n.pick_now(f"Missing required arguments: {', '.join(missing)}", f"缺少必填参数:{', '.join(missing)}"), False)
        pol = self.policy(spec)
        if pol == "deny":
            return ToolOutcome(i18n.pick_now(f"Tool {name} is blocked by the user under Permissions & control, so it was not run. Do not retry — find another way, or tell the user directly.", f"工具 {name} 已被用户在「权限与操控」里禁止,没有执行。不要重试,请换个办法或直接告诉用户。"), False, 0, True)
        if pol == "ask" and not (approve and await approve(spec, args)):
            return ToolOutcome(
                i18n.pick_now(f"The user did not approve this call ({name}: denied, or no confirmation before the timeout), so it was not run. Do not retry the same operation — find another way, or tell the user what you need and why.", f"用户没有批准这次调用({name}:拒绝或超时未确认),没有执行。不要重试同一操作,请换个办法,或直接告诉用户你需要做什么、为什么。"),
                False, int((time.time() - t0) * 1000), True,
            )
        if pol == "ask" and self.policy(spec) == "deny":   # while waiting for confirmation the user changed it to "forbidden"
            return ToolOutcome(i18n.pick_now(f"Tool {name} is blocked by the user under Permissions & control, so it was not run.", f"工具 {name} 已被用户在「权限与操控」里禁止,没有执行。"), False, 0, True)
        t0 = time.time()  # elapsed time excludes the wait for user confirmation
        timeout = timeout_budget(self.store.get_settings(), spec)
        try:
            text, ok, files = await asyncio.wait_for(self._dispatch(ctx, spec, args, timeout), timeout + 5)
        except asyncio.TimeoutError:
            self._note_unstoppable(spec)
            text, ok, files = i18n.pick_now(f"Tool execution timed out ({int(timeout)} seconds)", f"工具执行超时({int(timeout)} 秒)"), False, []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            text, ok, files = i18n.pick_now(f"Tool execution failed: {type(e).__name__}: {e}", f"工具执行出错:{type(e).__name__}: {e}")[:500], False, []
        return ToolOutcome(text, ok, int((time.time() - t0) * 1000), False, files)

    def _note_unstoppable(self, spec: dict) -> None:
        """A plugin tool that is a plain function runs in a worker thread (see `tools.py`), and a
        thread cannot be cancelled: the timeout above ends the *wait*, not the work, so the plugin may
        still be writing files or spawning processes. Nothing here can stop it — code that has to be
        killable belongs in a subprocess (`coderun`) — so at least leave a trace."""
        if spec.get("source") == "plugin":
            print(f"plugin tool {spec.get('name')!r} is still running in a worker thread after its "
                  f"timeout; the side effects of that call were not stopped")

    async def _dispatch(self, ctx: ToolContext, spec: dict, args: dict, timeout: float) -> tuple[str, bool, list[dict]]:
        """Always (text, ok, files): whether the tool produced files is not a special case."""
        src, name = spec["source"], spec["name"]
        if src == "mcp":
            text, ok = await self.mcp.call_tool(spec["server_id"], spec["tool"], args, timeout)
            return text, ok, []
        if src == "plugin":
            res = await self.registry.call(name, args)
            return (res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)), True, []
        if name == "generate_video":
            return await self._generate_video(ctx, args)
        text, ok = await self._builtin(ctx, name, args)
        return text, ok, []

    # ----------------------------------------------------------- video generation
    async def _generate_video(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Render one clip through the video provider, into this group's workspace.

        Every refusal here says what is actually wrong, because the alternative — handing the
        model a generic "failed" — makes it retry and then blame itself.
        """
        cfg = self.store.get_settings()
        prov, why = video.pick_provider(self.store, cfg)
        if prov is None:
            return why, False, []
        blocked = video.blocked_by_offline(prov, cfg)
        if blocked:
            return blocked, False, []
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return i18n.pick_now("The prompt was empty, so nothing was generated.", "提示词是空的,没有生成。"), False, []
        ratio = str(args.get("aspect_ratio") or video.DEFAULT_ASPECT).strip()
        if ratio not in video.ASPECT_RATIOS:
            return i18n.pick_now(
                f"\"{ratio}\" is not an aspect ratio this model produces, so nothing was generated. Use one of: {', '.join(video.ASPECT_RATIOS)}.",
                f"「{ratio}」不是这个模型支持的画幅,没有生成。可用:{', '.join(video.ASPECT_RATIOS)}。",
            ), False, []
        # The workspace is created here rather than assumed to exist: this is the first thing a
        # fresh group does with it.
        workspace = coderun.workspace_dir(Path(self.store.data_dir), cfg, ctx.group["id"])
        try:
            first = video.frame_uri(str(args.get("first_frame") or ""), workspace)
            last = video.frame_uri(str(args.get("last_frame") or ""), workspace)
        except video.VideoError as e:
            return str(e), False, []
        seconds, clamped = video.clamp_seconds(args.get("duration_seconds"), int(cfg["video_max_seconds"]))
        payload = video.build_payload(
            prompt, short_edge=int(cfg["video_short_edge"]), aspect_ratio=ratio, duration_seconds=seconds,
            seed=int(args.get("seed") or 0), first_frame=first, last_frame=last,
        )
        try:
            r = await video.generate(
                prov, payload, workspace=workspace,
                max_bytes=max(1, int(cfg["video_max_mb"])) * 1024 * 1024,
                deadline_s=float(cfg["video_timeout"]),
            )
        except video.VideoError as e:
            return str(e), False, []
        # KB below a megabyte: a short 768p clip really can be a few hundred KB, and "0.0 MB"
        # reads like something went wrong.
        size = (f"{r['bytes'] / 1024:.0f} KB" if r["bytes"] < 1024 * 1024
                else f"{r['bytes'] / 1024 / 1024:.1f} MB")
        lines = [
            i18n.pick_now(
                f"Rendered a {seconds}s {ratio} clip with sound using {prov['name']}: {r['name']} "
                f"({size}, took {r['seconds']:.0f}s).",
                f"用 {prov['name']} 生成了一段 {seconds} 秒、{ratio} 的带声音视频:{r['name']}"
                f"({size},用了 {r['seconds']:.0f} 秒)。",
            ),
            i18n.pick_now(f"Saved in this group's workspace: {r['path']}", f"已保存在本群工作目录:{r['path']}"),
        ]
        if clamped:
            lines.append(i18n.pick_now(
                f"(Length was adjusted to {seconds}s: this group's limit is {int(cfg['video_max_seconds'])}s "
                f"and the model itself accepts {video.MIN_SECONDS}-{video.MAX_SECONDS}s.)",
                f"(时长已调整为 {seconds} 秒:本群上限是 {int(cfg['video_max_seconds'])} 秒,模型本身支持 "
                f"{video.MIN_SECONDS}-{video.MAX_SECONDS} 秒。)",
            ))
        # Without this line the model tends to narrate what "happened" in a video it never saw.
        lines.append(i18n.pick_now(
            "You cannot watch or hear the result, so do not describe what happens in it — tell the "
            "user it is ready and where it is.",
            "你看不到也听不到生成结果,不要描述里面的内容 —— 只要告诉用户已经生成好了、文件在哪里。",
        ))
        return "\n".join(lines), True, [{"kind": "video", "name": r["name"], "bytes": r["bytes"], "seconds": seconds}]

    async def _builtin(self, ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
        group, agent = ctx.group, ctx.agent
        if name == "current_time":
            return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %A (%z)"), True
        if name == "library_search":
            k = max(1, min(int(args.get("top_k") or self.store.get_settings()["library_top_k"]), 10))
            # BM25 scoring is pure CPU work, and the first search over a large library also rebuilds the
# index; run it in a thread pool so it cannot block the event loop (which would slow down
# everyone's streaming output)
            hits = await asyncio.to_thread(
                self.library.search, str(args["query"]), k, self.library.scope_ids(group["ext"]["library"], group["id"])
            )
            if not hits:
                return i18n.pick_now("Nothing relevant was found in the library.", "资料库里没有找到相关内容。"), True
            return "\n\n".join(i18n.pick_now(f"[{h['title']} · passage {h['idx'] + 1}]\n{h['text'][:900]}", f"[《{h['title']}》第 {h['idx'] + 1} 段]\n{h['text'][:900]}") for h in hits), True
        if name == "library_read":
            doc = self.library.find_by_title(str(args["doc"]))
            if not doc or not doc["enabled"]:
                return i18n.pick_now(f"The library has no document called {args['doc']}.", f"资料库里没有《{args['doc']}》。"), False
            allowed = self.library.scope_ids(group["ext"]["library"], group["id"])
            if allowed is not None and doc["id"] not in allowed:
                return i18n.pick_now("This document is not enabled for this group.", "本群没有启用这份文档。"), False
            r = await asyncio.to_thread(self.library.read, doc["id"], max(0, int(args.get("start") or 0)), 3000)
            tail = i18n.pick_now(f"\n(read up to character {r['end']} of {r['total']}; pass start={r['end']} to carry on)", f"\n(已读到第 {r['end']} 字,共 {r['total']} 字;继续读请用 start={r['end']})") if r["end"] < r["total"] else ""
            return i18n.pick_now(f"{doc['title']}\n{r['text']}{tail}", f"《{doc['title']}》\n{r['text']}{tail}"), True
        if name == "memory_search":
            mems = self.memory.recall(group["id"], agent["id"], str(args["query"]), 8)
            return (self.memory.block(mems) or i18n.pick_now("No relevant memories.", "没有相关记忆。")), True
        if name == "memory_save":
            content = str(args["content"]).strip()
            if not content or len(content) > 120:
                return i18n.pick_now("The content is empty, or longer than 120 characters.", "内容为空或超过 120 字。"), False
            if looks_sensitive(content):
                return i18n.pick_now("The content looks like a key, a password or a long digit string, so it was not saved.", "内容像是密钥/密码/长数字串,出于安全没有保存。"), False
            kind = args.get("kind") if args.get("kind") in ("preference", "fact", "decision", "lesson") else "fact"
            self.memory.save_manual(content, "group", group["id"], kind, "auto")
            return i18n.pick_now("Saved to this group's memory.", "已记入本群记忆。"), True
        if name == "run_code":
            cfg = self.store.get_settings()
            # Each group runs in its own workspace, so one project's files are never in reach of
            # another's code.
            workspace = coderun.workspace_dir(Path(self.store.data_dir), cfg, group["id"])
            cwd, why = coderun.resolve_cwd(workspace, str(args.get("cwd") or ""))
            if cwd is None:
                return why, False
            limit = max(500, int(cfg["tool_output_limit"]))
            return await coderun.run(
                str(args.get("language") or "python"), str(args.get("code") or ""),
                cwd, workspace, max(1.0, float(cfg["code_timeout"])), limit,
            )
        return i18n.pick_now(f"Built-in tool {name} is not implemented", f"未实现的内置工具 {name}"), False
