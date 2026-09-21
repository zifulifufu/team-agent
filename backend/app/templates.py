"""群聊模板与成员预设:一键建群、随时把预设成员拉进群。"""

from __future__ import annotations

from .presets import AGENT_PRESET_BY_KEY, AGENT_PRESETS, SEED_AGENTS, TEMPLATES
from .store import Store

_BY_NAME = {**{a["name"]: a for a in SEED_AGENTS}, **{a["name"]: a for a in AGENT_PRESETS}}


def ensure_agent(store: Store, name: str) -> dict | None:
    """按名字找成员;不存在且是已知预设,就创建。"""
    for a in store.list_agents():
        if a["name"] == name:
            return a
    p = _BY_NAME.get(name)
    if not p:
        return None
    return store.create_agent(p["name"], p.get("avatar", "🤖"), p.get("role", ""), p.get("prompt", ""),
                              None, p.get("skills"), p.get("tags"))


def ensure_agent_from_key(store: Store, key: str) -> dict | None:
    p = AGENT_PRESET_BY_KEY.get(key)
    return ensure_agent(store, p["name"]) if p else None


def create_group_from_template(store: Store, tid: str, name: str | None = None) -> dict | None:
    t = next((x for x in TEMPLATES if x["id"] == tid), None)
    if not t:
        return None
    agents = [a for a in (ensure_agent(store, n) for n in t["members"]) if a]
    host = next((a for a in agents if a["name"] == t["host"]), agents[0] if agents else None)
    return store.create_group(
        name or t["name"], host["id"] if host else None, [a["id"] for a in agents],
        ext={"skills": t["skills"]}, prompt=t.get("prompt", ""),
    )
