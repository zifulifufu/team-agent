"""Group chat templates and member presets: create a group in one click, pull a preset
member into a group at any time."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import i18n
from .presets import (
    AGENT_PRESET_BY_KEY,
    TEMPLATES,
    builtin_for,
    builtin_names,
    display_name,
    localize_member,
)
from .tools import display_skill_name

if TYPE_CHECKING:   # annotations only — importing Store for real would be a cycle (store → templates)
    from .store import Store


def _find(store: Store, name: str) -> dict | None:
    """A stored member answering to `name`, in either language.

    An agent created before the built-in names were translated is stored under its
    Chinese name while a template may ask for the English one (or the other way round),
    so compare against every spelling of the built-in name before giving up.
    """
    entry = builtin_for(name)
    wanted = set(builtin_names(entry)) if entry else {name}
    wanted.add(name)
    for a in store.list_agents():
        if a["name"] in wanted:
            return a
    return None


def ensure_agent(store: Store, name: str) -> dict | None:
    """Find a member by name; if it does not exist and is a known preset, create it."""
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
    """Members with built-in names/roles/prompts shown in the request language.

    Attached skill names are stored language-neutrally and resolved back to a display
    name here, so the skills a client reads are the same ones it can tick and send back.
    """
    lang = i18n.current()
    return [agent_view(a, lang) for a in agents]


def agent_view(agent: dict, lang: str | None = None) -> dict:
    """One member, shown in `lang`."""
    lang = lang or i18n.current()
    out = localize_member(agent, lang)
    if out.get("skills"):
        out = dict(out)
        out["skills"] = [display_skill_name(n, lang) for n in out["skills"]]
    return out


def skill_list_view(names: list[str] | None, lang: str | None = None) -> list[str]:
    """Stored skill names (either spelling) as shown in `lang`."""
    lang = lang or i18n.current()
    return [display_skill_name(n, lang) for n in (names or [])]


def group_view(group: dict | None, lang: str | None = None) -> dict | None:
    """A group with the built-in skill names it carries shown in `lang`."""
    if not group:
        return group
    out = dict(group)
    ext = dict(out.get("ext") or {})
    if ext.get("skills"):
        ext["skills"] = skill_list_view(ext["skills"], lang)
    out["ext"] = ext
    return out


def template_rows() -> list[dict]:
    """Group templates with every displayed name and text in the request language."""
    lang = i18n.current()
    out = []
    for t in TEMPLATES:
        row = i18n.localize(t, lang)
        row["members"] = [display_name(n, lang) for n in t.get("members", [])]
        row["host"] = display_name(t.get("host", ""), lang)
        row["skills"] = [display_skill_name(s, lang) for s in t.get("skills", [])]
        out.append(row)
    return out
