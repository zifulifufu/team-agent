"""统一的工具调度:内置工具 + 插件工具 + MCP 工具,按「本群启用了什么」给每个成员一份可用清单。

内置工具:current_time / library_search / library_read / memory_search / memory_save。
它们是否可用取决于群设置(资料库开关、记忆开关),插件和 MCP 则要在群里勾选后才可用——
勾选就是你对「让这个群的成员自主调用它」的授权。每次调用都会记在消息的工具轨迹里,气泡下方能看到。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
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
    denied: bool = False       # 被权限拦下(用户拒绝、超时未确认、或被禁止),没有真正执行


@dataclass
class ToolContext:
    group: dict
    agent: dict
    tools: dict[str, dict] = field(default_factory=dict)   # 名称 -> spec(含 source / server_id)
    problems: list[str] = field(default_factory=list)      # 连接失败等,提示给用户看

    def specs(self) -> list[dict]:
        return list(self.tools.values())


BUILTIN_SPECS: dict[str, dict] = {
    "current_time": {
        "description": "获取当前本地日期和时间",
        "parameters": {"type": "object", "properties": {}},
    },
    "library_search": {
        "description": "在资料库里检索与问题相关的片段(返回文档标题和原文)。需要引用事实、数据、规定时先用它。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词或问题"},
            "top_k": {"type": "integer", "description": "返回片段数,默认 5"}}, "required": ["query"]},
    },
    "library_read": {
        "description": "按标题读取资料库中某份文档的原文(分段读取)。",
        "parameters": {"type": "object", "properties": {
            "doc": {"type": "string", "description": "文档标题或 ID"},
            "start": {"type": "integer", "description": "从第几个字符开始,默认 0"}}, "required": ["doc"]},
    },
    "memory_search": {
        "description": "检索长期记忆(用户偏好、以往决定、过往做法)。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "memory_save": {
        "description": "把一条长期有用的信息记入本群记忆(偏好、决定、教训)。不要记密钥、密码和个人隐私。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "一句话,不超过 120 字"},
            "kind": {"type": "string", "enum": ["preference", "fact", "decision", "lesson"]}}, "required": ["content"]},
    },
}


class ToolHub:
    def __init__(self, store: Store, registry: ToolRegistry, mcp: McpManager, library: Library, memory: MemoryService):
        self.store, self.registry, self.mcp, self.library, self.memory = store, registry, mcp, library, memory

    # ----------------------------------------------------------- 清单
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

        def add(name: str, spec: dict, **extra: Any) -> None:
            ctx.tools[name] = {"name": name, "description": spec["description"], "parameters": spec["parameters"], **extra}

        add("current_time", BUILTIN_SPECS["current_time"], source="builtin")
        if ext["library"]["mode"] != "off" and self.store.list_docs():
            add("library_search", BUILTIN_SPECS["library_search"], source="builtin")
            add("library_read", BUILTIN_SPECS["library_read"], source="builtin")
        if cfg["memory_enabled"] and ext["memory"]:
            add("memory_search", BUILTIN_SPECS["memory_search"], source="builtin")
            add("memory_save", BUILTIN_SPECS["memory_save"], source="builtin")
        for t in self.registry.plugin_tools(ext["plugins"]):
            if t.name in ctx.tools or t.name in BUILTIN_SPECS:   # 插件不能顶替内置工具(权限判断按名字走)
                ctx.problems.append(f"插件工具「{t.name}」和内置工具重名,已忽略。")
                continue
            ctx.tools[t.name] = {**t.spec(), "source": "plugin"}
        taken = set(ctx.tools)
        for sid in ext["mcp"]:
            server = self.store.get_mcp(sid)
            if not server or not server["enabled"]:
                continue
            if pick_transport(server) != "stdio" and not cfg["external_calls_enabled"]:   # 远程 MCP 也算对外通信
                ctx.problems.append(f"MCP「{server['name']}」是远程服务,而「允许外呼」是关的,本次没有使用。")
                continue
            st = self.mcp.state(sid)
            if (not st or st.status != "ready") and connect:
                st = await self.mcp.connect(server, timeout=30)
            if not st or st.status != "ready":
                ctx.problems.append(f"MCP「{server['name']}」未连接:{(st.error if st else '') or '尚未连接'}")
                continue
            for t in st.tools:
                name = self._mcp_name(server, t["name"], taken)
                ctx.tools[name] = {"name": name, "description": f"[{server['name']}] {t['description']}",
                                   "parameters": t["parameters"], "source": "mcp", "server_id": sid,
                                   "tool": t["name"], "read_only": t.get("read_only", False)}
        return ctx

    # ----------------------------------------------------------- 调用
    def policy(self, spec: dict) -> str:
        return policy_for(self.store.get_settings(), spec)

    async def call(
        self, ctx: ToolContext, name: str, args: dict[str, Any],
        approve: Callable[[dict, dict], Awaitable[bool]] | None = None,
    ) -> ToolOutcome:
        """approve:需要确认的调用交给它去问用户(返回 True=放行)。没有传时,需要确认的调用一律拒绝。"""
        t0 = time.time()
        spec = ctx.tools.get(name)
        if not spec:
            return ToolOutcome(f"没有名为 {name} 的工具(或本群未启用)。可用工具:{', '.join(ctx.tools) or '无'}", False)
        req = (spec["parameters"] or {}).get("required") or []
        missing = [r for r in req if r not in args]
        if missing:
            return ToolOutcome(f"缺少必填参数:{', '.join(missing)}", False)
        pol = self.policy(spec)
        if pol == "deny":
            return ToolOutcome(f"工具 {name} 已被用户在「权限与操控」里禁止,没有执行。不要重试,请换个办法或直接告诉用户。", False, 0, True)
        if pol == "ask" and not (approve and await approve(spec, args)):
            return ToolOutcome(
                f"用户没有批准这次调用({name}:拒绝或超时未确认),没有执行。不要重试同一操作,请换个办法,或直接告诉用户你需要做什么、为什么。",
                False, int((time.time() - t0) * 1000), True,
            )
        if pol == "ask" and self.policy(spec) == "deny":   # 等确认的这段时间里用户又把它设成了「禁止」
            return ToolOutcome(f"工具 {name} 已被用户在「权限与操控」里禁止,没有执行。", False, 0, True)
        t0 = time.time()  # 耗时不含等用户确认的时间
        timeout = float(self.store.get_settings()["tool_timeout"])
        try:
            text, ok = await asyncio.wait_for(self._dispatch(ctx, spec, args, timeout), timeout + 5)
        except asyncio.TimeoutError:
            text, ok = f"工具执行超时({int(timeout)} 秒)", False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            text, ok = f"工具执行出错:{type(e).__name__}: {e}"[:500], False
        return ToolOutcome(text, ok, int((time.time() - t0) * 1000))

    async def _dispatch(self, ctx: ToolContext, spec: dict, args: dict, timeout: float) -> tuple[str, bool]:
        src, name = spec["source"], spec["name"]
        if src == "mcp":
            return await self.mcp.call_tool(spec["server_id"], spec["tool"], args, timeout)
        if src == "plugin":
            res = await self.registry.call(name, args)
            return (res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)), True
        return await self._builtin(ctx, name, args)

    async def _builtin(self, ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
        group, agent = ctx.group, ctx.agent
        if name == "current_time":
            return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %A (%z)"), True
        if name == "library_search":
            k = max(1, min(int(args.get("top_k") or self.store.get_settings()["library_top_k"]), 10))
            # BM25 打分是纯 CPU 活,大资料库首次检索还要重建索引;放线程池,别卡住事件循环(会拖慢所有人的流式输出)
            hits = await asyncio.to_thread(
                self.library.search, str(args["query"]), k, self.library.scope_ids(group["ext"]["library"])
            )
            if not hits:
                return "资料库里没有找到相关内容。", True
            return "\n\n".join(f"[《{h['title']}》第 {h['idx'] + 1} 段]\n{h['text'][:900]}" for h in hits), True
        if name == "library_read":
            doc = self.library.find_by_title(str(args["doc"]))
            if not doc or not doc["enabled"]:
                return f"资料库里没有《{args['doc']}》。", False
            allowed = self.library.scope_ids(group["ext"]["library"])
            if allowed is not None and doc["id"] not in allowed:
                return "本群没有启用这份文档。", False
            r = await asyncio.to_thread(self.library.read, doc["id"], max(0, int(args.get("start") or 0)), 3000)
            tail = f"\n(已读到第 {r['end']} 字,共 {r['total']} 字;继续读请用 start={r['end']})" if r["end"] < r["total"] else ""
            return f"《{doc['title']}》\n{r['text']}{tail}", True
        if name == "memory_search":
            mems = self.memory.recall(group["id"], agent["id"], str(args["query"]), 8)
            return (self.memory.block(mems) or "没有相关记忆。"), True
        if name == "memory_save":
            content = str(args["content"]).strip()
            if not content or len(content) > 120:
                return "内容为空或超过 120 字。", False
            if looks_sensitive(content):
                return "内容像是密钥/密码/长数字串,出于安全没有保存。", False
            kind = args.get("kind") if args.get("kind") in ("preference", "fact", "decision", "lesson") else "fact"
            self.memory.save_manual(content, "group", group["id"], kind, "auto")
            return "已记入本群记忆。", True
        return f"未实现的内置工具 {name}", False
