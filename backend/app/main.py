"""FastAPI entry point: REST + WebSocket. Listens on the loopback address only."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import platform
import re
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .api_ext import Ctx, build_router
from .api_external import build_external_router
from .api_gallery import build_gallery_router
from .api_video_zone import build_video_zone_router
from .api_zones import build_zones_router
from .api_channels import build_channels
from .api_hooks import build_hooks_router
from .api_import import build_import_router
from .approvals import Approvals
from . import attachments
from .attachments import MAX_PER_MESSAGE as MAX_ATTACHMENTS
from . import channels
from . import coderun
from . import comfyui
from . import embed
from .discovery import DiscoveryError, fetch_model_ids
from .health import HealthBoard
from .hooks import HookManager, ensure_example_hooks
from .library import Library
from .local_models import native_machine
from .mcp_client import McpManager
from . import media
from . import names
from .memory import MemoryService
from . import templates
from . import teamsetup
from . import zones
from . import i18n
from . import images
from . import import_sources
from . import layouts
from . import music
from . import toolcall
from . import studio
from . import voices
from . import net
from .obsidian import ObsidianSync
from .orchestrator import Orchestrator
from . import planner
from . import presets
from .presets import DEFAULT_SETTINGS, PRESETS
from .prompting import PromptBuilder
from .router import ModelRouter, has_credentials
from .stats import compute_stats
from .store import Store
from .toolhub import ToolHub
from .tools import build_registry, ensure_example_skills, merge_builtin_skill_copies, list_skills, skill_names
from .updater import Updater, backfill_notice_languages, drop_stale_check_snapshot


# ------------------------------------------------------------------ schemas
class ProviderIn(BaseModel):
    preset: str | None = None
    name: str | None = None
    kind: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    is_local: bool = False


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    is_local: bool | None = None


class ModelIn(BaseModel):
    model_name: str
    display_name: str | None = None


class ModelPatch(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    strengths: list[str] | None = None   # passing null = go back to inferring automatically


class AgentIn(BaseModel):
    name: str
    avatar: str = "🤖"
    role: str = ""
    prompt: str = ""
    model_id: str | None = None
    skills: list[str] = []
    tags: list[str] = []


class AgentPatch(BaseModel):
    name: str | None = None
    avatar: str | None = None
    role: str | None = None
    prompt: str | None = None
    model_id: str | None = None
    skills: list[str] | None = None
    tags: list[str] | None = None


class GroupIn(BaseModel):
    # ⚠️ `name` 可以为空:空了就由**服务端**从 `task`(用户敲的那句话)里取关键词来命名。
    # 命名规则住在 `names.py` 一处(用户 2026-09-25:「限制 8 个字以内,系统自动使用最核心的
    # 关键词命名」);前端自己算一个名字再传过来,就等于把这条规则抄成了两份。
    name: str = ""
    #: What the user typed. Only read when `name` is empty.
    task: str = ""
    member_ids: list[str] = []
    host_agent_id: str | None = None
    ext: dict | None = None
    prompt: str = ""
    # A directory the user picked for this group; empty = the app manages one under the data dir.
    workspace: str = ""
    # Staged members are materialized only when the user confirms the reviewed lineup.
    lineup: list[dict[str, str]] = []
    host_ref: str = ""


class GroupPatch(BaseModel):
    name: str | None = None
    host_agent_id: str | None = None
    prompt: str | None = None
    ext: dict | None = None
    workspace: str | None = None
    # A group is a project: its own state, and whether it has been filed away. Both are set by hand.
    status: str | None = None
    archived: bool | None = None


class MemberIn(BaseModel):
    agent_id: str


class CapabilityBinding(BaseModel):
    kind: Literal["skills", "plugins", "mcp"]
    ref: str
    attached: bool


class ForwardIn(BaseModel):
    to_group_id: str


class MessageIn(BaseModel):
    text: str
    # Ids returned by POST /api/groups/{gid}/attachments: any kind of file, not only images. A
    # message may carry files with no text at all ("have a look at this"), but not the reverse.
    # `images` is the name this field had when attachments were images only; kept so an older
    # client (and a replay of an older request) still lands somewhere sensible.
    attachments: list[str] = []
    images: list[str] = []


class PullIn(BaseModel):
    model: str


class ModelBatchIn(BaseModel):
    model_names: list[str]


APP_VERSION = "0.5.1"

# A project's own state. Two values, both set by hand ("active" is what every project starts as);
# the labels the user sees are 进行中 / 已完成. Nothing infers "done" from the messages, because "the
# conversation stopped" and "the work is finished" are not the same statement.
GROUP_STATUS = ("active", "done")


def _pkg_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def public_provider(p: dict) -> dict:
    key = p.get("api_key") or ""
    out = {k: v for k, v in p.items() if k != "api_key"}
    out["has_key"] = bool(key)
    out["credentials_ready"] = has_credentials(p)
    out["key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else (i18n.pick_now("set", "已设置") if key else "")
    return out


class Hub:
    """Distributes WebSocket events per group chat."""

    def __init__(self) -> None:
        self.conns: dict[str, set[WebSocket]] = {}

    async def connect(self, gid: str, ws: WebSocket) -> None:
        await ws.accept()
        self.conns.setdefault(gid, set()).add(ws)

    def disconnect(self, gid: str, ws: WebSocket) -> None:
        self.conns.get(gid, set()).discard(ws)

    async def broadcast(self, gid: str, event: dict) -> None:
        for ws in list(self.conns.get(gid, ())):
            try:
                await asyncio.wait_for(ws.send_text(json.dumps(event, ensure_ascii=False)), 5)   # a stuck connection must not slow down the whole conversation
            except Exception:  # noqa: BLE001
                self.disconnect(gid, ws)


# ------------------------------------------------------------------ factory
DEV_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

# How long to wait before paying the slow imports, so the window's opening requests (settings,
# groups, models) are served first. Measured on this machine: `import litellm` takes ~3.1 s, i.e.
# an order of magnitude more than the entire request handling of a normal turn.
WARM_UP_DELAY = 2.0


def _warm_model_stack() -> None:
    """Import the model SDK while the user is still looking at the window.

    `router._default_completion` imports litellm lazily on purpose — importing it at module level
    would add three seconds to every start, including the tests. But that only moves the cost to
    whoever sends the first message of a session, where it is far more annoying. Doing it once,
    in a daemon thread, after the UI has made its opening requests gets both: a fast start and a
    fast first reply.
    """
    time.sleep(WARM_UP_DELAY)
    try:
        import litellm  # noqa: F401
    except Exception as e:  # noqa: BLE001 — a warm-up that fails must not affect anything
        print("model stack warm-up skipped:", e)


def ensure_loopback_no_proxy() -> None:
    """With a proxy such as Clash running (HTTP_PROXY set in the shell), httpx by default sends
    requests to 127.0.0.1 through the proxy as well, so local Ollama, self-hosted services and
    local MCP all return 502. Adding the loopback addresses to NO_PROXY affects this process only."""
    have: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):          # different tools recognize different letter cases, so both spellings are set after merging
        have += [x.strip() for x in os.environ.get(key, "").split(",") if x.strip() and x.strip() not in have]
    merged = ",".join(have + [w for w in ("127.0.0.1", "localhost", "::1") if w not in have])
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged


def _visible_message(m: dict) -> dict:
    """One stored message as the chat should show it.

    ⚠️ The tool protocol is stripped **on the way out**, not only on the way in. A round whose call
    arrived with no tags around it (see `toolcall._bare_spans`) was stored with that JSON in the
    body, and every row already in someone's database was written before the parser learned the
    shape — so cleaning only at write time leaves those bubbles unreadable forever, which is exactly
    what the user was looking at (「成员发表的内容没有格式,看起来很乱」). Nothing is rewritten here:
    this is a view, the raw rows still hold what the member actually sent, and `/export` reads the
    store rather than this function.
    """
    if m.get("sender_type") != "agent" or not m.get("content"):
        return m
    shown = toolcall.strip_hidden(str(m["content"]))
    return m if shown == m["content"] else {**m, "content": shown}


def create_app(
    data_dir: Path | str | None = None,
    completion_fn: Callable[..., Awaitable[Any]] | None = None,
    token: str | None = None,
    github_transport: Any = None,
    background: bool = False,
) -> FastAPI:
    """token: in the desktop build Electron generates it at random and passes it in through the
    TEAM_AGENT_TOKEN environment variable. Once set, every /api and WebSocket request must carry
    it, which stops other pages in the user's browser from reaching the local backend (DNS
    rebinding included). When it is unset (pure browser development mode), only cross-origin
    requests from localhost are allowed. The generated docs (/docs, /redoc, /openapi.json) live
    outside /api, so they are served in that unset mode only."""
    token = token if token is not None else os.environ.get("TEAM_AGENT_TOKEN") or None
    ensure_loopback_no_proxy()
    store = Store(data_dir)
    store.recover_interrupted_plans()
    ensure_example_skills(store.data_dir / "skills", store._flag)
    # Installs that predate the bilingual built-in skills have every one of them twice — the seed
    # marker was keyed by the skill's name, and the name changed from Chinese to English when the
    # text became language-neutral, so the set was written a second time. Both copies show the same
    # title and the same description, and a member could be given the same rule twice. Repairing it
    # here means it costs the user nothing: no reinstall, no manual delete, no list to prune.
    merge_builtin_skill_copies(store)
    # Skills imported before the importer knew a skill can be a folder arrived as their SKILL.md
    # alone, so anything they said to read or run pointed at nothing. One pass, once per skill, adds
    # only the files that are missing.
    import_sources.restore_imported_skill_files(store)
    # Reminders used to be written in whichever language the check happened to run in. This gives
    # the ones already on disk their English form, so an English interface stops showing Chinese
    # ones immediately rather than waiting for the next check to rewrite them. Called by name
    # rather than as `updater.…`: `updater` is a local variable further down this function.
    backfill_notice_languages(store)
    drop_stale_check_snapshot(store)
    # Every group has a workspace. Groups made before this existed get theirs now, so a
    # member asked to "put it in the workspace" always has somewhere to put it. Groups whose
    # workspace the user picked themselves are skipped — see `coderun.ensure_workspaces`.
    coderun.ensure_workspaces(store.data_dir, store.get_settings(),
                              [(g["id"], g.get("workspace") or "") for g in store.list_groups()])
    # …and the process engineer, in every group. A watcher that has to be added by hand to each new
    # group is a watcher that is missing from the groups where something went wrong; the setting is
    # what makes it optional, and the groups that already have it are left alone.
    templates.keep_process_engineer(store)
    # …and the names that an older build built out of the first fourteen characters of the task
    # ("我需要做一个未破裂颅内动脉瘤" — the keyword is in there, the prefix is not the point). Only
    # names longer than the cap are touched, and only by the one rule in `names.py`. Changed names
    # are printed: rewriting something the user reads every day is not a silent operation.
    for _old, _new in names.shorten_stored(store):
        print(f"shortened a project name: {_old!r} -> {_new!r}")
    router = ModelRouter(store, completion_fn)
    registry = build_registry(store.data_dir / "plugins")
    mcp = McpManager()
    library = Library(store)
    # The user's own ComfyUI graphs, one JSON per workflow. Read here rather than at first render
    # so that a file which cannot be parsed is a startup fact rather than a surprise mid-job — and
    # so the probe and the settings dropdown both already know the names.
    comfyui.use_folder(store.data_dir / "workflows")
    # And the look of an assembled film, one JSON per layout. Read at startup for the same reason:
    # a file that cannot be parsed is a startup fact, not a surprise halfway through a render.
    layouts.use_folder(store.data_dir / "layouts")
    # Cloned voices: a folder per voice holding a reference recording and its transcript. Read at
    # startup so a `voice:<name>` in a film's settings is checked long before the film is timed.
    voices.use_folder(store.data_dir / "voices")
    # The music shelf: audio files a film can be scored with, each with an optional sidecar saying
    # what it is. Read at startup for the same reason as the other three — a broken sidecar is then
    # a fact you can see, instead of a track that silently carries no metadata.
    music.use_folder(store.data_dir / music.FOLDER_NAME)
    # The private studio: the user's own footage, and every take generated from it. Read at startup
    # like the rest — but ⚠️ this shelf keeps to itself: it is not in any group's workspace and not
    # in a knowledge base, because the material is the user's own face and their own recordings.
    studio.use_folder(store.data_dir / studio.FOLDER_NAME)
    # The zones the user writes themselves: one JSON per zone, for a zone that needs no new engine.
    # Read at startup like the other four, and for the same reason — a file that cannot be parsed is
    # a fact you can see, rather than a zone that silently is not in the sidebar.
    zones.use_folder(store.data_dir / "zones")
    memory = MemoryService(store, router)
    # Hooks: the user's own code at six fixed points. Written once by the example below, off
    # until switched on, and every run goes through a subprocess (see app/hooks.py).
    ensure_example_hooks(store.data_dir)
    hooks = HookManager(store.data_dir)
    hooks.load()
    toolhub = ToolHub(store, registry, mcp, library, memory, hooks=hooks, router=router)
    board = HealthBoard(store, router)
    prompts = PromptBuilder(store, router)
    approvals = Approvals(store)
    obsidian = ObsidianSync(store)
    orch = Orchestrator(store, router, prompts=prompts, toolhub=toolhub, memory=memory, library=library,
                        registry=registry, mcp=mcp, approvals=approvals, hooks=hooks)
    updater = Updater(store, APP_VERSION, github_transport)
    hub = Hub()
    # Chat channels: inbound webhooks, polled platforms, and the one-way pushes. Built here
    # because the middleware below needs to know which public hostnames to accept, and the
    # orchestrator needs somewhere to hand each finished answer.
    chan = build_channels(store, orch, hub)
    orch.on_answer = chan.push_answer
    tasks = orch.tasks

    def busy(gid: str) -> bool:
        return any(not t.done() for t in tasks.get(gid, ()))

    def require_idle(gid: str) -> None:
        if busy(gid):
            raise HTTPException(409, i18n.pick_now("This project is working — stop it before changing its workspace or members, or clearing it.",
                                                  "项目正在工作,请先停止再修改工作空间、成员或清空项目。"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        async def obsidian_loop() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    cfg = store.get_settings()
                    if cfg["obsidian_auto"] and cfg["obsidian_dir"]:
                        await asyncio.to_thread(obsidian.sync)
                except Exception as e:  # noqa: BLE001 — one error must not stop the automatic sync forever
                    print("obsidian auto-sync error:", e)

        bg = [asyncio.create_task(updater.run_forever()), asyncio.create_task(obsidian_loop())] if background else []
        if background:
            # Uploads the user picked but never sent would otherwise sit in the data directory
            # for good; images a message references are kept (see store.stale_attachments)
            try:
                images.sweep(store, store.data_dir)
            except Exception as e:  # noqa: BLE001 — housekeeping must never stop startup
                print("attachment sweep failed:", e)
            threading.Thread(target=_warm_model_stack, daemon=True).start()
            # A polled channel (Telegram) fetches its own messages, so it has to be running
            # for the app to receive anything. Off unless a channel is both enabled and
            # complete, which is why this is safe to arm unconditionally here.
            await chan.startup()
        try:
            yield
        finally:
            for t in bg:
                t.cancel()
            if background:
                await chan.shutdown()
            active = [t for group_tasks in tasks.values() for t in group_tasks if not t.done()]
            for t in active:
                t.cancel()
            await asyncio.gather(*active, *bg, return_exceptions=True)
            await orch.drain()
            await hooks.drain()      # let a running observer finish rather than cutting it off
            await mcp.shutdown()
            # Stop the servers this app started itself, and do it here rather than anywhere else
            # because this is the only place that runs on every exit (Electron's `before-quit` sends
            # SIGTERM, uvicorn runs the lifespan on the way down).
            #
            # ⚠️ Both are launched with `start_new_session=True` — that is what lets a startup timeout
            # kill their whole tree, and it is also why nothing else would ever stop them: as session
            # leaders they do not receive the signal that ends this process. Left running, ComfyUI
            # keeps a loaded diffusion model in RAM and the embedding server keeps its model and its
            # port, until the machine is rebooted. Measured on this project: quit the app and ComfyUI
            # was still answering on 8188.
            #
            # ⚠️ Order matters only in that both must happen; each stops **only** what this process
            # started, so a server the user launched themselves is left alone (`embed.stop`,
            # `LocalRuntime.stop` both say so where they are defined).
            toolhub.comfy_runtime.stop()
            embed.stop()

    # The interactive docs live outside /api, so `require_token` never covers them: with a token in
    # use, any local page could still read the whole API surface from /openapi.json. They are served
    # in dev mode only (no token at all), where the API is open anyway.
    expose_docs = not token
    app = FastAPI(title="Team Agent", lifespan=lifespan,
                  docs_url="/docs" if expose_docs else None,
                  redoc_url="/redoc" if expose_docs else None,
                  openapi_url="/openapi.json" if expose_docs else None)
    # Resolves the request language (?lang= or Accept-Language) for built-in content.
    app.add_middleware(i18n.LanguageMiddleware)
    # The API is loopback-only on purpose. The exception is a chat channel that needs a
    # public address: a platform posts to whatever tunnel or VPS sits in front of this app,
    # so those hostnames have to be accepted here. They are the only paths reachable from
    # outside, and each authenticates every request with the platform's own signature
    # rather than with a token, so allowing the hosts does not widen the API surface.
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost"] + channels.public_hosts(store.get_settings()))
    if token:  # the Electron renderer's Origin may be file:// (i.e. "null"), so authenticate by token
# rather than by origin
        app.add_middleware(
            CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
            expose_headers=["Content-Disposition"],  # let the front end read the backup file name
        )
    else:
        app.add_middleware(
            CORSMiddleware, allow_origin_regex=DEV_ORIGIN_RE, allow_methods=["*"], allow_headers=["*"],
            expose_headers=["Content-Disposition"],
        )

    def token_ok(candidate: str | None) -> bool:
        return not token or (candidate is not None and hmac.compare_digest(candidate, token))

    @app.middleware("http")
    async def require_token(request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        origin = request.headers.get("origin")
        # CORS hides responses; it does not prevent a simple cross-origin POST.
        if not token and request.method != "OPTIONS" and path.startswith("/api") and origin is not None and not re.fullmatch(DEV_ORIGIN_RE, origin):
            return JSONResponse({"detail": "untrusted origin"}, status_code=403)
        if (
            token
            and request.method != "OPTIONS"
            and path.startswith("/api")
            and path != "/api/health"
            and not token_ok(request.headers.get("x-team-agent-token"))
        ):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)
    app.state.store, app.state.router, app.state.orch = store, router, orch
    app.state.updater, app.state.mcp, app.state.registry = updater, mcp, registry
    app.state.hooks = hooks

    def need(x: Any, what: str) -> Any:
        if x is None:
            raise HTTPException(404, i18n.pick_now(f"{what} not found", f"{what}不存在"))
        return x

    def files_for(gid: str, ids: list[str]) -> list[dict]:
        """Descriptors for the attachment ids a message references.

        An id belonging to another group, or one whose file has gone, is dropped rather than
        trusted: the client is not the authority on what a file is or where it came from.
        """
        out: list[dict] = []
        for aid in list(dict.fromkeys(ids))[:MAX_ATTACHMENTS]:        # a message carries a limited number
            row = store.get_attachment(aid)
            if not row or row["group_id"] != gid or not attachments.path_for_row(store, row):
                continue
            out.append({"id": aid, "name": row["name"], "mime": row["mime"], "bytes": row["bytes"],
                        "kind": row["kind"] or "image"})
        return out

    # -------------------------------------------------------------- misc
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/presets")
    async def preset_catalog() -> list[dict]:
        # Not named `presets`: a route function of that name shadows the `presets` module for the
        # whole of `create_app`, and the next `presets.localize_provider(...)` in here would then
        # be calling this function instead. (It was, until this rename.)
        return i18n.localize(PRESETS)

    def public_settings() -> dict:
        s = store.get_settings()
        for k in store.SECRET_SETTINGS:      # secrets can be written but never read back
            s[k + "_set"] = bool(s.get(k))
            s[k] = ""
        return s

    ENUMS = {"plan_mode": ("auto", "on", "off"), "perm_mode": ("ask_risky", "ask_all", "allow_all")}
    RANGES = {"tool_rounds": (0, 10), "tool_timeout": (5, 600), "plan_max_tasks": (2, 12), "memory_top_k": (0, 20),
              "library_top_k": (1, 10), "max_hops": (1, 30), "history_limit": (1, 200), "history_clip": (200, 20000), "tool_output_limit": (500, 50000), "update_interval_hours": (1, 168),
              "perm_timeout": (10, 600), "request_timeout": (5, 600), "circuit_threshold": (1, 10), "circuit_cooldown": (5, 600),
              "code_timeout": (5, 600), "vision_max_mb": (1, 64),
              # files: an upload may be a video, and a referenced folder can be worth a lot of text
              "upload_max_mb": (1, 1024), "video_frames": (1, 12), "refs_budget": (1000, 200000),
              "integration_budget": (2000, 200000),
              # video: the cap is the group's, not any one model's — H3 stops at 15s and Seedance 2.5
              # runs to 30, and `video.clamp_seconds` narrows each request to its own provider.
              "video_short_edge": (128, 2048), "video_max_seconds": (1, 30), "video_timeout": (30, 7200), "music_timeout": (60, 7200), "video_max_mb": (1, 4096),
              # assembling: local ffmpeg work, so the floor is a real render and the ceiling is an
              # unusually long film rather than a service's patience
              "assemble_timeout": (60, 7200),
              # asking an outside model: a narrow question takes minutes, a real review of a run
              # takes longer, and the ceiling is a person's patience rather than a service's
              "advisor_timeout": (60, 3600),
              # handing the findings to a coding agent: it is editing files, so the ceiling is
              # "how long may a repair run before it is not a repair any more"
              "handoff_timeout": (60, 7200),
              # image: a generation is seconds rather than minutes, and a 4K PNG is tens of MB
              "image_timeout": (20, 900), "image_max_mb": (1, 128),
              # scoring: the threshold is a percentage, and the excerpt bounds what a judge reads
              "score_threshold": (0, 100), "score_excerpt_chars": (100, 4000), "score_max_lessons": (0, 3),
              # indexing a knowledge base: 64 texts per request is the local server's own ceiling,
              # and a batch on a cold CPU model can take minutes rather than seconds
              "embed_batch": (1, 64), "embed_timeout": (10, 3600),
              # chat channels: each numeric field declares its own bounds in channels/spec.py,
              # so a new channel's limit cannot be forgotten here (which shows up as a 400 when
              # the settings page tries to save it).
              **channels.ranges()}
    # obsidian_dir can only be set through /api/obsidian (which validates the path); it is not
# accepted here
    READONLY = {"obsidian_dir"}

    @app.get("/api/settings")
    async def get_settings() -> dict:
        return public_settings()

    @app.put("/api/settings")
    async def put_settings(patch: dict[str, Any]) -> dict:
        clean: dict[str, Any] = {}
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS or k in READONLY:
                continue
            if k in ENUMS and v not in ENUMS[k]:
                raise HTTPException(400, i18n.pick_now(f"{k} has an invalid value", f"{k} 的取值不合法"))
            if k in RANGES:
                lo, hi = RANGES[k]
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    raise HTTPException(400, i18n.pick_now(f"{k} must be an integer between {lo} and {hi}", f"{k} 需要是 {lo}~{hi} 的整数"))
            if k in ("perm_allow", "perm_deny"):
                if not (isinstance(v, list) and len(v) <= 500 and all(isinstance(x, str) and 0 < len(x) <= 200 for x in v)):
                    raise HTTPException(400, i18n.pick_now(f"{k} must be a list of tool names", f"{k} 需要是工具名列表"))
                v = list(dict.fromkeys(v))
            if k == "github_token" and v is None:
                continue
            if k not in ENUMS and k not in RANGES and k not in ("perm_allow", "perm_deny"):
                # the remaining settings are type-checked against their defaults: this stops "false" counting
# as true and null/strings from wrecking routing or timeouts
                d = DEFAULT_SETTINGS[k]
                if isinstance(d, bool):
                    good = isinstance(v, bool)
                elif isinstance(d, list):
                    good = isinstance(v, list) and all(isinstance(x, str) for x in v) and len(v) <= 200
                else:
                    good = isinstance(v, str)
                if not good:
                    raise HTTPException(400, i18n.pick_now(f"{k} has the wrong type", f"{k} 的类型不对"))
            if k in ("app_repo",) and v and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", v.strip()):
                raise HTTPException(400, i18n.pick_now("The app repository must be in owner/repo form", "程序仓库格式应为 owner/repo"))
            clean[k] = v.strip() if isinstance(v, str) and k in ("app_repo", "catalog_url", "github_token") else v
        store.update_settings(clean)
        router.reset_circuit()
        return public_settings()

    @app.get("/api/route/preview")
    async def route_preview(preferred: str | None = None) -> dict:
        cands, skipped = router.build_chain(preferred)
        return {
            "external_calls_enabled": store.get_settings()["external_calls_enabled"],
            "chain": [c["id"] for c in cands],
            "skipped": [a.to_dict() for a in skipped],
            # Models that *could* be called but that the chain never mentions. Reported so an empty
            # chain can be explained honestly: "you have 90 usable models and none of them is in
            # the chain" is a fixable sentence, while "no model is available" is not.
            "unchained": [m["id"] for m in router.unchained_usable()],
        }

    # --------------------------------------------------------- providers
    @app.get("/api/providers")
    async def providers() -> list[dict]:
        models = store.list_models()
        # The generators, as a *separate* key. They stay out of `models` because that list is what a
        # member can be pointed at as a conversational model, and moving them in would re-open the
        # bug this split exists to prevent (a member asked to chat with a video model). The member
        # adder reads `media_models` for its own section; nothing else has a use for it.
        media_models = {m["id"]: m for m in store.list_media_models()}
        lang = i18n.current()
        out = []
        for p in store.list_providers():
            # The built-in name is shown in the request language; a name the user typed is theirs.
            pp = presets.localize_provider(public_provider(p), lang)
            # The rows carry their provider's name too, so the same rule applies to them.
            def _named(m: dict) -> dict:
                return {**m, "provider_name": presets.provider_name_view(p["id"], m.get("provider_name") or "", lang),
                        "setup_required": p["kind"] == "comfyui" and m["model_name"] in comfyui.SETUP_WORKFLOWS}
            pp["models"] = [_named(m) for m in models if m["provider_id"] == p["id"]]
            pp["media_models"] = [_named(m) for m in media_models.values() if m["provider_id"] == p["id"]]
            out.append(pp)
        return out

    @app.post("/api/providers")
    async def add_provider(body: ProviderIn) -> dict:
        if body.preset:
            from .presets import PRESET_BY_ID

            if body.preset not in PRESET_BY_ID:
                raise HTTPException(400, i18n.pick_now("Unknown preset", "未知预设"))
            p = store.add_provider_from_preset(body.preset, body.api_key)
        else:
            if not body.name:
                raise HTTPException(400, i18n.pick_now("A name is required", "请填写名称"))
            p = store.add_provider(body.name, body.kind, body.base_url, body.api_key, body.is_local)
        pp = presets.localize_provider(public_provider(p), i18n.current())
        pp["models"] = [m for m in store.list_models() if m["provider_id"] == p["id"]]
        return pp

    @app.patch("/api/providers/{pid}")
    async def patch_provider(pid: str, body: ProviderPatch) -> dict:
        need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        p = store.update_provider(pid, body.model_dump(exclude_unset=True))
        router.reset_circuit()
        return presets.localize_provider(public_provider(p), i18n.current())  # type: ignore[arg-type]

    @app.delete("/api/providers/{pid}")
    async def del_provider(pid: str) -> dict:
        need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        store.delete_provider(pid)
        return {"ok": True}

    @app.post("/api/providers/{pid}/models")
    async def add_model(pid: str, body: ModelIn) -> dict:
        need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        name = body.model_name.strip()
        if not name:
            raise HTTPException(400, i18n.pick_now("A model ID is required", "请填写模型 ID"))
        return store.add_model(pid, name, body.display_name)

    @app.post("/api/providers/{pid}/models/batch")
    async def add_models_batch(pid: str, body: ModelBatchIn) -> list[dict]:
        need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        names = [n.strip() for n in body.model_names if n and n.strip()]
        return [store.add_model(pid, n) for n in dict.fromkeys(names)]

    @app.post("/api/providers/{pid}/fetch-models")
    async def fetch_models(pid: str) -> dict:
        """Ask the provider for its list of available models. When outbound calls are disabled, only
local providers may be queried."""
        p = need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        if not p["is_local"] and not store.get_settings()["external_calls_enabled"]:
            raise HTTPException(403, i18n.pick_now("Outbound calls are disabled, so the cloud provider's model list cannot be fetched", "外呼已禁用,无法向云端服务商查询模型列表"))
        try:
            ids = await fetch_model_ids(p, timeout=store.get_settings().get("request_timeout", 60))
        except DiscoveryError as e:
            raise HTTPException(502, str(e)) from None
        have = {m["model_name"] for m in store.list_models() if m["provider_id"] == pid}
        return {"models": [{"id": i, "added": i in have} for i in ids]}

    @app.get("/api/models")
    async def models() -> list[dict]:
        # Each row carries its provider's name for display, so the same rule applies here.
        lang = i18n.current()
        return [{**m, "provider_name": presets.provider_name_view(m["provider_id"], m.get("provider_name") or "", lang)}
                for m in store.list_models()]

    @app.patch("/api/models/{model_id:path}")
    async def patch_model(model_id: str, body: ModelPatch) -> dict:
        need(store.get_model(model_id), i18n.pick_now("Model", "模型"))
        return store.update_model(model_id, body.model_dump(exclude_unset=True))  # type: ignore[return-value]

    @app.delete("/api/models/{model_id:path}")
    async def del_model(model_id: str) -> dict:
        need(store.get_model(model_id), i18n.pick_now("Model", "模型"))
        store.delete_model(model_id)
        return {"ok": True}

    @app.get("/api/models-health")
    async def models_health() -> dict:
        """Indicator state of every model (reads the database only, sends no requests; probing local
services is triggered by POST)."""
        return {"health": board.view()}

    @app.post("/api/models-health/check")
    async def models_health_check(body: dict[str, Any] | None = None) -> dict:
        """Check connectivity. Leaving body.model_ids empty = all of them; body.cloud=false probes
        only local services (costs no tokens). Cloud models get one very short request, so this
        only happens when the user presses "check"."""
        body = body or {}
        ids = body.get("model_ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            raise HTTPException(400, i18n.pick_now("model_ids must be a list of strings", "model_ids 需要是字符串列表"))
        return await board.check(ids, cloud=bool(body.get("cloud", True)))

    @app.post("/api/test-model")
    async def test_model(body: dict[str, str]) -> dict:
        return await router.test_model(body["model_id"])

    # ------------------------------------------------- stats / system / data
    @app.get("/api/stats")
    async def stats(days: int = 14) -> dict:
        return compute_stats(store, days)

    @app.get("/api/system")
    async def system() -> dict:
        db = store.data_dir / "team-agent.db"
        return {
            "app_version": APP_VERSION,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {native_machine(platform.system(), platform.machine())[0]}",
            "litellm": _pkg_version("litellm"),
            "fastapi": _pkg_version("fastapi"),
            "data_dir": str(store.data_dir),
            # in WAL mode there is also a -wal file; leaving it out makes the "database size" shown in
# the UI too small
            "db_bytes": sum(f.stat().st_size for f in (db, Path(str(db) + "-wal")) if f.exists()),
            "external_calls_enabled": store.get_settings()["external_calls_enabled"],
            "key_secret_backend": store.secret_backend(),   # keychain = the key is in the system keychain; plaintext = fell back to plaintext
            "auth": bool(token),
        }

    @app.get("/api/data/export")
    async def data_export(bg: BackgroundTasks, include_keys: bool = False) -> FileResponse:
        """Export a database snapshot. API keys are excluded by default, so casually forwarding the
backup file cannot leak secrets."""
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # `backup_to` holds the store's global lock while it copies and VACUUMs. Run on the event loop
        # thread that freezes every group chat, WebSocket broadcast and heartbeat for its duration.
        await asyncio.to_thread(store.backup_to, tmp, include_keys=include_keys)
        bg.add_task(os.unlink, tmp)
        name = time.strftime("team-agent-backup-%Y%m%d-%H%M%S.db")
        return FileResponse(tmp, media_type="application/octet-stream", filename=name)

    @app.delete("/api/data/messages")
    async def data_clear_messages() -> dict:
        for gid in tasks:
            require_idle(gid)
        return {"deleted": store.clear_all_messages()}

    # ------------------------------------------------------ local (Ollama)
    def local_base() -> str:
        for p in store.list_providers():
            if p["kind"] == "ollama":
                return p["base_url"] or "http://127.0.0.1:11434"
        return "http://127.0.0.1:11434"

    @app.get("/api/local/status")
    async def local_status() -> dict:
        try:
            async with net.client(timeout=2.5, trust_env=False) as c:
                r = await c.get(local_base() + "/api/tags")
                r.raise_for_status()
                names = [m["name"] for m in r.json().get("models", [])]
            return {"running": True, "base_url": local_base(), "installed": names}
        except Exception as e:  # noqa: BLE001
            return {"running": False, "base_url": local_base(), "installed": [], "error": str(e)[:200]}

    @app.get("/api/local/catalog")
    async def local_catalog() -> dict:
        """Recommended local models (grouped by vendor) + this machine's hardware + a rough "will it
run" assessment per model (a rule-of-thumb estimate, not a guarantee)."""
        st = await local_status()
        return {**store.local_catalog.view(set(st["installed"])), "running": st["running"], "installed": st["installed"]}

    @app.post("/api/local/pull")
    async def local_pull(body: PullIn) -> StreamingResponse:
        """Proxy Ollama's /api/pull, returning download progress line by line (NDJSON)."""

        async def gen():
            ok = False
            try:
                async with net.client(timeout=None, trust_env=False) as c:
                    async with c.stream(
                        "POST", local_base() + "/api/pull", json={"model": body.model, "stream": True}
                    ) as r:
                        async for line in r.aiter_lines():
                            if not line.strip():
                                continue
                            yield line + "\n"
                            try:
                                if json.loads(line).get("status") == "success":
                                    ok = True
                            except ValueError:
                                pass
            except Exception as e:  # noqa: BLE001
                yield json.dumps({"error": i18n.pick_now(f"Cannot reach Ollama: {e}", f"无法连接 Ollama: {e}")}, ensure_ascii=False) + "\n"
                return
            if ok:
                prov = next((p for p in store.list_providers() if p["kind"] == "ollama"), None)
                if prov:
                    store.add_model(prov["id"], body.model)

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # ------------------------------------------------------------ agents
    @app.get("/api/agents")
    async def agents() -> list[dict]:
        # Built-in members are shown in the request language (an install seeded before
        # the names were translated still holds the Chinese spelling in the database — see
        # presets.localize_agent). Anything the user renamed or rewrote is untouched.
        return templates.member_view(store.list_agents())

    def check_agent_name(name: str, exclude_id: str | None = None) -> None:
        if not name or not name.strip():
            raise HTTPException(400, i18n.pick_now("A member name is required", "请填写成员名字"))
        if len(name) > 30 or any(ch in name for ch in " @\n\t"):
            raise HTTPException(400, i18n.pick_now("A name cannot contain spaces or @, and is at most 30 characters long", "名字不能包含空格或 @,最长 30 字"))
        if any(a["name"] == name and a["id"] != exclude_id for a in store.list_agents()):
            raise HTTPException(409, i18n.pick_now("A member with this name already exists", "已有同名成员"))

    @app.post("/api/agents")
    async def create_agent(body: AgentIn) -> dict:
        check_agent_name(body.name)
        return store.create_agent(**body.model_dump())

    @app.patch("/api/agents/{aid}")
    async def patch_agent(aid: str, body: AgentPatch) -> dict:
        cur = need(store.get_agent(aid), i18n.pick_now("Member", "成员"))
        patch = body.model_dump(exclude_unset=True)
        if "name" in patch:
            check_agent_name(patch["name"], aid)
        if cur.get("engine") and patch.get("model_id"):
            raise HTTPException(400, i18n.pick_now("External agents do not use model routing, so they cannot take a model", "外部智能体不使用模型路由,不能指定模型"))
        if cur.get("origin") in ("model", "media") and "model_id" in patch and patch["model_id"] != cur["model_id"]:
            raise HTTPException(400, i18n.pick_now("A model member *is* that model, so its model cannot be changed; to change it, remove the member from the group and add a different model", "模型成员就是这个模型本身,不能换模型;想换的话把它移出群、再拉入另一个模型"))
        return store.update_agent(aid, patch)  # type: ignore[return-value]

    @app.delete("/api/agents/{aid}")
    async def del_agent(aid: str) -> dict:
        need(store.get_agent(aid), i18n.pick_now("Member", "成员"))
        store.delete_agent(aid)
        return {"ok": True}

    # ------------------------------------------------------------ groups
    def check_workspace(raw: str) -> str:
        """Validate a directory the user picked, and return it in the form that gets stored.

        "" means "the app manages one", which is what a group had before this was choosable. A
        path that is there but unusable is refused *before* the group is made rather than stored
        and failed on later, when the reason is harder to see.
        """
        s = str(raw or "").strip()
        if not s:
            return ""
        p = coderun.normalise_workspace(s)
        assert p is not None
        why = coderun.check_workspace(p)
        if why:
            raise HTTPException(400, why)
        return str(p)

    def group_out(g: dict | None) -> dict | None:
        """A group plus where its workspace actually is — and what it is doing.

        The stored value is what the user picked, which is usually empty — "the app manages one".
        A page that showed that empty value would look like the group has no workspace at all, so
        the resolved directory is sent alongside it.

        `folder` is that directory as the sidebar shows it: the **shortest name that identifies it**,
        which is the folder's own name when the user chose it and the *group's* name when the app
        manages it — an app-managed folder is named after the group's id, and `a54f9f874a82` tells
        nobody anything. `mine` says whether the user picked it, because that is exactly what decides
        whether the folder may be renamed (see `/folder/rename`).

        `task` is the newest task board's headline, read here rather than per project by the client:
        the sidebar asks this about every project at once, and one extra query per row would be one
        round trip per row.
        """
        v = templates.group_view(g)
        if not v:
            return v
        try:
            path = store.workspace_path(v["id"])
            v["workspace_path"] = str(path)
            v["folder"] = {
                "name": path.name if str(v.get("workspace") or "").strip() else str(v.get("name") or path.name),
                "path": str(path),
                "mine": bool(str(v.get("workspace") or "").strip()),
            }
        except (OSError, ValueError):
            v["workspace_path"] = ""
            v["folder"] = {"name": "", "path": "", "mine": False}
        # ⚠️ 没有任务板时**不能写「还没有任务」**:用户 2026-09-25 报的就是这个 —— 一个跑过一轮、
        # 出过图出过 30 秒视频的项目,那一行写着「还没有任务」。退回来说「它最近做了什么」。
        # (没有任务板的原因可能是那一轮被 `@别人` 点名了,也可能是老版本跑出来的接力轮。)
        v["task"] = planner.headline(store.latest_plan(v["id"])) or \
            planner.said_headline(store.latest_said(v["id"]))
        return v

    @app.get("/api/groups")
    async def groups() -> list[dict]:
        # `busy` rides along with every row. Whether a turn is running is something this process
        # knows and the database does not, and the sidebar shows it live — a stored flag would be a
        # stale copy of a fact that changes by the second. One request, so the panel can show
        # "which project is working right now" without asking once per project.
        return [{**group_out(g), "busy": busy(g["id"])} for g in store.list_groups()]  # type: ignore[misc]

    @app.post("/api/groups")
    async def create_group(body: GroupIn) -> dict:
        workspace = check_workspace(body.workspace)
        if body.lineup:
            if body.member_ids or body.host_agent_id:
                raise HTTPException(400, "Use either lineup or member_ids, not both")
            try:
                teamsetup.validate(store, router, body.lineup, body.host_ref)
                actual = teamsetup.materialize(store, body.lineup)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            body.member_ids = list(dict.fromkeys(actual.values()))
            body.host_agent_id = actual[body.host_ref]
        known = {a["id"] for a in store.list_agents()}
        if any(i not in known for i in [*body.member_ids, *([body.host_agent_id] if body.host_agent_id else [])]):
            raise HTTPException(400, i18n.pick_now("A member or the host does not exist", "成员或群主不存在"))
        check_host(body.host_agent_id)
        if body.host_agent_id and body.host_agent_id not in body.member_ids:
            raise HTTPException(400, i18n.pick_now("The host must be a member of this group", "群主必须是本群成员"))
        name = (body.name or "").strip() or names.short_name(body.task) or i18n.pick_now(
            "New project", "新项目")
        created = store.create_group(name, body.host_agent_id, body.member_ids, body.ext, body.prompt,
                                     workspace)
        # The workspace is made with the group, not when someone first runs code: a group that has
        # a workspace only sometimes is a group where "put the file in your workspace" is a
        # promise the app cannot keep.
        try:
            store.workspace_dir(created["id"])
        except (OSError, ValueError) as e:  # noqa: BLE001 — a group without a folder is still usable
            print("could not create the group workspace:", e)
        # The process engineer joins after the user's own members, so the chair is never the watcher.
        templates.keep_process_engineer(store, created["id"])
        return group_out(created)

    def check_host(host_id: str | None) -> None:
        host = store.get_agent(host_id) if host_id else None
        if host_id and not host:
            raise HTTPException(400, i18n.pick_now("The host does not exist", "群主不存在"))
        # Who may take the chair is decided in one place (`media.may_host`), and this asks it. The
        # two refusals used to be spelled out here as well, which is how a generator got a different
        # answer from the API than from the orchestrator's own host-picking.
        if host and not media.may_host(host):
            if host.get("engine"):
                raise HTTPException(400, i18n.pick_now(
                    "An external agent cannot host a group (the host splits the work, so it has to be a model member)",
                    "外部智能体不能当群主(群主负责分工,需要是模型成员)"))
            raise HTTPException(400, i18n.pick_now(
                "A media generator cannot host a group; choose a conversational member.",
                "绘画或视频生成成员不能当群主,请选择对话成员。"))

    @app.patch("/api/groups/{gid}")
    async def patch_group(gid: str, body: GroupPatch) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        if body.host_agent_id:
            check_host(body.host_agent_id)
            if body.host_agent_id not in store.member_ids(gid):
                raise HTTPException(400, i18n.pick_now("The host must be a member of this group", "群主必须是本群成员"))
        patch = body.model_dump(exclude_unset=True)
        if any(k in patch for k in ("workspace", "host_agent_id", "ext")):
            require_idle(gid)
        if patch.get("status") is not None and patch["status"] not in GROUP_STATUS:
            raise HTTPException(400, i18n.pick_now(
                f"A project is either {GROUP_STATUS[0]} or {GROUP_STATUS[1]}, not \"{patch['status']}\"",
                f"项目状态只能是 active(进行中)或 done(已完成),不是「{patch['status']}」"))
        if patch.get("archived"):
            # Same rule WorkBuddy uses for tasks ("任务进行中，无法归档"): a project that is working
            # right now cannot be filed away, because archiving hides it and its members are still
            # writing into it. Saying so is the whole point — silently refusing would look broken.
            if any(not t.done() for t in tasks.get(gid, ())):
                raise HTTPException(409, i18n.pick_now(
                    "This project is working right now — stop it before archiving.",
                    "这个项目正在工作,先让它停下来再归档。"))
        if patch.get("workspace") is not None:
            # Validated here rather than in the store: the store writes what it is given, and this
            # is the one place that knows the difference between "no value sent" and "cleared".
            patch["workspace"] = check_workspace(patch["workspace"])
        updated = store.update_group(gid, patch)
        if patch.get("workspace"):
            try:
                store.workspace_dir(gid)
            except (OSError, ValueError) as e:  # noqa: BLE001
                print("could not create the group workspace:", e)
        return group_out(updated)

    @app.patch("/api/groups/{gid}/capability-binding")
    async def bind_capability(gid: str, body: CapabilityBinding) -> dict:
        group = need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        require_idle(gid)
        # No await between reading and updating: concurrent UI clicks cannot replace a stale
        # whole list. Attaching does not enable or start a server, load code, or run a task.
        ref = body.ref
        aliases = [ref]
        if body.kind == "skills":
            skill = next((s for s in list_skills(store.data_dir / "skills")
                          if ref in skill_names(s.name)), None)
            if skill:
                ref, aliases = skill.name, skill_names(skill.name)
            exists = skill is not None
        elif body.kind == "plugins":
            exists = ref in registry.plugins
        else:
            exists = any(s["id"] == ref for s in store.list_mcp())
        if body.attached and not exists:
            raise HTTPException(404, i18n.pick_now("Capability not found", "这项能力尚未安装或已删除"))
        refs = []
        for existing in group["ext"].get(body.kind, []):
            if existing in aliases:
                if body.attached and ref not in refs:
                    refs.append(ref)
            else:
                refs.append(existing)
        if body.attached and ref not in refs:
            refs.append(ref)
        return group_out(store.update_group(gid, {"ext": {body.kind: refs}}))

    @app.delete("/api/groups/{gid}")
    async def del_group(gid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        require_idle(gid)
        store.delete_group(gid)
        return {"ok": True}

    @app.post("/api/groups/{gid}/members")
    async def add_member(gid: str, body: MemberIn) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        need(store.get_agent(body.agent_id), i18n.pick_now("Member", "成员"))
        require_idle(gid)
        store.add_member(gid, body.agent_id)
        return group_out(store.get_group(gid))  # type: ignore[arg-type]

    @app.delete("/api/groups/{gid}/members/{aid}")
    async def remove_member(gid: str, aid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        require_idle(gid)
        store.remove_member(gid, aid)
        return group_out(store.get_group(gid))  # type: ignore[arg-type]

    @app.get("/api/groups/{gid}/messages")
    async def messages(gid: str) -> list[dict]:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        return [_visible_message(m) for m in store.list_messages(gid)]

    @app.delete("/api/groups/{gid}/messages")
    async def clear_messages(gid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        require_idle(gid)
        store.clear_messages(gid)
        return {"ok": True}

    @app.post("/api/groups/{gid}/messages")
    async def send_message(gid: str, body: MessageIn) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        text = body.text.strip()
        picked = files_for(gid, [*body.attachments, *body.images])
        if not text and not picked:
            raise HTTPException(400, i18n.pick_now("A message cannot be empty", "消息不能为空"))

        async def emit(ev: dict) -> None:
            await hub.broadcast(gid, ev)

        async def run() -> None:
            try:
                await orch.handle_user_message(gid, text, emit, files=picked)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — what went wrong has to be visible in the UI, not just printed in the background
                print("orchestrator error:", repr(e))
                try:
                    await orch._system(gid, i18n.pick_now(f"Something went wrong in this round of collaboration: {e}", f"这一轮协作出错了:{e}"), emit)
                except Exception:  # noqa: BLE001
                    pass

        task = asyncio.create_task(run())
        tasks.setdefault(gid, set()).add(task)

        def _done(t: asyncio.Task) -> None:
            tasks.get(gid, set()).discard(t)
            if not t.cancelled() and t.exception():
                print("orchestrator error:", repr(t.exception()))

        task.add_done_callback(_done)
        return {"ok": True}

    @app.post("/api/groups/{gid}/messages/{mid}/forward")
    async def forward_message(gid: str, mid: str, body: ForwardIn) -> dict:
        """Hand one member's reply to another group, as a message from you.

        It goes through **the same path as a typed message** (that is what calling `send_message`
        here means) rather than writing a row straight into the target group: a forwarded message is
        something you asked that group to work on, so its members have to be told about it, and the
        only way a group is told anything is that endpoint. Writing the row directly would leave a
        message that no member ever sees.

        The text travels with a header naming where it came from — a member in the receiving group has
        no way to know otherwise, and an unattributed quote reads as something the user wrote. What
        does **not** travel is the attachments: a file lives in the workspace of the group it was
        added to, so a forwarded message that mentioned one would point at nothing.
        """
        source = need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        target = need(store.get_group(body.to_group_id), i18n.pick_now("Target group", "目标群"))
        if str(target["id"]) == str(source["id"]):
            raise HTTPException(400, i18n.pick_now(
                "That is the group it is already in — quote it there instead.",
                "这就是它所在的群 —— 在本群里引用它就行。"))
        row = need(store.get_message(mid), i18n.pick_now("Message", "消息"))
        if str(row.get("group_id") or "") != str(source["id"]):
            raise HTTPException(404, i18n.pick_now("That message is not in this group",
                                                   "这条消息不在这个群里"))
        who = str(row.get("sender_name") or "")
        text = i18n.pick_now(
            f"[Forwarded from \"{source['name']}\" — {who}]\n\n{row.get('content') or ''}",
            f"【转发】来自本机「{source['name']}」群里的 {who}:\n\n{row.get('content') or ''}")
        await send_message(str(target["id"]), MessageIn(text=text))
        return {"ok": True, "group_id": target["id"], "group_name": target["name"]}

    @app.get("/api/groups/{gid}/status")
    async def group_status(gid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        return {"busy": busy(gid)}

    @app.post("/api/groups/{gid}/stop")
    async def stop(gid: str) -> dict:
        active = [t for t in tasks.get(gid, ()) if not t.done()]
        for t in active:
            t.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if not busy(gid):
            await hub.broadcast(gid, {"type": "stopped"})
            await hub.broadcast(gid, {"type": "idle"})
        return {"cancelled": len(active)}

    @app.websocket("/ws/groups/{gid}")
    async def ws_group(ws: WebSocket, gid: str) -> None:
        origin = ws.headers.get("origin")
        bad_origin = not token and origin is not None and not re.match(DEV_ORIGIN_RE, origin)
        if bad_origin or not token_ok(ws.query_params.get("token")):
            await ws.close(code=1008)
            return
        await hub.connect(gid, ws)
        try:
            while True:
                await ws.receive_text()  # clients only keep the connection open; sending messages goes over REST
        except WebSocketDisconnect:
            hub.disconnect(gid, ws)

    _ctx = Ctx(store, router, orch, registry, mcp, library, memory, toolhub, prompts, updater, approvals, obsidian)
    app.include_router(build_router(_ctx))
    # The video zone: the parts a person drives directly (composing music, and later scenes and
    # motion). Its routes call the same composers the tools do rather than a second copy of them.
    app.include_router(build_video_zone_router(_ctx))
    app.include_router(build_zones_router(_ctx.store))
    # Kept reachable for the same reason `store`/`router`/`orch` are above: a long-running job's
    # state lives on it, and a test that wants to see the state of a run has nowhere else to look.
    app.state.ctx = _ctx
    app.include_router(build_external_router(store, orch.external))
    app.include_router(build_gallery_router(store, hooks))
    app.include_router(build_hooks_router(store, hooks))
    app.include_router(build_import_router(store))
    # Chat channels: the inbound webhook lives outside /api on purpose. Token middleware does
    # not cover it, because the caller is a chat platform rather than the app's own front end,
    # and each channel authenticates by the platform's own signature. It is the only route
    # here reachable from outside the machine.
    app.include_router(chan.router)
    app.state.channels = chan          # the background poller and the tests both need it

    return app
