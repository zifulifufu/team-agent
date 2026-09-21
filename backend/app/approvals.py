"""工具调用审批:成员想用一个会「执行」的工具(插件、有副作用的 MCP)时,先在聊天里问你。

流程:orchestrator 遇到需要确认的调用 → Approvals.ask() 通过 WebSocket 推一条 approval 事件 →
界面弹出审批条 → 你点「允许一次 / 总是允许 / 拒绝」→ POST /api/approvals/{id} → ask() 返回。
超时(默认 120 秒)、你点了停止、或者没人在线,都按「拒绝」处理:宁可少做,不能替你放行。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import i18n
from .store import Store

Emit = Callable[[dict], Awaitable[None]]

# id -> (English label, Chinese label). Pairs rather than a `pick_now` call: a
# module-level call would be evaluated once at import and freeze the language.
RISK_LABELS = {
    "read": ("Read-only", "只读"),
    "write": ("Writes local data", "写入本地数据"),
    "exec": ("Runs things / has side effects", "执行/有副作用"),
}


def risk_label(risk: str) -> str:
    """The risk level of a tool call, in the request language."""
    pair = RISK_LABELS.get(risk)
    return i18n.pick_now(*pair) if pair else risk


def risk_of(spec: dict) -> str:
    """read 只读 · write 只改本应用自己的数据(记忆) · exec 会执行代码或对外部产生影响(插件、非只读的 MCP 工具)。"""
    src = spec.get("source")
    if src == "builtin":
        return "write" if spec["name"] == "memory_save" else "read"
    if src == "mcp":
        return "read" if spec.get("read_only") else "exec"
    return "exec"


def policy_for(cfg: dict, spec: dict) -> str:
    """allow | ask | deny。明确禁止的永远优先;然后看模式和「总是允许」名单。"""
    name = spec["name"]
    if name in cfg["perm_deny"]:
        return "deny"
    if cfg["perm_mode"] == "allow_all" or name in cfg["perm_allow"] or (spec.get("source") == "builtin" and name == "current_time"):
        return "allow"
    if cfg["perm_mode"] == "ask_all":
        return "ask"
    return "ask" if risk_of(spec) == "exec" else "allow"


def _short(v: Any, n: int = 200) -> Any:
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


@dataclass
class Pending:
    id: str
    group_id: str
    message_id: str
    agent: str
    tool: str
    source: str
    server: str
    risk: str
    args: dict
    created: float
    expires: float
    fut: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]

    def public(self) -> dict:
        return {"id": self.id, "group_id": self.group_id, "message_id": self.message_id, "agent": self.agent,
                "tool": self.tool, "source": self.source, "server": self.server, "risk": self.risk,
                "risk_label": risk_label(self.risk), "args": self.args, "expires_at": self.expires}


class Approvals:
    def __init__(self, store: Store):
        self.store = store
        self._pending: dict[str, Pending] = {}

    def list(self, group_id: str | None = None) -> list[dict]:
        return [p.public() for p in self._pending.values() if group_id in (None, p.group_id)]

    async def ask(self, *, group: dict, message_id: str, agent: dict, spec: dict, args: dict, emit: Emit) -> bool:
        cfg = self.store.get_settings()
        timeout = float(cfg["perm_timeout"])
        server = ""
        if spec.get("server_id"):
            s = self.store.get_mcp(spec["server_id"])
            server = s["name"] if s else ""
        now = time.time()
        p = Pending(
            id=self.store.new_id(), group_id=group["id"], message_id=message_id, agent=agent["name"],
            tool=spec["name"], source=spec["source"], server=server, risk=risk_of(spec),
            args={k: _short(v) for k, v in list(args.items())[:8]}, created=now, expires=now + timeout,
            fut=asyncio.get_running_loop().create_future(),
        )
        self._pending[p.id] = p
        outcome = "timeout"
        try:
            await emit({"type": "approval", "approval": p.public()})
            try:
                outcome = "allow" if await asyncio.wait_for(p.fut, timeout) else "deny"
            except asyncio.TimeoutError:
                outcome = "timeout"
            return outcome == "allow"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self._pending.pop(p.id, None)
            try:
                await emit({"type": "approval_done", "id": p.id, "group_id": p.group_id, "decision": outcome})
            except Exception:  # noqa: BLE001 — 通知失败不能掩盖真正的结果
                pass

    def resolve(self, aid: str, allow: bool, remember: bool = False) -> bool:
        """返回 False = 这条审批已经不存在(超时、已处理或已取消)。"""
        p = self._pending.get(aid)
        if not p or p.fut.done():
            return False
        if remember and allow:
            cfg = self.store.get_settings()
            if p.tool not in cfg["perm_allow"]:
                self.store.update_settings({"perm_allow": [*cfg["perm_allow"], p.tool]})
        p.fut.set_result(allow)
        return True
