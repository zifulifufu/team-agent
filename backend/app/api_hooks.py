"""Hook endpoints: the list, the on/off switch, one hook's source, and one-off runs.

  * `GET   /api/hooks`               -- every hook folder, with its state and last result
  * `GET   /api/hooks/guide`         -- how to write one (shown on the page)
  * `GET   /api/hooks/log`           -- the tail of the hook log
  * `PATCH /api/hooks/{hid}`         -- enable/disable, and which groups it applies to
  * `POST  /api/hooks/reload`        -- rescan the directory (a new folder needs no restart)
  * `GET   /api/hooks/{hid}/source`  -- the hook's own code, for reading it before trusting it
  * `POST  /api/hooks/{hid}/test`    -- run it once with a sample payload

The last one matters most: "installed" and "works" are different claims, and the panel should
never ask the user to believe the first one.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import hooks as hooks_lib, i18n
from .store import Store


class HookIn(BaseModel):
    enabled: bool | None = None
    groups: list[str] | None = None


class TestIn(BaseModel):
    event: str | None = None
    group_id: str | None = None


# One sample per event, carrying the fields that event really carries.
#
# Not a generic fixture on purpose: "run once" is the page's evidence that a hook works, and a
# payload missing a key the round always sends would report a failure for a hook that is fine —
# a test that lies is worse than no test. Every key here is one the app itself puts in.
_SAMPLES: dict[str, dict[str, Any]] = {
    "round.start": {"sender": "me", "chars": 12, "read_only": False, "files": 0},
    "round.end": {"sender": "me", "entries": 2, "seconds": 3.5,
                  "agents": ["sample member"], "answer_chars": 120},
    "agent.reply": {"agent": "sample member", "model": "sample/model", "fallback_from": "",
                    "chars": 120, "tools": ["current_time"], "latency_ms": 800},
    "tool.called": {"agent": "sample member", "tool": "current_time", "source": "builtin",
                    "args": {}, "ok": True, "ms": 12, "text": "12:00"},
    "pre_prompt": {"agent": "sample member", "role": "Coordinator", "model": "sample/model",
                   "scene": "reply"},
    "pre_tool_use": {"tool": "current_time", "source": "builtin", "risk": "read", "args": {}},
    "post_reply": {"agent": "sample member", "model": "sample/model", "text": "sample reply"},
    "before_send": {"text": "sample message"},
}


def sample_payload(event: str, group: dict) -> dict[str, Any]:
    """What `POST /test` hands a hook: this event's own fields, plus which group it is about."""
    return {"group_id": group.get("id", ""), "group_name": group.get("name", ""),
            "at": time.time(), **_SAMPLES.get(event, {})}


def build_hooks_router(store: Store, manager: hooks_lib.HookManager) -> APIRouter:
    r = APIRouter()

    def view() -> list[dict]:
        return [h.to_dict() for h in sorted(manager.hooks.values(), key=lambda x: x.id)]

    @r.get("/api/hooks")
    async def list_hooks() -> dict:
        return {"hooks": view(), "errors": manager.errors, "guide": hooks_lib.guide(),
                "directory": str(manager.dir), "log_file": str(manager.log_path)}

    @r.get("/api/hooks/log")
    async def log_tail(limit: int = 50) -> dict:
        return {"entries": manager.recent(max(1, min(500, limit)))}

    @r.post("/api/hooks/reload")
    async def reload_hooks() -> dict:
        manager.load()
        return {"hooks": view(), "errors": manager.errors}

    @r.patch("/api/hooks/{hid}")
    async def patch_hook(hid: str, body: HookIn) -> dict:
        if hid not in manager.hooks:
            raise HTTPException(404, i18n.pick_now("That hook does not exist", "钩子不存在"))
        if body.groups is not None:
            known = {g["id"] for g in store.list_groups()}
            unknown = [g for g in body.groups if g not in known]
            if unknown:
                raise HTTPException(422, i18n.pick_now(
                    f"Unknown group: {', '.join(unknown)}", f"未知的群:{'、'.join(unknown)}"))
        info = manager.write_spec(hid, enabled=body.enabled, groups=body.groups)
        return {"hook": info.to_dict()}

    @r.get("/api/hooks/{hid}/source")
    async def hook_source(hid: str) -> dict:
        try:
            return {"id": hid, "content": manager.source(hid)}
        except KeyError:
            raise HTTPException(404, i18n.pick_now("That hook does not exist", "钩子不存在")) from None

    @r.post("/api/hooks/{hid}/test")
    async def hook_test(hid: str, body: TestIn | None = None) -> dict:
        info = manager.hooks.get(hid)
        if info is None:
            raise HTTPException(404, i18n.pick_now("That hook does not exist", "钩子不存在"))
        wanted = (body.event if body else "") or (info.events[0] if info.events else "")
        if wanted not in hooks_lib.EVENTS:
            raise HTTPException(422, i18n.pick_now(
                f"This hook is not registered for {wanted}", f"这个钩子没有登记 {wanted} 事件"))
        group = store.get_group((body.group_id if body else "") or "")
        if not group:
            existing = store.list_groups()
            group = existing[0] if existing else {}
        return await manager.test(hid, wanted, sample_payload(wanted, group))

    return r
