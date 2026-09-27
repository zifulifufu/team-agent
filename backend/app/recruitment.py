"""Host-only recruitment of registered resources. Never installs or enables anything."""
from __future__ import annotations

import os
from pathlib import Path

from . import cooperation, external, localcmd, media, presets, teamrec, teamsetup, templates


def _member_problem(store, router, member: dict) -> str:
    if member.get("hidden"):
        return "Hidden system members cannot be recruited"
    engine = member.get("engine") or ""
    if engine:
        if not store.get_settings()["external_agents_enabled"]:
            return "External agents/tools are switched off"
        try:
            external.clean_cfg(member.get("engine_cfg"), engine=engine)
        except ValueError as exc:
            return str(exc)
        if localcmd.row(engine):
            exe = localcmd.exe_for(engine, (member.get("engine_cfg") or {}).get("cli_path", ""))
            if not (Path(exe).is_file() and os.access(exe, os.X_OK)):
                return "Local tool executable is missing; configure/install it first"
        return ""
    mid = member.get("model_id")
    if mid:
        problem = teamsetup.model_problem(store, store.get_model(mid))
        if problem:
            return problem
    if member.get("origin") == media.MEDIA_ORIGIN:
        return teamsetup.model_problem(store, store.get_model(mid))
    if not router.resolve(mid, member.get("tags") or []):
        return "No enabled, configured chat model is available for this role"
    return ""


def catalog(store, router, registry, group: dict, query: str, limit: int = 16) -> list[dict]:
    inside = set(store.member_ids(group["id"]))
    ext = group["ext"]
    agents = store.list_agents()
    rows = []
    problems = router.recent_problems()
    def add(kind, ident, name, summary, problem="", joined=False, tags=None):
        rows.append({"ref": f"{kind}:{ident}", "name": name, "summary": summary,
                     "status": "unavailable" if problem else "configured", "reason": problem,
                     "in_group": joined, "tags": tags or []})
    for member in agents:
        if member.get("hidden"):
            continue
        contract = cooperation.contract(store, member)
        add("agent", member["id"], member["name"], member.get("role", "") + " " + contract.get("summary", ""),
            _member_problem(store, router, member), member["id"] in inside, member.get("tags"))
    for key, preset in presets.AGENT_PRESET_BY_KEY.items():
        if not preset.get("system") and key != "process":
            existing = templates._find(store, preset["name"])
            add("preset", key, preset["name"], preset.get("role", ""),
                _member_problem(store, router, existing or preset),
                bool(existing and existing["id"] in inside), tags=preset.get("tags"))
    for model in [*store.list_models(), *store.list_media_models()]:
        problem = teamsetup.model_problem(store, model)
        if not problem:
            problem = problems.get(model["id"], "")
        add("model", model["id"], model["display_name"], model.get("use", "chat"), problem,
            any(a.get("model_id") == model["id"] and a["id"] in inside for a in agents), model.get("strengths"))
    for engine, tool in localcmd.tools().items():
        if not any(a.get("engine") == engine for a in agents):
            add("tool", engine, tool.get("name") or engine, tool.get("role", ""),
                _member_problem(store, router, {"engine": engine}))
    for server in store.list_mcp():
        problem = "MCP server is disabled" if not server["enabled"] else ""
        if server.get("url") and not store.get_settings()["external_calls_enabled"]:
            problem = "External calls are switched off"
        add("mcp", server["id"], server["name"], "MCP tools (connection checked on use)", problem,
            server["id"] in ext["mcp"])
    for ident, plugin in registry.plugins.items():
        add("plugin", ident, plugin.name, ", ".join(plugin.tools), plugin.error, ident in ext["plugins"])
    wanted = teamrec._features(query)
    tags = set(teamrec.task_tags(query))
    def score(row):
        return len(wanted & teamrec._features(row["name"] + " " + row["summary"])) + 2 * len(tags & set(row["tags"]))
    rows.sort(key=lambda r: (-score(r), r["status"] == "unavailable", r["in_group"], r["ref"]))
    return rows[:max(1, min(int(limit), 24))]


def invite(store, router, registry, group_id: str, actor_id: str, ref: str, reason: str) -> dict:
    group = store.get_group(group_id)
    if not group or group.get("host_agent_id") != actor_id:
        raise ValueError("Only this group's current host may recruit")
    if not store.get_settings().get("host_auto_recruit"):
        raise ValueError("Automatic recruitment is switched off")
    if not str(reason).strip():
        raise ValueError("State the capability gap and the new resource's job")
    kind, sep, ident = ref.partition(":")
    if not sep or not ident:
        raise ValueError("Choose a ref returned by find_team_resources")
    if kind in {"mcp", "plugin"}:
        if kind == "mcp":
            server = store.get_mcp(ident)
            if not server or not server["enabled"]:
                raise ValueError("MCP server is missing or disabled")
            if server.get("url") and not store.get_settings()["external_calls_enabled"]:
                raise ValueError("External calls are switched off")
            name = server["name"]
        else:
            plugin = registry.plugins.get(ident)
            if not plugin or plugin.error:
                raise ValueError("Plugin is missing or has a loading error")
            name = plugin.name
        key = "mcp" if kind == "mcp" else "plugins"
        updated = ident not in group["ext"][key]
        group["ext"][key] = list(dict.fromkeys([*group["ext"][key], ident]))
        store.update_group(group_id, {"ext": group["ext"]})
        return {"ref": ref, "name": name, "added": updated, "reason": reason[:500]}
    if kind == "agent":
        member = store.get_agent(ident)
    elif kind == "preset":
        member = presets.AGENT_PRESET_BY_KEY.get(ident)
        if ident == "process" or (member or {}).get("system"):
            raise ValueError("Hidden system roles cannot be recruited")
        if member:
            member = templates._find(store, member["name"]) or member
    elif kind == "tool":
        tool = localcmd.row(ident)
        if not tool:
            raise ValueError("Unknown registered local tool")
        member = next((a for a in store.list_agents() if a.get("engine") == ident),
                      {"engine": ident, "name": tool.get("name") or ident})
    elif kind == "model":
        model = store.get_model(ident)
        problem = teamsetup.model_problem(store, model)
        if problem:
            raise ValueError(problem)
        member = {"model_id": ident, "origin": "media" if model.get("use") in media.MEDIA_USES else "model",
                  "tags": model.get("strengths")}
        member = next((a for a in store.list_agents() if a.get("origin") == member["origin"]
                       and a.get("model_id") == ident), member)
    else:
        raise ValueError("Only registered members, models, presets, MCP servers and plugins can join")
    if not member:
        raise ValueError("Resource no longer exists")
    problem = _member_problem(store, router, member)
    if problem:
        raise ValueError(problem)
    if len(store.member_ids(group_id)) >= 64 and member.get("id") not in store.member_ids(group_id):
        raise ValueError("The group already has 64 members")
    if kind == "tool" and not member.get("id"):
        member = store.create_agent(member["name"], role=tool.get("role", ""), prompt=tool.get("prompt", ""),
                                    engine=ident, engine_cfg=external.clean_cfg({}, engine=ident))
    member = (store.ensure_model_agent(ident) if kind == "model" else
              templates.ensure_agent_from_key(store, ident) if kind == "preset" else member)
    updated = member["id"] not in store.member_ids(group_id)
    store.add_member(group_id, member["id"])
    return {"ref": ref, "name": member["name"], "agent_id": member["id"], "added": updated,
            "reason": reason[:500], "contract": cooperation.contract(store, member)}
