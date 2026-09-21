"""Endpoints added in phase four: model selection and strengths, group chat extensions,
plugins / MCP / skills, the library, memory, prompting, and updates."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import coderun, i18n, images, modelopts, strengths as strength_lib, video
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
from .store import Store, new_id
from .templates import create_group_from_template, ensure_agent_from_key
from .toolhub import ToolHub, builtin_specs
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

# the single source of truth for the MCP usage list lives in gallery.py (shared by the template
# center and the "MCP" page, so the two definitions cannot drift apart)


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
    kb_id: str = ""
    group_id: str = ""


class LibraryDirIn(BaseModel):
    path: str
    recursive: bool = True
    kb_id: str = ""
    group_id: str = ""


class McpImportIn(BaseModel):
    text: str
    names: list[str] | None = None   # import only these; empty = all


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
    # Either an explicit knowledge base, or a group whose workspace knowledge base receives it
    kb_id: str = ""
    group_id: str = ""


class DocPatch(BaseModel):
    title: str | None = None
    enabled: bool | None = None
    kb_id: str | None = None          # moves the document to another knowledge base


class KBIn(BaseModel):
    name: str
    description: str = ""
    group_id: str = ""                # "" = shared (any group may attach it)


class KBPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    group_id: str | None = None


class CollectionIn(BaseModel):
    name: str
    description: str = ""
    kb_ids: list[str] = []


class CollectionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    kb_ids: list[str] | None = None


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
    remember: bool = False   # only applies to allow: never ask again for this tool


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
    """Raw byte uploads only accept application/octet-stream: if another site tries to push a
    cross-origin text/plain form in, the browser preflights it first and the request is rejected."""
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
        """Turn a GitHubError into the matching HTTP error."""
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
        """Query the provider for its live model list, marking newly appeared models as "new".
        When outbound calls are disabled, only local providers are allowed."""
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
        """Division of labour: which model each member is actually using right now, which strengths
        they have, and the tools available to this group."""
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
                "model_problem": problem,  # why the requested model cannot be used right now (no key set / disabled / circuit broken...); requests fall back to another model
            })
        ctx = await c.toolhub.context(group, host, connect=False) if host else None
        cfg = store.get_settings()
        vprov, vwhy = video.pick_provider(store, cfg)
        return {
            "members": rows,
            # Whether members can generate video right now, and if not, why not — the panel shows
            # the reason instead of leaving the user to guess where the tool went.
            "video": {
                "enabled": bool(cfg["video_enabled"]),
                "provider": ({"id": vprov["id"], "name": vprov["name"], "base_url": vprov["base_url"]}
                             if vprov else None),
                "problem": vwhy or (video.blocked_by_offline(vprov, cfg) if vprov else ""),
            },
            "tools": [{"name": t["name"], "description": t["description"], "source": t["source"]} for t in (ctx.specs() if ctx else [])],
            "problems": ctx.problems if ctx else [],
            "mcp_deferred": bool(ctx.mcp_deferred) if ctx else False,
            "ext": group["ext"],
            # Exactly the documents its members can reach through the knowledge bases in scope
            "docs": len(c.library.scope_ids(store.get_group(gid)["ext"]["library"] if store.get_group(gid) else {}, gid)),
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
        """Add a model from "my models" straight into the group as a member (a matching member is
        created automatically, with its name and strengths taken from the model)."""
        _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        model = _need(store.get_model(body.model_id), i18n.pick_now("Model", "模型"))
        if not model["enabled"] or not model["provider_enabled"]:
            raise HTTPException(400, i18n.pick_now("This model, or its provider, is disabled — enable it under Model providers first", "这个模型或它的服务商已停用,先在「模型服务」里启用"))
        if model.get("kind") in video.MEDIA_KINDS:
            raise HTTPException(400, i18n.pick_now(
                "That is a video-generation provider, so it cannot join the group as a member. Members are driven through the generate_video tool instead.",
                "那是视频生成服务商,不能作为成员入群。成员是通过 generate_video 工具使用它的。"))
        agent = _need(store.ensure_model_agent(body.model_id), i18n.pick_now("Model", "模型"))
        store.add_member(gid, agent["id"])
        return group_view(store.get_group(gid))  # type: ignore[arg-type]

    @r.get("/api/templates")
    async def templates() -> list[dict]:
        return template_rows()

    @r.post("/api/templates/{tid}/create-group")
    async def template_create(tid: str, body: TemplateIn) -> dict:
        return _need(create_group_from_template(store, tid, body.name), i18n.pick_now("Template", "模板"))

    # ============================================================ Data / export
    @r.post("/api/data/restore")
    async def data_restore(request: Request) -> dict:
        """The request body is the raw bytes of the backup file (.db). It replaces all current
        data, after automatically keeping a copy of the current data first."""
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
        await c.mcp.shutdown()   # after a restore the MCP config may have changed, so the old connections no longer match
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
                store.clear_obsidian_map()       # the folder changed: the old mapping is no longer valid
                store.set_meta("obsidian_last", "")
        if body.auto is not None:
            patch["obsidian_auto"] = body.auto
        store.update_settings(patch)
        return c.obsidian.status()

    @r.post("/api/obsidian/sync")
    async def obsidian_sync(force: bool = False) -> dict:
        return await asyncio.to_thread(c.obsidian.sync, force)

    # ============================================================ Permissions and control
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
        """The facts shown on the "Permissions and control" page: the mode, the allow list, which
        tools exist right now and their risk levels, and what this app can reach."""
        cfg = store.get_settings()
        tools: list[dict] = []

        def row(spec: dict, group: str) -> None:
            tools.append({"name": spec["name"], "group": group, "risk": risk_of(spec), "risk_label": risk_label(risk_of(spec)),
                          "policy": c.toolhub.policy(spec)})

        # Built-ins are listed from their canonical specs, so the page shows the same risk the
        # dispatcher uses. Handing this loop a hand-built {"name", "source"} dict would make
        # every new built-in look read-only here — on the one page where the user decides.
        for n, spec in builtin_specs().items():
            row({**spec, "name": n, "source": "builtin"}, i18n.pick_now("built-in", "内置"))
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
                # The base the per-group workspaces live under; each group gets <base>/<group id>
                "code_default_dir": str(coderun.base_dir(Path(store.data_dir), cfg)),
            },
        }

    # ============================================================ video generation
    class VideoProbeIn(BaseModel):
        provider_id: str = ""      # empty = the one that would actually be used

    @r.post("/api/video/test")
    async def video_test(body: VideoProbeIn | None = None) -> dict:
        """Is the video server awake? Renders nothing.

        It asks for a task id that cannot exist, so a live server answers 404 — which still proves
        it is up and speaking the video API. Checking this before a member tries to generate
        something costs one request instead of several minutes of GPU time.
        """
        cfg = store.get_settings()
        want = (body.provider_id if body else "") or str(cfg.get("video_provider_id") or "")
        if want:
            prov = next((p for p in video.media_providers(store) if p["id"] == want), None)
            if prov is None:
                raise HTTPException(404, i18n.pick_now("No video provider with that id", "没有这个 id 的视频服务商"))
        else:
            prov, why = video.pick_provider(store, cfg)
            if prov is None:
                return {"ok": False, "provider": None, "detail": why}
        blocked = video.blocked_by_offline(prov, cfg)
        if blocked:
            return {"ok": False, "provider": {"id": prov["id"], "name": prov["name"], "base_url": prov["base_url"]},
                    "detail": blocked}
        ok, detail = await video.probe(prov)
        return {"ok": ok, "provider": {"id": prov["id"], "name": prov["name"], "base_url": prov["base_url"]}, "detail": detail}

    @r.get("/api/groups/{gid}/video/{name}")
    async def group_video(gid: str, name: str) -> Response:
        """Serve a clip a member generated, out of this group's own workspace.

        `name` is treated as a plain filename — a separator, a parent reference or anything but
        `.mp4` is refused before the filesystem is touched, so this cannot become a way to read
        arbitrary files. Only this group's workspace is ever consulted.
        """
        _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.mp4", name):
            raise HTTPException(400, i18n.pick_now("That is not a video file name", "这不是一个视频文件名"))
        # `workspace_path`, not `workspace_dir`: a GET must not create directories as a side effect
        ws = coderun.workspace_path(Path(store.data_dir), store.get_settings(), gid)
        path = (ws / "video" / name).resolve()
        if not path.is_file():
            raise HTTPException(404, i18n.pick_now("That video is not there any more", "这个视频已经不在了"))
        return Response(path.read_bytes(), media_type="video/mp4",
                        headers={"Cache-Control": "private, max-age=3600"})

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
        """Parse an MCP config JSON exported elsewhere. Parsing only: nothing is saved and nothing
        is started; the UI asks you to confirm each server before it is added."""
        try:
            servers, warnings = parse_mcp_json(body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        have = {m["name"] for m in store.list_mcp()}
        return {"servers": [{**s, "exists": s["name"] in have,
                             "env": _mask(s["env"]), "headers": _mask(s["headers"])} for s in servers],
                "warnings": warnings}       # fields holding keys are shown masked; on import the backend re-parses the original, so keys never reach the UI

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
            check_mcp_cfg(s["name"], s["command"], s["url"], s["transport"])   # validate everything before saving anything: an import never fails halfway through
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
        for g in store.list_groups():  # also clear the selection inside the groups
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
        if new.name != name:  # renamed: follow the change on members and on the group selections
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

    # ============================================================ attachments (images)
    @r.post("/api/groups/{gid}/attachments")
    async def attachment_upload(request: Request, gid: str, filename: str = "image.png") -> dict:
        """Raw bytes, like the library upload. The image is checked before it is written."""
        _need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        _need_octet(request)
        data = await request.body()
        problem = images.check(data, store.get_settings())
        if problem:
            raise HTTPException(400, problem)
        found = images.sniff(data)
        assert found is not None                      # `check` already refused anything else
        mime, ext = found
        aid = new_id()
        images.path_for(store.data_dir, aid, ext).write_bytes(data)
        row = store.add_attachment(gid, aid, images.display_name(filename), mime, len(data))
        return {**row, "url": f"/api/attachments/{aid}"}

    @r.get("/api/attachments/{aid}")
    async def attachment_get(aid: str) -> Response:
        row = store.get_attachment(aid)
        f = images.find_file(store.data_dir, aid) if row else None
        if not row or not f:
            raise HTTPException(404, i18n.pick_now("This image is no longer available", "这张图片已经不在了"))
        return Response(f.read_bytes(), media_type=row["mime"],
                        headers={"Cache-Control": "private, max-age=86400"})

    @r.delete("/api/attachments/{aid}")
    async def attachment_delete(aid: str) -> dict:
        row = store.get_attachment(aid)
        if row:
            f = images.find_file(store.data_dir, aid)
            if f:
                f.unlink(missing_ok=True)
            store.delete_attachment(aid)
        return {"ok": True}

    # ============================================================ library
    # "The group's library" is now "the knowledge bases this group can reach"; a document is
    # added into one of them. Nothing here takes a raw document id from the client and trusts it.
    def _check_group(group_id: str) -> None:
        if group_id:
            _need(store.get_group(group_id), i18n.pick_now("Group chat", "群聊"))

    def _check_kb(kb_id: str) -> dict:
        return _need(store.get_kb(kb_id), i18n.pick_now("Knowledge base", "知识库"))

    @r.get("/api/knowledge-bases")
    async def kbs_list(group_id: str | None = None) -> list[dict]:
        """No group_id = every knowledge base (the overview); "" = the shared ones; a group id =
        that workspace's own plus the shared ones, which is what the group may attach."""
        _check_group(group_id or "")
        counts = store.count_by_kb()
        members = store.collection_kb_ids()
        in_collections = {kid: cid for cid, kids in members.items() for kid in kids}
        return [{**k, "docs": counts.get(k["id"], 0), "collection_id": in_collections.get(k["id"], "")}
                for k in store.list_kbs(group_id)]

    @r.post("/api/knowledge-bases")
    async def kbs_add(body: KBIn) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, i18n.pick_now("A knowledge base needs a name", "知识库需要名称"))
        _check_group(body.group_id)
        return store.add_kb(name, body.description.strip(), body.group_id)

    @r.patch("/api/knowledge-bases/{kid}")
    async def kbs_patch(kid: str, body: KBPatch) -> dict:
        _check_kb(kid)
        return store.update_kb(kid, body.model_dump(exclude_unset=True))  # type: ignore[return-value]

    @r.delete("/api/knowledge-bases/{kid}")
    async def kbs_delete(kid: str) -> dict:
        _check_kb(kid)
        doomed = len(store.list_docs(kid))
        store.delete_kb(kid)                       # removes its documents too
        # Groups that had it attached should not keep pointing at something that is gone
        for g in store.list_groups():
            lib = g["ext"]["library"]
            if kid in lib["kb_ids"]:
                store.update_group(g["id"], {"ext": {"library": {**lib, "kb_ids": [x for x in lib["kb_ids"] if x != kid]}}})
        return {"ok": True, "deleted_docs": doomed}

    @r.get("/api/collections")
    async def collections_list() -> list[dict]:
        """Flat lists of knowledge bases. Membership is returned as ids, so the client does not
        have to join anything."""
        members = store.collection_kb_ids()
        counts = store.count_by_kb()
        kbs = {k["id"]: k for k in store.list_kbs()}
        out = []
        for col in store.list_collections():
            ids = [x for x in members.get(col["id"], []) if x in kbs]
            out.append({**col, "kb_ids": ids,
                        "kbs": [{"id": i, "name": kbs[i]["name"], "group_id": kbs[i]["group_id"],
                                 "docs": counts.get(i, 0)} for i in ids]})
        return out

    @r.post("/api/collections")
    async def collections_add(body: CollectionIn) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, i18n.pick_now("A collection needs a name", "合集需要名称"))
        col = store.add_collection(name, body.description.strip())
        if body.kb_ids:
            store.set_collection_kbs(col["id"], body.kb_ids)
        return col

    @r.patch("/api/collections/{cid}")
    async def collections_patch(cid: str, body: CollectionPatch) -> dict:
        _need(store.get_collection(cid), i18n.pick_now("Collection", "合集"))
        col = store.update_collection(cid, body.model_dump(exclude_unset=True))
        if body.kb_ids is not None:
            store.set_collection_kbs(cid, body.kb_ids)
        return col  # type: ignore[return-value]

    @r.delete("/api/collections/{cid}")
    async def collections_delete(cid: str) -> dict:
        _need(store.get_collection(cid), i18n.pick_now("Collection", "合集"))
        store.delete_collection(cid)
        for g in store.list_groups():
            lib = g["ext"]["library"]
            if cid in lib["collection_ids"]:
                store.update_group(g["id"], {"ext": {"library": {
                    **lib, "collection_ids": [x for x in lib["collection_ids"] if x != cid]}}})
        return {"ok": True}

    def _kb_for_new_doc(kb_id: str, group_id: str) -> dict:
        """Where a newly added document goes.

        An explicit knowledge base wins. Otherwise a group's upload lands in that workspace's own
        knowledge base (created on first use) and anything else in the shared one — so a document
        added in a group never ends up somewhere another group can read by accident.
        """
        if kb_id:
            return _check_kb(kb_id)
        if group_id:
            _check_group(group_id)
            kb = c.library.workspace_kb(group_id)
            return _need(kb, i18n.pick_now("Knowledge base", "知识库"))
        return _need(c.library.shared_kb(), i18n.pick_now("Knowledge base", "知识库"))

    @r.get("/api/library")
    async def library_list(kb_id: str | None = None, group_id: str | None = None) -> dict:
        if kb_id:
            _check_kb(kb_id)
            docs = store.list_docs(kb_id)
        elif group_id is not None and group_id != "":
            _check_group(group_id)
            docs = store.list_docs(kb_ids=[k["id"] for k in c.library.visible_kbs(group_id)])
        elif group_id == "":
            docs = store.list_docs(kb_ids=[k["id"] for k in store.list_kbs("")])
        else:
            docs = store.list_docs()
        return {"docs": docs, "total_chars": sum(d["chars"] for d in docs), "count": len(docs)}

    @r.post("/api/library/upload")
    async def library_upload(request: Request, filename: str, kb_id: str = "", group_id: str = "") -> dict:
        """The request body is the raw bytes of the file (no multipart, which saves a dependency)."""
        kb = _kb_for_new_doc(kb_id, group_id)
        _need_octet(request)
        data = await request.body()
        if not data:
            raise HTTPException(400, i18n.pick_now("The file is empty", "文件是空的"))
        try:
            return c.library.add_file(filename, data, kb_id=kb["id"])
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/library/note")
    async def library_note(body: NoteIn) -> dict:
        kb = _kb_for_new_doc(body.kb_id, body.group_id)
        try:
            return c.library.add_text(body.title, body.content, kb_id=kb["id"])
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/library/url")
    async def library_url(body: LibraryUrlIn) -> dict:
        if not store.get_settings()["external_calls_enabled"]:
            raise HTTPException(403, i18n.pick_now("Outbound calls are disabled, so web pages cannot be fetched (you can turn this on under Permissions & control)", "外呼已禁用,不能抓取网页(在「权限与操控」里可以打开)"))
        kb = _kb_for_new_doc(body.kb_id, body.group_id)
        try:
            return await asyncio.to_thread(c.library.add_url, body.url, kb["id"])
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/library/dir")
    async def library_dir(body: LibraryDirIn) -> dict:
        kb = _kb_for_new_doc(body.kb_id, body.group_id)
        try:
            return await asyncio.to_thread(c.library.add_dir, body.path, body.recursive, kb["id"])
        except LibraryError as e:
            raise HTTPException(400, str(e)) from None

    @r.get("/api/library/search")
    async def library_search(q: str, top_k: int = 5, group_id: str | None = None, kb_id: str | None = None) -> list[dict]:
        """With a group id, only what that group may search comes back — the same scope its
        members get, so a preview cannot show more than they can reach."""
        if kb_id:
            scope = [d["id"] for d in store.list_docs(kb_id) if d["enabled"]]
        elif group_id:
            group = _need(store.get_group(group_id), i18n.pick_now("Group chat", "群聊"))
            scope = c.library.scope_ids(group["ext"]["library"], group_id)
        else:
            scope = None
        # the first search on a large library rebuilds the BM25 index, so run it in a thread pool
        # to keep the event loop free
        return await asyncio.to_thread(c.library.search, q, max(1, min(top_k, 20)), scope)

    @r.get("/api/library/{did}")
    async def library_read(did: str, start: int = 0) -> dict:
        try:
            return await asyncio.to_thread(c.library.read, did, max(0, start), 6000)
        except LibraryError as e:
            raise HTTPException(404, str(e)) from None

    @r.patch("/api/library/{did}")
    async def library_patch(did: str, body: DocPatch) -> dict:
        _need(store.get_doc(did), i18n.pick_now("Document", "文档"))
        if body.kb_id is not None:
            _check_kb(body.kb_id)
        return c.library.update(did, body.model_dump(exclude_unset=True))  # type: ignore[return-value]

    @r.delete("/api/library/{did}")
    async def library_delete(did: str) -> dict:
        _need(store.get_doc(did), i18n.pick_now("Document", "文档"))
        c.library.delete(did)
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
        """Show the full system prompt a member actually receives in this group (without the live
        tool and memory parts)."""
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

    # ------------------------------------------------ Local models: discovery / add to recommended
    @r.post("/api/local/check")
    @gh
    async def local_check() -> dict:
        """Look for new open-source models right now (without waiting for the scheduled check).
        The result is also written into the "Updates and discovery" reminders."""
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
        """Add an Ollama model to the recommended list. It first asks the Ollama registry to
        confirm the model exists and reads its size; unknown models are rejected. No weights are
        downloaded."""
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
