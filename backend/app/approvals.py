"""Tool call approval: when a member wants to use a tool that "executes" (a plugin, an
MCP tool with side effects), you are asked in the chat first.

Flow: the orchestrator hits a call that needs confirmation -> Approvals.ask() pushes
an approval event over WebSocket -> the UI shows an approval bar -> you click
"allow once / always allow / deny" -> POST /api/approvals/{id} -> ask() returns.
A timeout (120s by default), pressing stop, or nobody being online are all treated
as "denied": better to do less than to authorize on your behalf.
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
    """read is read-only · write only modifies this app's own data (memory) · exec runs
code or has external effects (plugins, non-read-only MCP tools).

A spec may carry its own `risk`; that always wins. The name-based rules below are the
fallback for built-ins that predate the field, and their shape ("read unless memory_save")
means a new built-in that runs something would silently be treated as read-only.
"""
    if spec.get("risk"):
        return str(spec["risk"])
    src = spec.get("source")
    if src == "builtin":
        return "write" if spec["name"] == "memory_save" else "read"
    if src == "mcp":
        return "read" if spec.get("read_only") else "exec"
    return "exec"


def policy_for(cfg: dict, spec: dict) -> str:
    """allow | ask | deny. An explicit deny always wins; then the mode and the
"always allow" list are consulted."""
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
            except Exception:  # noqa: BLE001 — a notification failure must not mask the real result
                pass

    def resolve(self, aid: str, allow: bool, remember: bool = False) -> bool:
        """Returns False when this approval no longer exists (timed out, already handled,
or cancelled)."""
        p = self._pending.get(aid)
        if not p or p.fut.done():
            return False
        if remember and allow:
            cfg = self.store.get_settings()
            if p.tool not in cfg["perm_allow"]:
                self.store.update_settings({"perm_allow": [*cfg["perm_allow"], p.tool]})
        p.fut.set_result(allow)
        return True
