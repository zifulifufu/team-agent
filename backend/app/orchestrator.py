"""群聊编排:决定谁发言、拼上下文、调用路由层、驱动工具调用、解析 @ 交接、群主分工。

一条用户消息的处理流程:
  A. 用户 @ 了成员 / @所有人 → 点名的人依次发言;回复里再 @ 别人就接力(最多 max_hops 轮)。
  B. 没 @ 任何人 → 交给群主。若开启了分工(默认「auto」),群主先看成员强项分工表决定要不要分工:
       - 需要:输出 <plan> → 校验 → 按依赖顺序让各成员各做一项(带统一约定和上游成果)→ 群主整合。
       - 不需要:群主直接回答,回复里的 @ 照常接力。
  每次发言里,成员可以按文本协议调用工具(资料库 / 记忆 / 插件 / MCP),最多 tool_rounds 轮。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import external, planner
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

# 流式分片合并:模型通常每 1~3 个字吐一片,原样转发会让一次 1000 字的回答产生几百个 WebSocket 帧、
# 前端就要重渲染几百次。攒够 DELTA_BATCH_CHARS 个字、或距上次下发超过 DELTA_BATCH_SECONDS 才发一次,
# 拼接结果和原来完全一致,但帧数与重渲染次数下降一个量级。
DELTA_BATCH_CHARS = 24
DELTA_BATCH_SECONDS = 0.06

# @ 前面是字母数字(邮箱 me@x.com)不算点名;@all 后面接字母(@Allen)也不算
_ALL_RE = re.compile(r"(?<![A-Za-z0-9_.])@(?:所有人|all(?![A-Za-z0-9_]))", re.IGNORECASE)


def _mention_re(name: str) -> "re.Pattern[str]":
    tail = r"(?![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", name[-1:]) else ""
    return re.compile(r"(?<![A-Za-z0-9_.])@" + re.escape(name) + tail)


def _member_names(member: dict) -> list[str]:
    """Every spelling this member answers to: the stored name plus its built-in twin.

    A built-in member may be stored as 小助 while the rest of the conversation uses
    Aide (or the reverse), so @mention has to accept both.
    """
    stored = member.get("name") or ""
    twin = twin_name(stored)
    return [n for n in (stored, twin) if n] or [stored]


def find_mentions(text: str, members: list[dict], exclude_id: str | None = None) -> list[dict]:
    """按出现顺序返回被 @ 的成员(名字越长越优先匹配,避免 @文案 误中 @文案组)。

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


PLAN_FALLBACK = "已做好分工,见任务板。"


@dataclass
class RunState:
    """一次用户消息从开始到结束的运行记录(记忆和统计用)。"""
    gid: str
    user_text: str
    started: float = field(default_factory=time.time)
    steps: list[dict] = field(default_factory=list)
    final_text: str = ""
    refs_block: str = ""
    warned: set[str] = field(default_factory=set)


@dataclass
class TurnOut:
    text: str          # 存进聊天记录的可见内容
    raw: str           # 各轮模型原始输出(含 <plan> / <tool_call>)
    message: dict


class Orchestrator:
    def __init__(
        self, store: Store, router: ModelRouter, *, prompts: PromptBuilder | None = None,
        toolhub: ToolHub | None = None, memory: MemoryService | None = None, library: Library | None = None,
        registry: ToolRegistry | None = None, mcp: McpManager | None = None,
        approvals: Approvals | None = None, external_runner: ExternalRunner | None = None,
    ):
        self.store = store
        self.external = external_runner or ExternalRunner(store.data_dir)
        self.router = router
        self.library = library or Library(store)
        self.memory = memory or MemoryService(store, router)
        self.registry = registry or ToolRegistry()
        self.mcp = mcp or McpManager()
        self.toolhub = toolhub or ToolHub(store, self.registry, self.mcp, self.library, self.memory)
        self.prompts = prompts or PromptBuilder(store, router)
        self.approvals = approvals or Approvals(store)
        self._locks: dict[str, asyncio.Lock] = {}
        self._bg: set[asyncio.Task] = set()

    def _lock(self, gid: str) -> asyncio.Lock:
        return self._locks.setdefault(gid, asyncio.Lock())

    def _spawn(self, coro: Awaitable[Any]) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """等待后台任务(记忆提炼)结束。测试和退出时用。"""
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ---------------------------------------------------------------- context
    def build_messages(
        self, group: dict, agent: dict, members: list[dict], *, memory_block: str = "", tools_block: str = "",
        extra_system: str = "", extra_user: str | None = None, exclude_plan_id: str | None = None,
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
                continue  # 本次分工里别人的成果会在任务提示里完整给出,不在历史里重复
            text = h["content"] if i in (len(history) - 1, last_user) else clip_middle(h["content"], clip)   # 最新一条和用户最新的请求原样带入
            if h["sender_type"] == "agent" and h["sender_id"] == agent["id"]:
                role, content = "assistant", text
            else:
                role, content = "user", f"[{h['sender_name']}] {text}"
            if convo and convo[-1]["role"] == role:  # 合并相邻同角色,兼容严格交替的 API
                convo[-1]["content"] += "\n\n" + content
            else:
                convo.append({"role": role, "content": content})
        while convo and convo[0]["role"] == "assistant":
            convo.pop(0)
        if not convo or convo[-1]["role"] != "user":
            convo.append({"role": "user", "content": "(请继续)"})
        convo[-1]["content"] += f"\n\n(现在轮到你「{agent['name']}」发言)"
        if extra_user:
            convo[-1]["content"] += "\n\n" + extra_user
        return [{"role": "system", "content": sysmsg}] + convo

    def _refs_block(self, group: dict, text: str) -> str:
        """用户消息里的 #文档标题:把这些文档的开头直接放进上下文。"""
        if group["ext"]["library"]["mode"] == "off":
            return ""
        allowed = self.library.scope_ids(group["ext"]["library"])
        out = []
        for m in re.finditer(r"#([^\s#@,,。;;::!!??]{2,40})", text):
            doc = self.library.find_by_title(m.group(1))
            if doc and doc["enabled"] and (allowed is None or doc["id"] in allowed) and all(doc["title"] not in o for o in out):
                r = self.library.read(doc["id"], 0, 2500)
                more = "(节选,完整内容可用 library_read 继续读)" if r["end"] < r["total"] else ""
                out.append(f"《{doc['title']}》{more}\n{r['text']}")
            if len(out) >= 3:
                break
        return ("【用户引用的资料】\n" + "\n\n".join(out)) if out else ""

    # ------------------------------------------------------------- entrypoint
    async def handle_user_message(self, gid: str, text: str, emit: Emit, sender_name: str = "我") -> None:
        group = self.store.get_group(gid)
        if not group:
            return
        user_msg = self.store.add_message(gid, "user", "user", sender_name, text)
        await emit({"type": "message", "message": user_msg})
        async with self._lock(gid):
            group = self.store.get_group(gid) or group
            run = RunState(gid, text)
            run.refs_block = self._refs_block(group, text)
            await self._run_turns(group, text, emit, run)
            self._after_run(group, run)

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
            await self._system(group["id"], "群里还没有成员,请先添加 agent。", emit)
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
            if out is not True:  # True = 已经按计划执行完;否则 out 是群主的普通回复,走接力
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
                continue   # 这个外部智能体被设为「不接力」:它回复里的 @ 只是文字
            for m in find_mentions(out.text, members, exclude_id=agent["id"]):
                if m["id"] not in queued:
                    queue.append(m)
                    queued.add(m["id"])
        if queue:
            names = "、".join(a["name"] for a in queue)
            await self._system(
                group["id"], f"已达到单次最大发言轮数({max_hops}),{names} 的发言被暂停。可再发一条消息继续。", emit
            )

    @staticmethod
    def _pick_host(group: dict, members: list[dict]) -> dict:
        """群主必须是模型成员:外部智能体不会按 <plan> 协议分工,也不该由它决定其他人做什么。"""
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
            empty_fallback=PLAN_FALLBACK,
        )
        if out is None:
            return None
        try:
            obj = planner.extract_plan_json(out.raw)
        except planner.PlanError as e:
            await self._plan_failed(group["id"], out, f"群主的分工计划格式不对({e}),已改用普通接力模式。", emit)
            return out
        if obj is None:
            if mode == "on":
                await self._system(group["id"], "本群设为「总是先分工」,但群主没有给出计划,已按普通回复处理。", emit)
            return out
        # 群主这一轮已经把 MCP 连上了;计划里写了不存在的工具名只是被忽略,不当作错误
        known = {t["name"] for t in (await self.toolhub.context(group, host, connect=False)).specs()}
        try:
            plan = planner.build_plan(obj, members, int(cfg["plan_max_tasks"]), known)
        except planner.PlanError as e:
            await self._plan_failed(group["id"], out, f"群主的分工计划不合规({e}),已改用普通接力模式。", emit)
            return out
        await self._execute_plan(group, members, host, plan, run, emit)
        return True

    async def _execute_plan(
        self, group: dict, members: list[dict], host: dict, plan: planner.Plan, run: RunState, emit: Emit
    ) -> None:
        gid = group["id"]
        pm = self.store.add_message(gid, "plan", None, "任务板", planner.summarize(plan), meta=plan.to_meta())
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
                    task.status, task.error = "failed", "成员已不在群里"
                    await push()
                    continue
                task.status = "running"
                await push()
                out = await self._agent_turn(
                    group, agent, members, emit, run,
                    extra_user=planner.task_prompt(plan, task, outputs, i),
                    exclude_plan_id=plan.message_id,
                    extra_meta={"plan_id": plan.message_id, "task_id": task.id, "task_title": task.title},
                )
                if out is None:
                    task.status, task.error = "failed", "模型调用失败"
                else:
                    task.status, task.message_id = "done", out.message["id"]
                    outputs[task.id] = out.text
                await push()
            plan.status = "integrating"
            await push()
            final = await self._agent_turn(
                group, host, members, emit, run,
                extra_user=planner.integration_prompt(plan, outputs),
                exclude_plan_id=plan.message_id,
                extra_meta={"plan_id": plan.message_id, "task_id": "final", "task_title": "整合"},
            )
            plan.status = "done" if final is not None else "failed"
            if final is not None:
                run.final_text = final.text
            await push()
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
        except Exception as e:  # noqa: BLE001  —— 分工执行中途出错:任务板标成失败,别一直显示「进行中」
            plan.status = "failed"
            for t in plan.tasks:
                if t.status in ("running", "pending"):
                    t.status, t.error = "failed", t.error or "分工执行出错"
            try:
                await push()
            except Exception:  # noqa: BLE001
                pass
            await self._system(gid, f"分工执行出错:{e}", emit)

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
            """把攒着的分片合并成一次 delta 发出去。"""
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
            pending.clear()   # reset 马上会清屏,攒着的内容没必要再发一遍
            pending_len = 0
            filt = TagFilter()
            await emit({"type": "reset", "message_id": mid})
            earlier = "\n\n".join(visible_parts)
            if earlier:  # 前几轮已经给用户看过的内容要补回去
                await emit({"type": "delta", "message_id": mid, "text": earlier + "\n\n"})
            last_emit = time.monotonic()

        try:
            ctx = await self.toolhub.context(group, agent)
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
            messages = self.build_messages(
                group, agent, members, memory_block=memory_block,
                tools_block=tools_prompt(ctx.specs()) if ctx.tools else "", extra_system=run.refs_block,
                extra_user=extra_user, exclude_plan_id=exclude_plan_id,
            )

            for rnd in range(rounds + 1):
                filt = TagFilter()
                if rnd and visible_parts:
                    await flush_delta()   # 先落盘再换段,避免上一轮攒着的字跑到分隔符后面
                    await emit({"type": "delta", "message_id": mid, "text": "\n\n"})
                res = await self.router.complete(
                    messages, preferred=agent["model_id"], tags=agent.get("tags"),
                    on_delta=on_delta, on_reset=on_reset,
                )
                await flush_delta()   # 收尾:把最后不足一批的分片发出去,再处理标签尾巴
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
                    entry = {"name": call.name or "(格式错误)", "args": _short_args(call.arguments), "status": "running"}
                    trace.append(entry)
                    idx = len(trace) - 1
                    await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})

                    async def approve(spec: dict, args: dict, entry: dict = entry, idx: int = idx) -> bool:
                        entry["status"] = "waiting"   # 气泡里显示「等你确认」
                        await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})
                        allowed = await self.approvals.ask(group=group, message_id=mid, agent=agent, spec=spec, args=args, emit=emit)
                        entry["status"] = "running"
                        if allowed:
                            await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})
                        return allowed

                    denied = False
                    if call.error:
                        text, ok, ms = call.error, False, 0
                    else:
                        oc = await self.toolhub.call(ctx, call.name, call.arguments, approve)
                        text, ok, ms, denied = oc.text, oc.ok, oc.ms, oc.denied
                    entry.update(status="denied" if denied else "ok" if ok else "failed", ms=ms, preview=text[:300])
                    await emit({"type": "tool", "message_id": mid, "index": len(trace) - 1, "call": dict(entry)})
                    results.append(format_result(call.name or "error", ok, text, int(cfg["tool_output_limit"])))
                messages.append({"role": "assistant", "content": res.text})
                messages.append({"role": "user", "content": "\n\n".join(results) + "\n\n请基于工具结果继续。"})
        except AllRoutesFailed as e:
            detail = "; ".join(f"{a.model_id}:{a.detail}" for a in e.attempts) or "没有可用模型"
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(
                group["id"], f"「{agent['name']}」暂时无法回复,所有模型均不可用({detail})。", emit
            )
            run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
            return None
        except asyncio.CancelledError:
            await emit({"type": "message_discard", "message_id": mid})
            raise
        except Exception as e:  # noqa: BLE001
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(group["id"], f"「{agent['name']}」发言出错:{e}", emit)
            run.steps.append({"agent": agent["name"], "ok": False, "tools": [t["name"] for t in trace]})
            return None

        assert res is not None
        content = "\n\n".join(visible_parts).strip()
        if not content:
            content = empty_fallback or ("(已调用工具,没有额外说明)" if trace else res.text.strip())
        meta = {"attempts": attempts, **(extra_meta or {})}
        if trace:
            meta["tools"] = trace
        try:
            saved = self.store.add_message(
                group["id"], "agent", agent["id"], agent["name"], content,
                model_id=res.model_id, fallback_from=res.fallback_from, meta=meta, mid=mid,
            )
        except Exception as e:  # noqa: BLE001  —— 存库失败也要让界面收尾,别留一个永远在转的气泡
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(group["id"], f"「{agent['name']}」的回复没能保存:{e}", emit)
            return None
        await emit({"type": "message_end", "message": saved})
        run.steps.append({
            "agent": agent["name"], "model": res.model_id, "ok": True,
            "tools": [t["name"] for t in trace if t.get("status") == "ok"], "fallback": bool(res.fallback_from),
        })
        return TurnOut(content, "\n".join(raws), saved)

    # ------------------------------------------------------- external agent turn
    async def _external_turn(
        self, group: dict, agent: dict, members: list[dict], emit: Emit, run: RunState, *, mid: str,
        extra_user: str | None, extra_meta: dict | None, exclude_plan_id: str | None, empty_fallback: str,
    ) -> TurnOut | None:
        """外部智能体(WorkBuddy)发言:群聊记录喂给它的命令行引擎,流式取回回复。
        它有自己的工具,所以这里不走模型路由、不解析 <tool_call>/<plan>;回复只当聊天文字。"""
        cfg = self.store.get_settings()
        name = agent["name"]
        gid = group["id"]

        async def fail(msg: str) -> None:
            await emit({"type": "message_discard", "message_id": mid})
            await self._system(gid, msg, emit)
            run.steps.append({"agent": name, "ok": False, "tools": []})
            return None

        if not cfg["external_agents_enabled"]:
            return await fail(f"「{name}」是外部智能体,而外部智能体总开关是关着的,已跳过。到「设置 → 外部智能体」里打开后再试。")
        if not cfg["external_calls_enabled"]:
            return await fail(f"「{name}」要连接云端模型,而「禁止外呼」正开着,已跳过。")
        try:
            ecfg = external.clean_cfg(agent.get("engine_cfg"))    # 运行前再校验一遍(备份恢复、手改数据库都可能带进不合规的设置)
        except ValueError as e:
            return await fail(f"「{name}」的外部智能体设置不合规:{e}")

        memory_block = ""
        if cfg["memory_enabled"] and group["ext"]["memory"]:
            memory_block = self.memory.block(
                self.memory.recall(gid, agent["id"], run.user_text + " " + (extra_user or "")[:300])
            )
        messages = self.build_messages(
            group, agent, members, memory_block=memory_block, extra_system=run.refs_block,
            extra_user=extra_user, exclude_plan_id=exclude_plan_id,
        )
        system = messages[0]["content"] + "\n\n" + external.ADDENDUM.format(
            name=name, group=group["name"], level=external.LEVELS[ecfg["level"]]["label"],
            cwd=str(self.external.workspace(agent)),
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
            await flush_ext_delta()   # 工具胶囊按出现顺序展示,先把已攒的文字发出去,别让胶囊插到文字前面
            while len(trace) <= idx:
                trace.append({})
            trace[idx] = entry
            await emit({"type": "tool", "message_id": mid, "index": idx, "call": dict(entry)})

        try:
            res = await self.external.run(agent, system=system, prompt=prompt, on_delta=on_delta, on_tool=on_tool)
        except ExternalError as e:
            return await fail(f"「{name}」没能回复:{e}")
        except asyncio.CancelledError:
            await emit({"type": "message_discard", "message_id": mid})
            raise
        except Exception as e:  # noqa: BLE001
            return await fail(f"「{name}」发言出错:{e}")

        await flush_ext_delta()   # 收尾:把最后不足一批的分片发出去
        content = res.text.strip() or empty_fallback or "(没有回复内容)"
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
            return await fail(f"「{name}」的回复没能保存:{e}")
        await emit({"type": "message_end", "message": saved})
        run.steps.append({"agent": name, "model": f"ext:{agent['engine']}", "ok": True,
                          "tools": [t["name"] for t in trace if t.get("status") == "ok"], "fallback": False})
        return TurnOut(content, res.text, saved)

    async def _plan_failed(self, gid: str, out: "TurnOut", note: str, emit: Emit) -> None:
        """计划没能执行时,把群主那条只有「已做好分工,见任务板」的消息改掉,免得和下面的系统提示互相矛盾。"""
        if out.text == PLAN_FALLBACK:
            try:
                fixed = self.store.update_message(out.message["id"], content="(分工计划没能生效,见下方系统提示)")
                if fixed:
                    await emit({"type": "message_end", "message": fixed})
            except Exception:  # noqa: BLE001 — 改文案失败不影响后面的提示
                pass
        await self._system(gid, note, emit)

    async def _system(self, gid: str, text: str, emit: Emit) -> None:
        msg = self.store.add_message(gid, "system", None, "系统", text)
        await emit({"type": "message", "message": msg})


def clip_middle(text: str, limit: int) -> str:
    """过长的历史消息:保留开头和结尾,省掉中间(开头交代背景,结尾常是结论)。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n…(中间省略 {len(text) - limit} 字)…\n{text[-tail:]}"


def _short_args(args: dict) -> dict:
    out = {}
    for k, v in list(args.items())[:6]:
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s if not isinstance(s, str) or len(s) <= 120 else s[:120] + "…"
    return out
