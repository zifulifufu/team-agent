"""Endpoints for external agent members: the engines on offer (a command-line engine such as
WorkBuddy, or an OpenAI-compatible chat gateway such as Cherry Studio or MetaChat), detection,
creation, settings updates, and the connectivity test."""

from __future__ import annotations

from . import i18n

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import external, presets
from .store import Store

NAME_BAD = re.compile(r"[\s@]")
MASK = "***"          # what the UI is shown instead of a stored API key


def provider_view(store: Store, engine: str) -> dict | None:
    """The provider an engine is bound to, shaped for the interface.

    Returns None for an engine that carries its own address and key (Cherry Studio), in which case
    the dialog keeps asking for them. For a bound engine (MetaChat) this is where the address, the
    model list and "is a key set" come from — the provider row is the only copy, so the dialog no
    longer offers a second one. The key itself never leaves the backend.
    """
    pid = external.bound_provider(engine)
    if not pid:
        return None
    prov = store.get_provider(pid)
    if not prov:
        return {"id": pid, "missing": True, "name": pid, "base_url": "", "has_key": False, "models": []}
    shown = presets.localize_provider(prov, i18n.current())
    return {
        "id": pid,
        "missing": False,
        "name": shown.get("name") or pid,
        "base_url": prov.get("base_url") or "",
        "has_key": bool(prov.get("api_key")),
        "models": [{"name": m["model_name"], "display_name": m["display_name"]}
                   for m in store.list_models() if m["provider_id"] == pid and m["enabled"]],
    }


def provider_model_names(store: Store, engine: str) -> list[str]:
    """The model names a bound engine is allowed to be pointed at (empty for an unbound one)."""
    pid = external.bound_provider(engine)
    if not pid:
        return []
    return [m["model_name"] for m in store.list_models() if m["provider_id"] == pid and m["enabled"]]


class ExternalCreate(BaseModel):
    engine: str = "workbuddy"
    name: str | None = None
    group_id: str | None = None      # also pull it into this group
    cfg: dict = {}


class ExternalPatch(BaseModel):
    cfg: dict


class ExternalProbe(BaseModel):
    live: bool = False               # True = really send a message to try it (this calls a cloud model)
    engine: str = "workbuddy"
    agent_id: str | None = None
    cli_path: str = ""
    base_url: str = ""               # for a chat gateway that has not been saved yet
    api_key: str = ""
    model: str = ""                  # likewise; an engine bound to a provider takes it from there


def build_external_router(store: Store, runner: external.ExternalRunner) -> APIRouter:
    r = APIRouter()

    def need_enabled() -> None:
        if not store.get_settings()["external_agents_enabled"]:
            raise HTTPException(403, i18n.pick_now("The external-agent master switch is still off: turn it on under Settings → External agents, then try again", "外部智能体总开关还没打开:到「设置 → 外部智能体」里打开后再试"))

    def engine_of(agent: dict) -> str:
        return str(agent.get("engine") or "workbuddy")

    def cfg_of(agent: dict, *, reveal: bool = False) -> dict:
        """The member's settings with the API key resolved. With `reveal=False` (everything the UI
        sees) the key itself is left out and only `has_key` says whether one is stored — the same
        rule model provider keys follow.

        An engine bound to a provider owns none of the three: the address and the key are the
        provider's, and the model is one of its models. Reporting the member's own leftovers here is
        what made the dialog look as if it still kept them."""
        cfg = {**external.DEFAULT_CFG, **(agent.get("engine_cfg") or {})}
        if external.provider_bound(engine_of(agent)):
            bound = provider_view(store, engine_of(agent)) or {}
            cfg["base_url"] = ""
            cfg["api_key"] = ""
            cfg["has_key"] = bool(bound.get("has_key"))
            return cfg
        stored = str(cfg.get("api_key") or "")
        if reveal:
            cfg["api_key"] = store._secret_off(stored)
        else:
            cfg["api_key"] = ""
            cfg["has_key"] = bool(stored)
        return cfg

    def binding_of(agent: dict) -> dict:
        """What the member actually resolves to, minus the key: the provider it talks through, the
        model it will run, and whether that model is one the user chose or one we fell back to.
        Empty for an engine that is not bound to a provider."""
        engine = engine_of(agent)
        if not external.provider_bound(engine):
            return {}
        cfg, info = runner.resolve(engine, {**external.DEFAULT_CFG, **(agent.get("engine_cfg") or {})})
        prov = store.get_provider(info["provider_id"])
        return {
            "provider_id": info["provider_id"],
            "provider_name": (presets.localize_provider(prov, i18n.current())["name"]
                              if prov else info["provider_id"]),
            "model": str(cfg.get("model") or ""),
            "model_default": bool(info["model_default"]),
            "problem": info["problem"],
        }

    def keep_key(raw: dict, cur: dict) -> dict:
        """A `***` coming back means "I did not touch the key" — it is the placeholder we sent."""
        if str(raw.get("api_key") or "") == MASK:
            return {**raw, "api_key": cur.get("api_key") or ""}
        return raw

    def save(agent: dict, cfg: dict) -> dict:
        """Persist engine_cfg, putting any new key in the keychain and keeping only a reference."""
        cfg = dict(cfg)
        key = str(cfg.pop("api_key", "") or "")
        if key:
            cfg["api_key"] = store._secret_on("external", agent["id"], key)
        updated = store.update_agent(agent["id"], {"engine_cfg": cfg})
        return {**(updated or agent), "engine_cfg": cfg_of(updated or agent)}

    @r.get("/api/external")
    async def overview() -> dict:
        s = store.get_settings()
        engines = []
        for eid, e in external.ENGINES.items():
            shown = i18n.localize(e)
            engines.append({"id": eid, "name": shown["name"], "avatar": shown["avatar"],
                            "role": shown["role"], "kind": external.kind_of(eid),
                            "base_url": shown.get("base_url", ""), "docs": shown.get("docs", ""),
                            "key_hint": shown.get("key_hint", ""),
                            # True = this machine has one of these, so there is one member to add
                            # (`external.SINGLE_ENGINES`): the add-member list greys it out once one
                            # exists instead of offering a copy that would be refused anyway.
                            "single": eid in external.SINGLE_ENGINES,
                            # Non-null = the engine talks through a provider, and this is that
                            # provider: its address, its models, whether it has a key. The dialog
                            # asks for none of those itself, and the add-member list leaves the
                            # engine out entirely (its models join as ordinary members).
                            "provider": provider_view(store, eid),
                            **runner.describe(eid)})
        return {
            "enabled": bool(s["external_agents_enabled"]),
            "external_calls_enabled": bool(s["external_calls_enabled"]),
            "engines": engines,
            "levels": [{"id": k, **external.level_view(k)} for k in external.LEVELS],
            "defaults": external.DEFAULT_CFG,
            "members": [
                {"id": a["id"], "name": a["name"], "engine": engine_of(a), "cfg": cfg_of(a),
                 "binding": binding_of(a),
                 # a chat gateway has no working directory, and asking for one would create it
                 "workspace": (str(runner.workspace(a))
                               if s["external_agents_enabled"] and external.kind_of(engine_of(a)) == "cli" else "")}
                for a in store.list_agents() if a.get("engine")
            ],
        }

    @r.post("/api/external/agents")
    async def create(body: ExternalCreate) -> dict:
        need_enabled()
        eng = external.ENGINES.get(body.engine)
        if not eng:
            raise HTTPException(400, i18n.pick_now("Unsupported external agent type", "不支持的外部智能体类型"))
        try:
            cfg = external.clean_cfg(body.cfg, engine=body.engine,
                                    models=provider_model_names(store, body.engine))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        # Engines that exist once on this machine can only be added once (`external.SINGLE_ENGINES`).
        # The refusal names the member that is in the way, because what to do about it is to change
        # that one — not to add a second copy of the same engine.
        def refuse_a_second_one() -> None:
            if body.engine not in external.SINGLE_ENGINES:
                return
            same = [a for a in store.list_agents() if str(a.get("engine") or "") == body.engine]
            if not same:
                return
            eng_name = i18n.localize(eng)["name"]
            raise HTTPException(409, i18n.pick_now(
                f"This machine has one {eng_name} engine, so there is only one {eng_name} member to add — and “{same[0]['name']}” is already here. Change that member's settings instead of adding a second one.",
                f"这台机器上只有一个 {eng_name} 引擎,所以 {eng_name} 成员也只能有一个——现在已经有「{same[0]['name']}」了。请改那个成员的设置,不要再加一个。"))
        # An engine's display name may contain a space ("Cherry Studio") while a member name may
        # not, so the default is squeezed into a legal one. An explicit name is still checked.
        name = (body.name or re.sub(r"\s+", "", str(eng["name"]))).strip()
        if not name or len(name) > 30 or NAME_BAD.search(name):
            raise HTTPException(400, i18n.pick_now("A name cannot contain spaces or @, and is at most 30 characters long", "名字不能包含空格或 @,最长 30 字"))
        # Called after the name checks on purpose: a malformed request is still answered as a
        # malformed one, and this rule only decides between two well-formed ones.
        refuse_a_second_one()
        if any(a["name"] == name for a in store.list_agents()):
            if body.name:
                raise HTTPException(409, i18n.pick_now("A member with this name already exists", "已有同名成员"))
            n = 2
            while any(a["name"] == f"{name}{n}" for a in store.list_agents()):
                n += 1
            name = f"{name}{n}"
        if body.group_id and not store.get_group(body.group_id):
            raise HTTPException(404, i18n.pick_now("That group chat does not exist", "群聊不存在"))
        # The role and the prompt are stored in their canonical English, the way every other piece
        # of built-in content is: the display layer puts the reader's language back on top (see
        # `external.localize_member`). Storing them already localized is what left a member added
        # from a Chinese interface reading Chinese for ever — in an English one too.
        agent = store.create_agent(name, eng["avatar"], eng["role"], eng["prompt"], None, [], eng["tags"],
                                   engine=body.engine, engine_cfg={**cfg, "api_key": ""})
        agent = save(agent, cfg)                       # stores the key in the keychain, if one was given
        if body.group_id:
            store.add_member(body.group_id, agent["id"])
        return agent

    @r.patch("/api/external/agents/{aid}")
    async def patch(aid: str, body: ExternalPatch) -> dict:
        agent = store.get_agent(aid)
        if not agent or not agent.get("engine"):
            raise HTTPException(404, i18n.pick_now("That external agent member does not exist", "外部智能体成员不存在"))
        engine = engine_of(agent)
        current = cfg_of(agent, reveal=True)
        try:
            cfg = external.clean_cfg(keep_key(body.cfg, current), current, engine=engine,
                                    models=provider_model_names(store, engine))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return save(agent, cfg)

    @r.post("/api/external/test")
    async def test(body: ExternalProbe) -> dict:
        engine = body.engine or "workbuddy"
        cfg = {**external.DEFAULT_CFG, "cli_path": body.cli_path, "base_url": body.base_url,
               "api_key": body.api_key, "model": body.model}
        if body.agent_id:
            agent = store.get_agent(body.agent_id)
            if not agent or not agent.get("engine"):
                raise HTTPException(404, i18n.pick_now("That external agent member does not exist", "外部智能体成员不存在"))
            engine = engine_of(agent)
            cfg = cfg_of(agent, reveal=True)
        # A bound engine has no model of its own to be tested with until the user picks one, and the
        # dialog may be testing before anything is saved: resolving first turns the provider's first
        # model into the one under test, so "Check" answers something useful instead of refusing.
        if external.provider_bound(engine):
            cfg, _ = runner.resolve(engine, {**external.DEFAULT_CFG, **cfg})
        try:
            cfg = external.clean_cfg(cfg, engine=engine, models=provider_model_names(store, engine))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        need_enabled()   # detection really starts a command line (to read the version), so it is skipped when the
# master switch is off
        if body.live:
            if not store.get_settings()["external_calls_enabled"]:
                raise HTTPException(403, i18n.pick_now("Outbound calls are switched off: an external agent needs a cloud model, so allow outbound calls under Routing first", "「禁止外呼」正开着:外部智能体要连接云端模型,先在「路由」里放开"))
        return await runner.probe(cfg, live=body.live, engine=engine)

    return r
