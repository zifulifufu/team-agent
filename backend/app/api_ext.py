"""第四阶段新增的接口:模型挑选与强项、群聊扩展、插件 / MCP / 技能、资料库、记忆、提示词、更新。"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import i18n, modelopts, strengths as strength_lib
from .approvals import Approvals, risk_label, risk_of
from .discovery import DiscoveryError
from .library import Library, LibraryError
from .mcp_client import McpManager, parse_mcp_json, pick_transport, slug
from .presets import builtin_names, localize_prompt, localize_system_prompt
from .templates import group_view, member_view, skill_list_view, template_rows
from .gallery import MCP_TEMPLATES, mcp_display_name as display_mcp_name
from .memory import MemoryService
from .obsidian import ObsidianError, ObsidianSync
from .orchestrator import Orchestrator
from .presets import AGENT_PRESETS, DEFAULT_SYSTEM_PROMPT, TEMPLATES
from .prompting import VARIABLES, PromptBuilder, estimate_tokens, render_vars
from .router import ModelRouter, has_credentials
from .store import Store
from .templates import create_group_from_template, ensure_agent_from_key
from .toolhub import BUILTIN_TOOL_NAMES, ToolHub, builtin_specs
from .tools import (
    ToolRegistry,
    delete_skill,
    list_skills,
    localize_skill,
    safe_skill_name,
    skill_names,
    write_skill,
)
from .updater import GitHubError, Updater, curated

MASK = "••••••"

# MCP 用法清单的唯一数据源在 gallery.py(模板中心与「MCP」页共用,避免两处定义漂移)


# ------------------------------------------------------------------ schemas
class McpIn(BaseModel):
    name: str
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    transport: str = ""
    headers: dict[str, str] = {}
    description: str = ""
    enabled: bool = True


class LibraryUrlIn(BaseModel):
    url: str


class LibraryDirIn(BaseModel):
    path: str
    recursive: bool = True


class McpImportIn(BaseModel):
    text: str
    names: list[str] | None = None   # 只导入这几个;不填 = 全部


class McpPatch(BaseModel):
    name: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    transport: str | None = None
    headers: dict[str, str] | None = None
    description: str | None = None
    enabled: bool | None = None


class SkillIn(BaseModel):
    name: str
    description: str = ""
    body: str
    scope: str = "member"


class NoteIn(BaseModel):
    title: str
    content: str


class DocPatch(BaseModel):
    title: str | None = None
    enabled: bool | None = None


class MemoryIn(BaseModel):
    content: str
    scope: str = "global"
    scope_id: str = ""
    kind: str = "fact"
    pinned: bool = False


class MemoryPatch(BaseModel):
    content: str | None = None
    kind: str | None = None
    pinned: bool | None = None


class ObsidianIn(BaseModel):
    dir: str | None = None
    auto: bool | None = None


class PromptIn(BaseModel):
    title: str
    content: str
    kind: str = "general"
    use_globally: bool = False


class PromptPatch(BaseModel):
    title: str | None = None
    content: str | None = None
    kind: str | None = None
    use_globally: bool | None = None


class PromptPreviewIn(BaseModel):
    content: str
    agent_id: str | None = None
    group_id: str | None = None


class ApplyPromptIn(BaseModel):
    prompt_id: str
    mode: str = "replace"     # replace | append


class PresetIn(BaseModel):
    key: str


class ModelMemberIn(BaseModel):
    model_id: str


class TemplateIn(BaseModel):
    name: str | None = None


class LocalTagIn(BaseModel):
    tag: str
    note: str = ""


class RepoFileIn(BaseModel):
    repo: str
    path: str
    ref: str = ""
    overwrite: bool = False
    sha256: str = ""


class ApprovalIn(BaseModel):
    decision: str            # allow | deny
    remember: bool = False   # 仅对 allow 有效:以后这个工具不再问


@dataclass
class Ctx:
    store: Store
    router: ModelRouter
    orch: Orchestrator
    registry: ToolRegistry
    mcp: McpManager
    library: Library
    memory: MemoryService
    toolhub: ToolHub
    prompts: PromptBuilder
    updater: Updater
    approvals: Approvals
    obsidian: ObsidianSync


def _need(x: Any, what: str) -> Any:
    if x is None:
        raise HTTPException(404, i18n.pick_now(f"{what} not found", f"{what}不存在"))
    return x


def _need_octet(request: Request) -> None:
    """原始字节上传只认 application/octet-stream:这样别的网页想跨站塞一个 text/plain 表单进来,浏览器会先做预检并被拒绝。"""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/octet-stream":
        raise HTTPException(415, i18n.pick_now("The request must be application/octet-stream", "请求需要是 application/octet-stream"))


def _mask(d: dict[str, str]) -> dict[str, str]:
    return {k: (MASK if v else "") for k, v in d.items()}


def _unmask(new: dict[str, str], old: dict[str, str]) -> dict[str, str]:
    return {k: (old.get(k, "") if v == MASK else v) for k, v in new.items()}


def build_router(c: Ctx) -> APIRouter:
    r = APIRouter()
    store = c.store

    def gh(fn):  # type: ignore[no-untyped-def]
        """把 GitHubError 变成合适的 HTTP 错误。"""
        @functools.wraps(fn)
        async def wrapper(*a, **kw):  # type: ignore[no-untyped-def]
            try:
                return await fn(*a, **kw)
            except GitHubError as e:
                raise HTTPException(e.status, str(e)) from None
        return wrapper

    # =================================================== strengths / catalog
    @r.get("/api/strengths")
    async def strengths_list() -> dict:
        """Strength tags. The id is a stable ASCII key (stored in the database, never
        translated); label and desc follow the request language."""
        lang = i18n.current()
        return {"tags": [{"id": t, "label": strength_lib.label(t, lang),
                          "desc": strength_lib.description(t, lang)} for t, _ in strength_lib.TAGS]}

    @r.get("/api/catalog")
    async def catalog_info() -> dict:
        cat = store.catalog
        return {"version": cat.version, "source": cat.source,
                "providers": {k: len(v["models"]) for k, v in cat.data["providers"].items()}}

    @r.get("/api/providers/{pid}/model-options")
    async def model_options(pid: str) -> dict:
        _need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        return modelopts.model_options(store, pid)

    @r.post("/api/providers/{pid}/model-options/refresh")
    async def model_options_refresh(pid: str) -> dict:
        """向服务商查询实时清单(同时把新出现的型号标成「新」)。外呼禁用时只允许本地服务商。"""
        p = _need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        if not p["is_local"] and not store.get_settings()["external_calls_enabled"]:
            raise HTTPException(403, i18n.pick_now("Outbound calls are disabled, so the cloud provider's model list cannot be fetched", "外呼已禁用,无法向云端服务商查询模型列表"))
        try:
            await modelopts.refresh_live(store, pid)
        except DiscoveryError as e:
            raise HTTPException(502, str(e)) from None
        return modelopts.model_options(store, pid)

    @r.post("/api/providers/{pid}/model-options/seen")
    async def model_options_seen(pid: str) -> dict:
        _need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        modelopts.mark_seen(store, pid)
        return modelopts.model_options(store, pid)

    @r.get("/api/models/recommend")
    async def models_recommend(tags: str = "", limit: int = 5) -> dict:
        wanted = strength_lib.clean_tags([t for t in re.split(r"[,,\s]+", tags) if t])
        return {"tags": wanted, "models": c.router.rank_by_tags(wanted, max(1, min(limit, 20)))}

    # ============================================================ groups
    @r.get("/api/groups/{gid}/capabilities")
    async def capabilities(gid: str) -> dict:
        """成员分工表:每个成员现在实际用哪个模型、有哪些强项;以及本群可用的工具。"""
        group = _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        members = store.group_members(gid)
        host = next((m for m in members if m["id"] == group.get("host_agent_id")), members[0] if members else None)
        rows = []
        for e in c.prompts.roster_entries(members):
            m, model = e["agent"], e["model"]
            problem = ""
            if m["model_id"] and not (model and model["id"] == m["model_id"]):
                _, skipped = c.router.build_chain(m["model_id"], m.get("tags"))
                problem = next((s.detail for s in skipped if s.model_id == m["model_id"]), "") or i18n.pick_now("temporarily unavailable", "暂时不可用")
            rows.append({
                "agent_id": m["id"], "name": m["name"], "avatar": m["avatar"], "role": m["role"], "tags": m["tags"],
                "is_host": bool(host and m["id"] == host["id"]),
                "skills": skill_list_view(m["skills"]),
                "model": ({"id": model["id"], "display_name": model["display_name"], "strengths": model["strengths"],
                           "is_local": model["is_local"]} if model else None),
                "manual_model": bool(m["model_id"]), "strengths": e["strengths"], "origin": m.get("origin", ""), "engine": m.get("engine", ""),
                "model_problem": problem,  # 指定的模型现在用不了(没填 Key / 已停用 / 熔断…)时的原因,此时实际会回退到别的模型
            })
        ctx = await c.toolhub.context(group, host, connect=False) if host else None
        return {
            "members": rows,
            "tools": [{"name": t["name"], "description": t["description"], "source": t["source"]} for t in (ctx.specs() if ctx else [])],
            "problems": ctx.problems if ctx else [],
            "mcp_deferred": bool(ctx.mcp_deferred) if ctx else False,
            "ext": group["ext"],
            "docs": len([d for d in store.list_docs() if d["enabled"]]),
        }

    @r.post("/api/groups/{gid}/apply-prompt")
    async def apply_prompt(gid: str, body: ApplyPromptIn) -> dict:
        group = _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        p = _need(store.get_prompt(body.prompt_id), i18n.pick_now("Prompt", "提示词"))
        text = p["content"] if body.mode == "replace" or not group["prompt"].strip() else group["prompt"].rstrip() + "\n\n" + p["content"]
        return group_view(store.update_group(gid, {"prompt": text}))  # type: ignore[arg-type]

    @r.get("/api/agent-presets")
    async def agent_presets() -> list[dict]:
        have = {a["name"] for a in store.list_agents()}
        rows = [{**p, "exists": any(n in have for n in builtin_names(p))} for p in AGENT_PRESETS]
        return i18n.localize(rows)

    @r.post("/api/groups/{gid}/members/from-preset")
    async def member_from_preset(gid: str, body: PresetIn) -> dict:
        _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        agent = _need(ensure_agent_from_key(store, body.key), i18n.pick_now("Preset", "预设"))
        store.add_member(gid, agent["id"])
        return group_view(store.get_group(gid))  # type: ignore[arg-type]

    @r.post("/api/groups/{gid}/members/from-model")
    async def member_from_model(gid: str, body: ModelMemberIn) -> dict:
        """把「我添加的模型」直接拉进群当成员(没有对应的成员就自动创建一个,名字、强项取自模型)。"""
        _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        model = _need(store.get_model(body.model_id), i18n.pick_now("Model", "模型"))
        if not model["enabled"] or not model["provider_enabled"]:
            raise HTTPException(400, i18n.pick_now("This model, or its provider, is disabled — enable it under Model providers first", "这个模型或它的服务商已停用,先在「模型服务」里启用"))
        agent = _need(store.ensure_model_agent(body.model_id), i18n.pick_now("Model", "模型"))
        store.add_member(gid, agent["id"])
        return group_view(store.get_group(gid))  # type: ignore[arg-type]

    @r.get("/api/templates")
    async def templates() -> list[dict]:
        return template_rows()

    @r.post("/api/templates/{tid}/create-group")
    async def template_create(tid: str, body: TemplateIn) -> dict:
        return _need(create_group_from_template(store, tid, body.name), i18n.pick_now("Template", "模板"))

    # ============================================================ 数据 / 导出
    @r.post("/api/data/restore")
    async def data_restore(request: Request) -> dict:
        """请求体就是备份文件(.db)的原始字节。会替换当前全部数据,并先自动留一份当前数据的副本。"""
        _need_octet(request)
        data = await request.body()
        if not data:
            raise HTTPException(400, i18n.pick_now("The file is empty", "文件是空的"))
        fd, tmp = tempfile.mkstemp(suffix=".db")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                res = await asyncio.to_thread(store.restore_from, tmp)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        finally:
            os.unlink(tmp)
        c.library.invalidate()
        c.router.reset_circuit()
        await c.mcp.shutdown()   # 恢复后 MCP 配置可能变了,旧连接不再对应
        return res

    def chat_markdown(gid: str) -> tuple[dict, str]:
        g = _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        agents = {a["id"]: a for a in member_view(store.list_agents())}
        members = [agents[i]["name"] for i in g["member_ids"] if i in agents]
        out = [f"# {g['name']}", "", i18n.pick_now(f"Exported: {time.strftime('%Y-%m-%d %H:%M')} · Members: {', '.join(members) or 'none'}", f"导出时间:{time.strftime('%Y-%m-%d %H:%M')} · 成员:{'、'.join(members) or '无'}"), ""]
        for m in store.list_messages(gid, 1_000_000):
            when = time.strftime("%m-%d %H:%M", time.localtime(m["created_at"]))
            if m["sender_type"] == "system":
                out += [i18n.pick_now(f"> System · {when}: {m['content']}", f"> 系统 · {when}:{m['content']}"), ""]
            elif m["sender_type"] == "plan":
                board = m["meta"] or {}
                out += [i18n.pick_now(f"**Task board** · {when}: {board.get('goal', '')}", f"**任务板** · {when}:{board.get('goal', '')}")]
                out += [f"- [{t.get('status', '')}] {t.get('owner', '')}:{t.get('title', '')}" for t in board.get("tasks", [])] + [""]
            else:
                out += [f"**{m['sender_name']}** · {when}", "", m["content"], ""]
                for t in (m["meta"] or {}).get("tools", []):
                    out.append(i18n.pick_now(f"> Tool call `{t.get('name')}`: {t.get('status')}", f"> 调用工具 `{t.get('name')}`:{t.get('status')}"))
                if (m["meta"] or {}).get("tools"):
                    out.append("")
        return g, "\n".join(out).rstrip() + "\n"

    @r.get("/api/groups/{gid}/export")
    async def group_export(gid: str) -> Response:
        g, text = chat_markdown(gid)
        name = urllib.parse.quote(f"{g['name']}-{time.strftime('%Y%m%d')}.md")
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    @r.post("/api/groups/{gid}/export-obsidian")
    async def group_export_obsidian(gid: str) -> dict:
        g, text = chat_markdown(gid)
        try:
            p = await asyncio.to_thread(c.obsidian.write_export, f"{g['name']}-{time.strftime('%Y%m%d-%H%M')}", text)
        except ObsidianError as e:
            raise HTTPException(400, str(e)) from None
        return {"path": str(p)}

    # ============================================================ Obsidian
    @r.get("/api/obsidian")
    async def obsidian_status() -> dict:
        return {**c.obsidian.status(), "vaults": c.obsidian.detect_vaults()}

    @r.put("/api/obsidian")
    async def obsidian_config(body: ObsidianIn) -> dict:
        patch: dict[str, Any] = {}
        if body.dir is not None:
            if body.dir.strip():
                try:
                    p = c.obsidian.validate_dir(body.dir, store.data_dir)
                except ObsidianError as e:
                    raise HTTPException(400, str(e)) from None
                patch["obsidian_dir"] = str(p)
            else:
                patch["obsidian_dir"] = ""
            if patch["obsidian_dir"] != store.get_settings()["obsidian_dir"]:
                store.clear_obsidian_map()       # 换了文件夹:旧的对应关系作废
                store.set_meta("obsidian_last", "")
        if body.auto is not None:
            patch["obsidian_auto"] = body.auto
        store.update_settings(patch)
        return c.obsidian.status()

    @r.post("/api/obsidian/sync")
    async def obsidian_sync(force: bool = False) -> dict:
        return await asyncio.to_thread(c.obsidian.sync, force)

    # ============================================================ 权限与操控
    @r.get("/api/approvals")
    async def approvals_pending(group_id: str | None = None) -> list[dict]:
        return c.approvals.list(group_id)

    @r.post("/api/approvals/{aid}")
    async def approval_answer(aid: str, body: ApprovalIn) -> dict:
        if body.decision not in ("allow", "deny"):
            raise HTTPException(400, i18n.pick_now("decision must be allow or deny", "decision 只能是 allow 或 deny"))
        if not c.approvals.resolve(aid, body.decision == "allow", body.remember):
            raise HTTPException(404, i18n.pick_now("This approval has expired or was already handled", "这条审批已经过期或被处理了"))
        return {"ok": True}

    @r.get("/api/permissions")
    async def permissions() -> dict:
        """「权限与操控」页要展示的事实:模式、名单、当前有哪些工具及其风险等级、这个应用能碰到什么。"""
        cfg = store.get_settings()
        tools: list[dict] = []

        def row(spec: dict, group: str) -> None:
            tools.append({"name": spec["name"], "group": group, "risk": risk_of(spec), "risk_label": risk_label(risk_of(spec)),
                          "policy": c.toolhub.policy(spec)})

        for n in BUILTIN_TOOL_NAMES:
            row({"name": n, "source": "builtin"}, i18n.pick_now("built-in", "内置"))
        for p in c.registry.plugins.values():
            for t in c.registry.plugin_tools([p.id]):
                row({"name": t.name, "source": "plugin"}, i18n.pick_now(f"Plugin · {p.name or p.id}", f"插件 · {p.name or p.id}"))
        for s in store.list_mcp():
            st = c.mcp.state(s["id"])
            for t in (st.tools if st and st.status == "ready" else []):
                spec = {"name": f"mcp__{slug(s['name'])}__{t['name']}", "source": "mcp", "read_only": t.get("read_only", False)}
                row(spec, f"MCP · {s['name']}")
        providers = store.list_providers()
        groups = store.list_groups()
        return {
            "mode": cfg["perm_mode"], "timeout": cfg["perm_timeout"], "allow": cfg["perm_allow"], "deny": cfg["perm_deny"],
            "tools": tools,
            "access": {
                "external_calls": cfg["external_calls_enabled"],
                "cloud_providers": [p["name"] for p in providers if not p["is_local"] and p["enabled"] and has_credentials(p)],
                "data_dir": str(store.data_dir), "plugins_dir": str(store.data_dir / "plugins"),
                "plugins": [{"id": p.id, "name": p.name or p.id, "tools": len(p.tools), "error": p.error} for p in c.registry.plugins.values()],
                "mcp": [{"id": s["id"], "name": s["name"], "enabled": s["enabled"],
                         "kind": i18n.pick_now("remote", "远程") if s["url"] and not s["command"] else i18n.pick_now("local process", "本地进程"), "command": s["command"]} for s in store.list_mcp()],
                "library_docs": len(store.list_docs()),
                "groups": [{"id": g["id"], "name": g["name"], "plugins": len(g["ext"]["plugins"]), "mcp": len(g["ext"]["mcp"])}
                           for g in groups if g["ext"]["plugins"] or g["ext"]["mcp"]],
            },
        }

    # ============================================================ plugins
    def plugin_view(p: dict) -> dict:
        src = store.get_source("plugin", p["id"])
        return {**p, "source": ({"repo": src["repo"], "path": src["path"]} if src else None)}

    @r.get("/api/plugins")
    async def plugins() -> list[dict]:
        return [plugin_view(p.to_dict()) for p in c.registry.plugins.values()]

    @r.post("/api/plugins/reload")
    async def plugins_reload() -> list[dict]:
        c.registry.load_plugins(store.data_dir / "plugins")
        return await plugins()

    @r.get("/api/plugins/{pid}/source")
    async def plugin_source(pid: str) -> dict:
        f = store.data_dir / "plugins" / f"{pid}.py"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pid) or not f.exists():
            raise HTTPException(404, i18n.pick_now("Plugin not found", "插件不存在"))
        return {"id": pid, "content": f.read_text(encoding="utf-8", errors="replace")}

    @r.delete("/api/plugins/{pid}")
    async def plugin_delete(pid: str) -> dict:
        f = store.data_dir / "plugins" / f"{pid}.py"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pid) or not f.exists():
            raise HTTPException(404, i18n.pick_now("Plugin not found", "插件不存在"))
        f.unlink()
        store.delete_source("plugin", pid)
        c.registry.load_plugins(store.data_dir / "plugins")
        return {"ok": True}

    @r.get("/api/tools")
    async def tools() -> dict:
        builtin = [{"name": n, "description": s["description"], "parameters": s["parameters"], "source": "builtin"}
                   for n, s in builtin_specs().items()]
        return {"tools": builtin + c.registry.list(), "errors": c.registry.errors}

    # ================================================================ mcp
    def mcp_view(m: dict) -> dict:
        st = c.mcp.state(m["id"])
        lang = i18n.current()
        return {
            **{k: v for k, v in m.items() if k not in ("env", "headers")},
            "name": display_mcp_name(m["name"], lang),
            "env": _mask(m["env"]), "headers": _mask(m["headers"]),
            "transport_effective": pick_transport(m),
            "status": st.status if st else "idle", "error": st.error if st else "",
            "tools": [{"name": t["name"], "description": t["description"], "read_only": t["read_only"]} for t in (st.tools if st else [])],
        }

    def check_mcp_cfg(name: str, command: str, url: str, transport: str) -> None:
        if not name.strip():
            raise HTTPException(400, i18n.pick_now("A name is required", "请填写名称"))
        if not command.strip() and not url.strip():
            raise HTTPException(400, i18n.pick_now("Enter a start command (local) or a service URL (remote)", "请填写启动命令(本地)或服务地址(远程)"))
        if transport and transport not in ("stdio", "sse", "http"):
            raise HTTPException(400, i18n.pick_now("The transport must be one of stdio / sse / http", "传输方式只能是 stdio / sse / http"))
        if url and not re.match(r"^https?://", url):
            raise HTTPException(400, i18n.pick_now("The service URL must start with http:// or https://", "服务地址必须以 http:// 或 https:// 开头"))

    @r.get("/api/mcp/templates")
    async def mcp_templates() -> list[dict]:
        return i18n.localize(MCP_TEMPLATES)

    @r.get("/api/mcp")
    async def mcp_list() -> list[dict]:
        return [mcp_view(m) for m in store.list_mcp()]

    @r.post("/api/mcp")
    async def mcp_add(body: McpIn) -> dict:
        check_mcp_cfg(body.name, body.command, body.url, body.transport)
        m = store.add_mcp(body.name.strip(), body.command.strip(), body.args, body.env, body.url.strip(),
                          body.transport, body.headers, body.description)
        if not body.enabled:
            m = store.update_mcp(m["id"], {"enabled": False})  # type: ignore[assignment]
        return mcp_view(m)

    @r.post("/api/mcp/import/parse")
    async def mcp_import_parse(body: McpImportIn) -> dict:
        """解析别处导出的 MCP 配置 JSON。只解析,不保存、不运行;界面让你逐个确认后才会添加。"""
        try:
            servers, warnings = parse_mcp_json(body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        have = {m["name"] for m in store.list_mcp()}
        return {"servers": [{**s, "exists": s["name"] in have,
                             "env": _mask(s["env"]), "headers": _mask(s["headers"])} for s in servers],
                "warnings": warnings}       # 带密钥的字段只显示掩码;真正导入时由后端重新解析原文,密钥不经过界面

    @r.post("/api/mcp/import")
    async def mcp_import(body: McpImportIn) -> dict:
        try:
            servers, _ = parse_mcp_json(body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        have = {m["name"] for m in store.list_mcp()}
        added, skipped, todo = [], [], []
        for s in servers:
            if body.names is not None and s["name"] not in body.names:
                continue
            if s["name"] in have:
                skipped.append(s["name"])
                continue
            check_mcp_cfg(s["name"], s["command"], s["url"], s["transport"])   # 先全部检查完再保存:不会导入一半就报错
            todo.append(s)
            have.add(s["name"])
        for s in todo:
            m = store.add_mcp(s["name"], s["command"], s["args"], s["env"], s["url"], s["transport"], s["headers"], s["description"])
            if not s["enabled"]:
                m = store.update_mcp(m["id"], {"enabled": False})  # type: ignore[assignment]
            added.append(mcp_view(m))
        return {"added": added, "skipped": skipped}

    @r.patch("/api/mcp/{mid}")
    async def mcp_patch(mid: str, body: McpPatch) -> dict:
        old = _need(store.get_mcp(mid), i18n.pick_now("MCP server", "MCP 服务器"))
        patch = body.model_dump(exclude_unset=True)
        if "env" in patch and patch["env"] is not None:
            patch["env"] = _unmask(patch["env"], old["env"])
        if "headers" in patch and patch["headers"] is not None:
            patch["headers"] = _unmask(patch["headers"], old["headers"])
        merged = {**old, **{k: v for k, v in patch.items() if v is not None}}
        check_mcp_cfg(merged["name"], merged["command"], merged["url"], merged["transport"])
        m = store.update_mcp(mid, patch)
        if patch.get("enabled") is False:
            await c.mcp.disconnect(mid)
        return mcp_view(m)  # type: ignore[arg-type]

    @r.delete("/api/mcp/{mid}")
    async def mcp_del(mid: str) -> dict:
        await c.mcp.disconnect(mid)
        store.delete_mcp(mid)
        for g in store.list_groups():  # 群里的勾选也一并清掉
            if mid in g["ext"]["mcp"]:
                store.update_group(g["id"], {"ext": {"mcp": [x for x in g["ext"]["mcp"] if x != mid]}})
        return {"ok": True}

    @r.post("/api/mcp/{mid}/connect")
    async def mcp_connect(mid: str) -> dict:
        m = _need(store.get_mcp(mid), i18n.pick_now("MCP server", "MCP 服务器"))
        if pick_transport(m) != "stdio" and not store.get_settings()["external_calls_enabled"]:
            raise HTTPException(403, i18n.pick_now("Outbound calls are disabled, so remote MCP servers will not be contacted", "外呼已禁用,不会连接远程 MCP 服务器"))
        await c.mcp.connect(m, timeout=45)
        return mcp_view(m)

    @r.post("/api/mcp/{mid}/disconnect")
    async def mcp_disconnect(mid: str) -> dict:
        m = _need(store.get_mcp(mid), i18n.pick_now("MCP server", "MCP 服务器"))
        await c.mcp.disconnect(mid)
        return mcp_view(m)

    # ============================================================ skills
    def skill_view(s, with_body: bool = False) -> dict:  # type: ignore[no-untyped-def]
        # A built-in skill is stored language-neutrally; show it in the request language
        # (anything the user rewrote stays as they wrote it).
        shown = localize_skill(s, i18n.current())
        src = store.get_source("skill", s.name)
        out = {**shown.summary(), "source": ({"repo": src["repo"], "path": src["path"]} if src else None)}
        if with_body:
            out["body"] = shown.body
        return out

    @r.get("/api/skills")
    async def skills() -> list[dict]:
        return [skill_view(s) for s in list_skills(store.data_dir / "skills")]

    def find_skill(name: str):  # type: ignore[no-untyped-def]
        """The installed skill answering to `name`, in either language."""
        return next((x for x in list_skills(store.data_dir / "skills")
                     if x.name == name or name in skill_names(x.name)), None)

    @r.get("/api/skills/{name}")
    async def skill_get(name: str) -> dict:
        return skill_view(_need(find_skill(name), i18n.pick_now("Skill", i18n.pick_now("Skill", "技能"))), True)

    @r.post("/api/skills")
    async def skill_create(body: SkillIn) -> dict:
        if not safe_skill_name(body.name) or not body.body.strip():
            raise HTTPException(400, i18n.pick_now("Both a skill name and its content are required", "请填写技能名称和内容"))
        if any(s.name == safe_skill_name(body.name) for s in list_skills(store.data_dir / "skills")):
            raise HTTPException(409, i18n.pick_now("A skill with this name already exists", "已有同名技能"))
        return skill_view(write_skill(store.data_dir / "skills", body.name, body.description, body.body, body.scope), True)

    @r.put("/api/skills/{name}")
    async def skill_update(name: str, body: SkillIn) -> dict:
        if not any(s.name == name for s in list_skills(store.data_dir / "skills")):
            raise HTTPException(404, i18n.pick_now("Skill not found", "技能不存在"))
        if not safe_skill_name(body.name) or not body.body.strip():
            raise HTTPException(400, i18n.pick_now("Both a skill name and its content are required", "请填写技能名称和内容"))
        new = write_skill(store.data_dir / "skills", body.name, body.description, body.body, body.scope, old_name=name)
        if new.name != name:  # 改名:成员和群里的勾选跟着改
            for a in store.list_agents():
                if name in a["skills"]:
                    store.update_agent(a["id"], {"skills": [new.name if x == name else x for x in a["skills"]]})
            for g in store.list_groups():
                if name in g["ext"]["skills"]:
                    store.update_group(g["id"], {"ext": {"skills": [new.name if x == name else x for x in g["ext"]["skills"]]}})
            src = store.get_source("skill", name)
            if src:
                store.delete_source("skill", name)
        return skill_view(new, True)

    @r.delete("/api/skills/{name}")
    async def skill_delete(name: str) -> dict:
        if not delete_skill(store.data_dir / "skills", name):
            raise HTTPException(404, i18n.pick_now("Skill not found", "技能不存在"))
        store.delete_source("skill", name)
        for a in store.list_agents():
            if name in a["skills"]:
                store.update_agent(a["id"], {"skills": [x for x in a["skills"] if x != name]})
        for g in store.list_groups():
            if name in g["ext"]["skills"]:
                store.update_group(g["id"], {"ext": {"skills": [x for x in g["ext"]["skills"] if x != name]}})
        return {"ok": True}

    # ============================================================ library
    @r.get("/api/library")
    async def library_list() -> dict:
        docs = store.list_docs()
        return {"docs": docs, "total_chars": sum(d["chars"] for d in docs), "count": len(docs)}

    @r.post("/api/library/upload")
    async def library_upload(request: Request, filename: str) -> dict:
        """请求体就是文件原始字节(不用 multipart,少一个依赖)。"""
        _need_octet(request)
        data = await request.body()
        if not data:
            raise HTTPException(400, i18n.pick_now("The file is empty", "文件是空的"))
        try:
            return c.library.add_file(filename, data)
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/library/note")
    async def library_note(body: NoteIn) -> dict:
        try:
            return c.library.add_text(body.title, body.content)
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/library/url")
    async def library_url(body: LibraryUrlIn) -> dict:
        if not store.get_settings()["external_calls_enabled"]:
            raise HTTPException(403, i18n.pick_now("Outbound calls are disabled, so web pages cannot be fetched (you can turn this on under Permissions & control)", "外呼已禁用,不能抓取网页(在「权限与操控」里可以打开)"))
        try:
            return await asyncio.to_thread(c.library.add_url, body.url)
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/library/dir")
    async def library_dir(body: LibraryDirIn) -> dict:
        try:
            return await asyncio.to_thread(c.library.add_dir, body.path, body.recursive)
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.get("/api/library/search")
    async def library_search(q: str, top_k: int = 5) -> list[dict]:
        # 大库首次检索要重建 BM25 索引,放线程池,避免阻塞事件循环
        return await asyncio.to_thread(c.library.search, q, max(1, min(top_k, 20)))

    @r.get("/api/library/{did}")
    async def library_read(did: str, start: int = 0) -> dict:
        try:
            return await asyncio.to_thread(c.library.read, did, max(0, start), 6000)
        except LibraryError as e:
            raise HTTPException(404, str(e)) from None

    @r.patch("/api/library/{did}")
    async def library_patch(did: str, body: DocPatch) -> dict:
        _need(store.get_doc(did), i18n.pick_now("Document", "文档"))
        return c.library.update(did, body.model_dump(exclude_unset=True))  # type: ignore[return-value]

    @r.delete("/api/library/{did}")
    async def library_delete(did: str) -> dict:
        _need(store.get_doc(did), i18n.pick_now("Document", "文档"))
        c.library.delete(did)
        for g in store.list_groups():
            ids = g["ext"]["library"]["ids"]
            if did in ids:
                store.update_group(g["id"], {"ext": {"library": {**g["ext"]["library"], "ids": [x for x in ids if x != did]}}})
        return {"ok": True}

    # ============================================================ memory
    @r.get("/api/memories")
    async def memories(scope: str = "", scope_id: str = "", kind: str = "", q: str = "") -> dict:
        rows = store.list_memories(scope or None, scope_id if scope_id else None, kind or None)
        if q.strip():
            key = q.strip().lower()
            rows = [m for m in rows if key in m["content"].lower()]
        groups = {g["id"]: g["name"] for g in store.list_groups()}
        agents = {a["id"]: a["name"] for a in member_view(store.list_agents())}
        for m in rows:
            m["scope_name"] = groups.get(m["scope_id"]) if m["scope"] == "group" else agents.get(m["scope_id"]) if m["scope"] == "agent" else ""
        return {"memories": rows, "count": len(rows)}

    @r.post("/api/memories")
    async def memory_add(body: MemoryIn) -> dict:
        if not body.content.strip() or len(body.content) > 500:
            raise HTTPException(400, i18n.pick_now("The content cannot be empty, and must be 500 characters or fewer", "内容不能为空,且不超过 500 字"))
        if body.scope in ("group", "agent") and not body.scope_id:
            raise HTTPException(400, i18n.pick_now("A group or member memory needs a scope_id", "群/成员记忆需要指定 scope_id"))
        return store.add_memory(body.content, body.scope, body.scope_id, body.kind, "manual", body.pinned)

    @r.patch("/api/memories/{mid}")
    async def memory_patch(mid: str, body: MemoryPatch) -> dict:
        _need(store.get_memory(mid), i18n.pick_now("Memory", "记忆"))
        return store.update_memory(mid, body.model_dump(exclude_unset=True))  # type: ignore[return-value]

    @r.delete("/api/memories/{mid}")
    async def memory_delete(mid: str) -> dict:
        _need(store.get_memory(mid), i18n.pick_now("Memory", "记忆"))
        store.delete_memory(mid)
        return {"ok": True}

    @r.delete("/api/memories")
    async def memory_clear(scope: str = "", scope_id: str = "", source: str = "") -> dict:
        if not (scope or source):
            raise HTTPException(400, i18n.pick_now("To avoid clearing everything by accident, pass scope or source", "为避免误清空,请指定 scope 或 source"))
        return {"deleted": store.clear_memories(scope or None, scope_id if scope_id else None, source or None)}

    # ============================================================ prompts
    @r.get("/api/prompts")
    async def prompts_list() -> dict:
        lang = i18n.current()
        return {
            "prompts": [localize_prompt(p, lang) for p in store.list_prompts()],
            "system_prompt": localize_system_prompt(store.get_settings()["system_prompt"], lang),
            "default_system_prompt": localize_system_prompt(DEFAULT_SYSTEM_PROMPT, lang),
            "variables": [{"name": n, "desc": i18n.pick(lang, d_en, d_zh)}
                          for n, d_en, d_zh in VARIABLES],
        }

    @r.post("/api/prompts")
    async def prompts_add(body: PromptIn) -> dict:
        if not body.title.strip() or not body.content.strip():
            raise HTTPException(400, i18n.pick_now("Both a title and content are required", "请填写标题和内容"))
        return store.add_prompt(body.title.strip(), body.content, body.kind, body.use_globally)

    @r.patch("/api/prompts/{pid}")
    async def prompts_patch(pid: str, body: PromptPatch) -> dict:
        _need(store.get_prompt(pid), i18n.pick_now("Prompt", "提示词"))
        return store.update_prompt(pid, body.model_dump(exclude_unset=True))  # type: ignore[return-value]

    @r.delete("/api/prompts/{pid}")
    async def prompts_delete(pid: str) -> dict:
        _need(store.get_prompt(pid), i18n.pick_now("Prompt", "提示词"))
        store.delete_prompt(pid)
        return {"ok": True}

    @r.post("/api/prompts/preview")
    async def prompts_preview(body: PromptPreviewIn) -> dict:
        group = store.get_group(body.group_id) if body.group_id else (store.list_groups() or [None])[0]
        agent = store.get_agent(body.agent_id) if body.agent_id else None
        members = store.group_members(group["id"]) if group else []
        agent = agent or (members[0] if members else None)
        if group:
            vals = c.prompts.values(group, agent, members)
        else:
            vals = {}
        text = render_vars(body.content, vals)
        return {"text": text, "tokens": estimate_tokens(text), "raw_tokens": estimate_tokens(body.content)}

    @r.post("/api/prompts/reset-system")
    async def prompts_reset_system() -> dict:
        store.update_settings({"system_prompt": DEFAULT_SYSTEM_PROMPT})
        return {"system_prompt": DEFAULT_SYSTEM_PROMPT}

    @r.get("/api/groups/{gid}/system-prompt-preview")
    async def system_prompt_preview(gid: str, agent_id: str = "") -> dict:
        """看看某个成员在本群里实际收到的完整系统提示词(不含工具与记忆的实时部分)。"""
        group = _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        members = store.group_members(gid)
        agent = store.get_agent(agent_id) if agent_id else (members[0] if members else None)
        _need(agent, i18n.pick_now("Member", "成员"))
        text = c.prompts.system_prompt(group, agent, members)  # type: ignore[arg-type]
        return {"text": text, "tokens": estimate_tokens(text)}

    # ============================================================ updates
    @r.get("/api/updates")
    async def updates_list() -> dict:
        try:
            last = json.loads(store.get_meta("last_update_check", "null"))
        except ValueError:
            last = None
        cfg = store.get_settings()
        return {
            "items": store.list_updates("new"),
            "last_check": last,
            "checking": c.updater.checking,
            "configured": bool(cfg["app_repo"]),
            "curated": curated(),
            "catalog": {"version": store.catalog.version, "source": store.catalog.source},
            "sources": store.list_sources(),
        }

    @r.post("/api/updates/check")
    async def updates_check() -> dict:
        return await c.updater.check_all(auto_apply=True)

    @r.post("/api/updates/{uid}/dismiss")
    async def updates_dismiss(uid: str) -> dict:
        _need(store.list_update(uid), i18n.pick_now("Reminder", "提醒"))
        store.set_update_status(uid, "dismissed")
        return {"ok": True}

    @r.get("/api/updates/search")
    @gh
    async def updates_search(kind: str, q: str = "") -> list[dict]:
        return await c.updater.search(kind, q)

    @r.get("/api/updates/repo/skills")
    @gh
    async def updates_repo_skills(repo: str, ref: str = "") -> list[dict]:
        return await c.updater.skill_files(repo, ref)

    @r.get("/api/updates/repo/plugins")
    @gh
    async def updates_repo_plugins(repo: str, ref: str = "") -> list[dict]:
        return await c.updater.plugin_files(repo, ref)

    @r.get("/api/updates/repo/readme")
    @gh
    async def updates_repo_readme(repo: str) -> dict:
        return await c.updater.readme(repo)

    @r.post("/api/updates/preview")
    @gh
    async def updates_preview(body: RepoFileIn) -> dict:
        return await c.updater.preview(body.repo, body.path, body.ref)

    @r.post("/api/updates/skill/install")
    @gh
    async def updates_skill_install(body: RepoFileIn) -> dict:
        s = await c.updater.install_skill(body.repo, body.path, body.ref, body.overwrite)
        return s.summary()

    @r.post("/api/updates/skill/update/{name}")
    @gh
    async def updates_skill_update(name: str) -> dict:
        src = _need(store.get_source("skill", name), i18n.pick_now("Skill source", "技能来源"))
        s = await c.updater.install_skill(src["repo"], src["path"], src["ref"], True)
        return s.summary()

    @r.post("/api/updates/plugin/install")
    @gh
    async def updates_plugin_install(body: RepoFileIn) -> dict:
        if not re.fullmatch(r"[0-9a-f]{64}", body.sha256 or ""):
            raise HTTPException(400, i18n.pick_now("Installing a plugin requires previewing its source first, and passing the sha256 the preview returned", "安装插件必须先预览源码,并带上预览得到的 sha256"))
        stem = await c.updater.install_plugin(body.repo, body.path, body.ref, body.sha256, body.overwrite)
        c.registry.load_plugins(store.data_dir / "plugins")
        return {"id": stem, "plugins": [p.to_dict() for p in c.registry.plugins.values()]}

    @r.post("/api/updates/catalog/apply")
    @gh
    async def updates_catalog_apply() -> dict:
        return await c.updater.check_catalog(apply=True)

    # ------------------------------------------------ 本地模型:发现 / 加入推荐
    @r.post("/api/local/check")
    @gh
    async def local_check() -> dict:
        """现在就去找新的开源大模型(不等定时检查)。结果同时写入「更新与发现」的提醒。"""
        cat_info = None
        try:
            cat_info = await c.updater.check_local_catalog(apply=True)
        except GitHubError as e:
            cat_info = {"error": str(e)}
        found = await c.updater.check_local_models()
        return {**found, "catalog_update": cat_info}

    @r.post("/api/local/catalog/apply")
    @gh
    async def local_catalog_apply() -> dict:
        return await c.updater.check_local_catalog(apply=True)

    @r.post("/api/local/probe")
    @gh
    async def local_probe(body: LocalTagIn) -> dict:
        return await c.updater.probe_ollama(body.tag.strip())

    @r.post("/api/local/catalog/add")
    @gh
    async def local_catalog_add(body: LocalTagIn) -> dict:
        """把一个 Ollama 型号加入推荐列表。先向 Ollama 注册表确认它存在并读出大小,不存在就拒绝;不会下载权重。"""
        tag = body.tag.strip()
        pr = await c.updater.probe_ollama(tag)
        if not pr["exists"]:
            raise HTTPException(404, i18n.pick_now(f"Ollama has no model called {tag} (it may only exist in the cloud, or the name may be misspelled)", f"Ollama 模型库里找不到 {tag}(可能只有云端版,或名字写错了)"))
        entry = store.local_catalog.add_extra(tag, pr["size_gb"] or 0, body.note, source="probe")
        store.resolve_updates("localmodel", tag.split(":", 1)[0])
        return entry

    @r.delete("/api/local/catalog/extra")
    async def local_catalog_remove(tag: str) -> dict:
        if not store.local_catalog.remove_extra(tag):
            raise HTTPException(404, i18n.pick_now("That model is not in the recommendation list", "推荐列表里没有这个型号"))
        return {"ok": True}

    return r
