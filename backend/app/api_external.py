"""外部智能体成员(WorkBuddy)的接口:检测、创建、修改设置、连通性测试。"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import external
from .store import Store

NAME_BAD = re.compile(r"[\s@]")


class ExternalCreate(BaseModel):
    engine: str = "workbuddy"
    name: str | None = None
    group_id: str | None = None      # 顺便拉进这个群
    cfg: dict = {}


class ExternalPatch(BaseModel):
    cfg: dict


class ExternalProbe(BaseModel):
    live: bool = False               # True = 真的发一句话试试(会调用云端模型)
    agent_id: str | None = None
    cli_path: str = ""


def build_external_router(store: Store, runner: external.ExternalRunner) -> APIRouter:
    r = APIRouter()

    def need_enabled() -> None:
        if not store.get_settings()["external_agents_enabled"]:
            raise HTTPException(403, "外部智能体总开关还没打开:到「设置 → 外部智能体」里打开后再试")

    def cfg_of(agent: dict) -> dict:
        return {**external.DEFAULT_CFG, **(agent.get("engine_cfg") or {})}

    @r.get("/api/external")
    async def overview() -> dict:
        s = store.get_settings()
        engines = []
        for eid, e in external.ENGINES.items():
            engines.append({"id": eid, "name": e["name"], "avatar": e["avatar"], "role": e["role"], **runner.describe()})
        return {
            "enabled": bool(s["external_agents_enabled"]),
            "external_calls_enabled": bool(s["external_calls_enabled"]),
            "engines": engines,
            "levels": [{"id": k, **v} for k, v in external.LEVELS.items()],
            "defaults": external.DEFAULT_CFG,
            "members": [
                {"id": a["id"], "name": a["name"], "engine": a["engine"], "cfg": cfg_of(a),
                 "workspace": str(runner.workspace(a)) if s["external_agents_enabled"] else ""}
                for a in store.list_agents() if a.get("engine")
            ],
        }

    @r.post("/api/external/agents")
    async def create(body: ExternalCreate) -> dict:
        need_enabled()
        eng = external.ENGINES.get(body.engine)
        if not eng:
            raise HTTPException(400, "不支持的外部智能体类型")
        try:
            cfg = external.clean_cfg(body.cfg)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        name = (body.name or eng["name"]).strip()
        if not name or len(name) > 30 or NAME_BAD.search(name):
            raise HTTPException(400, "名字不能包含空格或 @,最长 30 字")
        if any(a["name"] == name for a in store.list_agents()):
            if body.name:
                raise HTTPException(409, "已有同名成员")
            n = 2
            while any(a["name"] == f"{name}{n}" for a in store.list_agents()):
                n += 1
            name = f"{name}{n}"
        if body.group_id and not store.get_group(body.group_id):
            raise HTTPException(404, "群聊不存在")
        agent = store.create_agent(name, eng["avatar"], eng["role"], eng["prompt"], None, [], eng["tags"],
                                   engine=body.engine, engine_cfg=cfg)
        if body.group_id:
            store.add_member(body.group_id, agent["id"])
        return agent

    @r.patch("/api/external/agents/{aid}")
    async def patch(aid: str, body: ExternalPatch) -> dict:
        agent = store.get_agent(aid)
        if not agent or not agent.get("engine"):
            raise HTTPException(404, "外部智能体成员不存在")
        try:
            cfg = external.clean_cfg(body.cfg, cfg_of(agent))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return store.update_agent(aid, {"engine_cfg": cfg})  # type: ignore[return-value]

    @r.post("/api/external/test")
    async def test(body: ExternalProbe) -> dict:
        cfg = {"cli_path": body.cli_path}
        if body.agent_id:
            agent = store.get_agent(body.agent_id)
            if not agent or not agent.get("engine"):
                raise HTTPException(404, "外部智能体成员不存在")
            cfg = cfg_of(agent)
        try:
            cfg = external.clean_cfg(cfg)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        need_enabled()   # 检测会真的启动一次命令行(读版本),所以总开关没开时也不做
        if body.live:
            if not store.get_settings()["external_calls_enabled"]:
                raise HTTPException(403, "「禁止外呼」正开着:外部智能体要连接云端模型,先在「路由」里放开")
        return await runner.probe(cfg, live=body.live)

    return r
