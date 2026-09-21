"""群聊模板与成员预设:一键建群、随时把预设成员拉进群。"""

from __future__ import annotations

from . import i18n
from .presets import (
    AGENT_PRESET_BY_KEY,
    TEMPLATES,
    builtin_for,
    builtin_names,
    localize_agent,
)


def _find(store: Store, name: str) -> dict | None:
    """A stored member answering to `name`, in either language.

    An agent created before the built-in names were translated is stored as 小助 while
    a template may ask for Aide (or the other way round), so compare against every
    spelling of the built-in name before giving up.
    """
    entry = builtin_for(name)
    wanted = set(builtin_names(entry)) if entry else {name}
    wanted.add(name)
    for a in store.list_agents():
        if a["name"] in wanted:
            return a
    return None


def ensure_agent(store: Store, name: str) -> dict | None:
    """按名字找成员;不存在且是已知预设,就创建。"""
    found = _find(store, name)
    if found:
        return found
    entry = builtin_for(name)
    if not entry:
        return None
    return store.create_agent(
        entry["name"], entry.get("avatar", "🤖"),
        entry.get("role", ""), entry.get("prompt", ""),
        None, entry.get("skills"), entry.get("tags"),
    )


def ensure_agent_from_key(store: Store, key: str) -> dict | None:
    p = AGENT_PRESET_BY_KEY.get(key)
    return ensure_agent(store, p["name"]) if p else None


def create_group_from_template(store: Store, tid: str, name: str | None = None) -> dict | None:
    t = next((x for x in TEMPLATES if x["id"] == tid), None)
    if not t:
        return None
    agents = [a for a in (ensure_agent(store, n) for n in t["members"]) if a]
    host = next((a for a in agents if a["name"] in builtin_names(builtin_for(t["host"]) or {"name": t["host"]})),
                agents[0] if agents else None)
    lang = i18n.current()
    return store.create_group(
        name or (t.get("name_zh") if lang == "zh" else None) or t["name"],
        host["id"] if host else None, [a["id"] for a in agents],
        ext={"skills": t["skills"]},
        prompt=(t.get("prompt_zh") if lang == "zh" else t.get("prompt")) or t.get("prompt", ""),
    )


def member_view(agents: list[dict]) -> list[dict]:
    """Members with built-in names/roles/prompts shown in the request language."""
    lang = i18n.current()
    return [localize_agent(a, lang) for a in agents]
