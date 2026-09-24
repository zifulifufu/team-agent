"""Group chat templates and member presets: create a group in one click, pull a preset
member into a group at any time."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import i18n
from . import proclog
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

    ⚠️ Hidden members are searched too: the process engineer is hidden, and a lookup that could not
    see it would try to create a second member with the same name — which the `UNIQUE` constraint on
    `agents.name` refuses, at startup.
    """
    entry = builtin_for(name)
    wanted = set(builtin_names(entry)) if entry else {name}
    wanted.add(name)
    for a in store.list_agents(include_hidden=True):
        if a["name"] in wanted:
            return a
    return None


def ensure_agent(store: Store, name: str, *, hidden: bool = False) -> dict | None:
    """Find a member by name; if it does not exist and is a known preset, create it.

    `hidden` only applies to the member that is being *created*; an existing row keeps whatever
    visibility it has, so a user who deliberately added a visible process engineer from the picker
    does not have it taken away by the next startup.
    """
    found = _find(store, name)
    if found:
        return found
    entry = builtin_for(name)
    if not entry:
        return None
    return store.create_agent(
        entry["name"], entry.get("avatar", "🤖"),
        entry.get("role", ""), entry.get("prompt", ""),
        None, entry.get("skills"), entry.get("tags"), hidden=hidden,
    )


def ensure_agent_from_key(store: Store, key: str) -> dict | None:
    p = AGENT_PRESET_BY_KEY.get(key)
    return ensure_agent(store, p["name"]) if p else None


# The one preset this module keeps in groups by itself, so the key lives here rather than being
# spelled out at each call site.
PROCESS_KEY = "process"


def process_engineer(store: Store) -> dict | None:
    """The built-in process engineer: created hidden if this install has never had one.

    Hidden is the point — it watches every group without appearing in any of them, so nobody has to
    read past it in the member list, address it by name, or see it answer. What it produces is the
    ledger, and the entries it writes carry its name.
    """
    entry = AGENT_PRESET_BY_KEY.get(PROCESS_KEY)
    return ensure_agent(store, entry["name"], hidden=True) if entry else None


def process_status(store: Store) -> dict:
    """The process engineer as one panel: what it is, where it is, what it has written down.

    It is invisible everywhere else on purpose — no member row, no `@`-candidate, not one message in
    any group — so this is the only place that answers "is it really watching my groups, and what has
    it found" without opening each workspace by hand.
    """
    agent = process_engineer(store)                     # created on an install that has never had one
    cfg = store.get_settings()
    groups = store.list_groups()
    watching = ([g for g in groups
                 if agent and any(m["id"] == agent["id"]
                                  for m in store.group_members(g["id"], include_hidden=True))]
                if agent else [])
    counts = {state: 0 for state in proclog.STATES}
    recent: list[dict] = []
    ledgers = 0
    for g in groups:
        path = proclog.unit(store.workspace_dir(g["id"]))
        if not path.is_file():
            continue
        ledgers += 1
        try:
            entries = proclog.read(path)
        except OSError:
            continue
        for e in entries:
            counts[e.status] = counts.get(e.status, 0) + 1
            recent.append({"group": g["name"], "gid": g["id"], **e.brief()})
    recent.sort(key=lambda r: (r.get("status") != "open", r.get("found") or ""), reverse=True)
    return {
        "name": (agent or {}).get("name", ""),
        "hidden": bool((agent or {}).get("hidden")),
        "autojoin": bool(cfg["process_autojoin"]),
        "autolog": bool(cfg["process_autolog"]),
        "review": bool(cfg["process_review"]),
        "groups": len(groups),
        "in_groups": len(watching),
        "not_in": [g["name"] for g in groups if g not in watching][:12],
        "entries": counts,
        "ledgers": ledgers,
        "recent": recent[:12],
    }


def keep_process_engineer(store: Store, group_id: str | None = None) -> list[str]:
    """Put the process engineer into this group — or every group — and say which ones gained it.

    Three decisions worth stating, because each of them is a way this could go wrong:

      * **the setting decides**, and it is off by an explicit `false`. Somebody who does not want a
        watcher in every group gets that, and the member they removed by hand does not come back;
      * **it is added last**, so on a group whose host was never set the chair does not land on the
        auditor — deciding how the work is split is not its job (the same reason `_pick_host` skips it);
      * **a group that already has it is left alone**, matched by the agent row rather than by name,
        so a member the user renamed is still recognised as the same one.
    """
    if not store.get_settings().get("process_autojoin"):
        return []
    agent = process_engineer(store)
    if agent is None:
        return []
    if not agent.get("hidden"):
        # ⚠️ Enforced, not merely set at creation. A row from a version that kept the watcher as an
        # ordinary member (or from a group where it was added by hand) would otherwise stay visible —
        # and "it is in every group but appears in none of them" would be false on exactly the
        # installs that have been using the app longest. There is no switch that un-hides it: the
        # promise of this feature is one thing, so the auto path is where it is kept.
        store.set_agent_hidden(agent["id"], True)
        agent = store.get_agent(agent["id"]) or agent
    groups = [store.get_group(group_id)] if group_id else store.list_groups()
    added: list[str] = []
    for g in groups:
        # `include_hidden=True` on the member check: the question is "is it already in this group",
        # not "is it visible" — and asking the visible list would add it all over again on every
        # startup, one duplicate row per round of app launches.
        if not g or any(m["id"] == agent["id"] for m in store.group_members(g["id"], include_hidden=True)):
            continue
        store.add_member(g["id"], agent["id"])
        added.append(g["id"])
    return added


def create_group_from_template(store: Store, tid: str, name: str | None = None) -> dict | None:
    t = next((x for x in TEMPLATES if x["id"] == tid), None)
    if not t:
        return None
    agents = [a for a in (ensure_agent(store, n) for n in t["members"]) if a]
    host = next((a for a in agents if a["name"] in builtin_names(builtin_for(t["host"]) or {"name": t["host"]})),
                agents[0] if agents else None)
    lang = i18n.current()
    group = store.create_group(
        name or (t.get("name_zh") if lang == "zh" else None) or t["name"],
        host["id"] if host else None, [a["id"] for a in agents],
        ext={"skills": t["skills"]},
        prompt=(t.get("prompt_zh") if lang == "zh" else t.get("prompt")) or t.get("prompt", ""),
    )
    # After the template's own members and host, never before: a group that ends up with the auditor
    # in the chair would have the watcher deciding how the work is split.
    keep_process_engineer(store, group["id"])
    return group


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
