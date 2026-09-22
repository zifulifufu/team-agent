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
from typing import Any, Awaitable, Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .api_ext import Ctx, build_router
from .api_external import build_external_router
from .api_gallery import build_gallery_router
from .api_whatsapp import build_whatsapp_router
from .approvals import Approvals
from .discovery import DiscoveryError, fetch_model_ids
from .health import HealthBoard
from .library import Library
from .local_models import native_machine
from .mcp_client import McpManager
from .memory import MemoryService
from . import templates
from . import i18n
from . import images
from . import net
from .obsidian import ObsidianSync
from .orchestrator import Orchestrator
from .presets import DEFAULT_SETTINGS, PRESETS
from .prompting import PromptBuilder
from .router import ModelRouter
from .stats import compute_stats
from .store import Store
from .toolhub import ToolHub
from .tools import build_registry, ensure_example_skills
from .updater import Updater


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
    name: str
    member_ids: list[str] = []
    host_agent_id: str | None = None
    ext: dict | None = None
    prompt: str = ""


class GroupPatch(BaseModel):
    name: str | None = None
    host_agent_id: str | None = None
    prompt: str | None = None
    ext: dict | None = None


class MemberIn(BaseModel):
    agent_id: str


class MessageIn(BaseModel):
    text: str
    # Ids returned by POST /api/groups/{gid}/attachments. A message may carry images with no
    # text at all ("have a look at this"), but not the other way round.
    images: list[str] = []


class PullIn(BaseModel):
    model: str


class ModelBatchIn(BaseModel):
    model_names: list[str]


APP_VERSION = "0.5.1"


def _pkg_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def public_provider(p: dict) -> dict:
    key = p.get("api_key") or ""
    out = {k: v for k, v in p.items() if k != "api_key"}
    out["has_key"] = bool(key)
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
    ensure_example_skills(store.data_dir / "skills", store._flag)
    router = ModelRouter(store, completion_fn)
    registry = build_registry(store.data_dir / "plugins")
    mcp = McpManager()
    library = Library(store)
    memory = MemoryService(store, router)
    toolhub = ToolHub(store, registry, mcp, library, memory)
    board = HealthBoard(store, router)
    prompts = PromptBuilder(store, router)
    approvals = Approvals(store)
    obsidian = ObsidianSync(store)
    orch = Orchestrator(store, router, prompts=prompts, toolhub=toolhub, memory=memory, library=library,
                        registry=registry, mcp=mcp, approvals=approvals)
    updater = Updater(store, APP_VERSION, github_transport)
    hub = Hub()
    tasks: dict[str, set[asyncio.Task]] = {}

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
        try:
            yield
        finally:
            for t in bg:
                t.cancel()
            await orch.drain()
            await mcp.shutdown()

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
    # The API is loopback-only on purpose. The single exception is the WhatsApp webhook:
    # Meta posts to whatever public hostname sits in front of this app (a tunnel or a VPS),
    # so that host has to be accepted here. It is the only path reachable from outside, and
    # it authenticates every request by HMAC signature rather than by token, so allowing the
    # host does not widen the API surface.
    _hook_host = str(store.get_settings().get("whatsapp_public_host") or "").strip()
    _hook_host = _hook_host.split("://")[-1].split("/")[0].split(":")[0]
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost"] + ([_hook_host] if _hook_host else []))
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

    def need(x: Any, what: str) -> Any:
        if x is None:
            raise HTTPException(404, i18n.pick_now(f"{what} not found", f"{what}不存在"))
        return x

    def images_for(gid: str, ids: list[str]) -> list[dict]:
        """Descriptors for the attachment ids a message references.

        An id belonging to another group, or one whose file has gone, is dropped rather than
        trusted: the client is not the authority on what an image is or where it came from.
        """
        out: list[dict] = []
        for aid in list(dict.fromkeys(ids))[:10]:          # a message carries at most ten
            row = store.get_attachment(aid)
            if not row or row["group_id"] != gid or not images.find_file(store.data_dir, aid):
                continue
            out.append({"id": aid, "name": row["name"], "mime": row["mime"], "bytes": row["bytes"]})
        return out

    # -------------------------------------------------------------- misc
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/presets")
    async def presets() -> list[dict]:
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
              # video: H3 itself caps a clip at 15s, and a render is minutes rather than seconds
              "video_short_edge": (128, 2048), "video_max_seconds": (1, 15), "video_timeout": (30, 7200), "video_max_mb": (1, 4096),
              # whatsapp: WhatsApp refuses a single text body over 4096 characters
              "whatsapp_max_chars": (100, 4096),
              # scoring: the threshold is a percentage, and the excerpt bounds what a judge reads
              "score_threshold": (0, 100), "score_excerpt_chars": (100, 4000), "score_max_lessons": (0, 3)}
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
        }

    # --------------------------------------------------------- providers
    @app.get("/api/providers")
    async def providers() -> list[dict]:
        models = store.list_models()
        out = []
        for p in store.list_providers():
            pp = public_provider(p)
            pp["models"] = [m for m in models if m["provider_id"] == p["id"]]
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
        pp = public_provider(p)
        pp["models"] = [m for m in store.list_models() if m["provider_id"] == p["id"]]
        return pp

    @app.patch("/api/providers/{pid}")
    async def patch_provider(pid: str, body: ProviderPatch) -> dict:
        need(store.get_provider(pid), i18n.pick_now("Provider", "服务商"))
        p = store.update_provider(pid, body.model_dump(exclude_unset=True))
        router.reset_circuit()
        return public_provider(p)  # type: ignore[arg-type]

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
        return store.list_models()

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
        if cur.get("origin") == "model" and "model_id" in patch and patch["model_id"] != cur["model_id"]:
            raise HTTPException(400, i18n.pick_now("A model member *is* that model, so its model cannot be changed; to change it, remove the member from the group and add a different model", "模型成员就是这个模型本身,不能换模型;想换的话把它移出群、再拉入另一个模型"))
        return store.update_agent(aid, patch)  # type: ignore[return-value]

    @app.delete("/api/agents/{aid}")
    async def del_agent(aid: str) -> dict:
        need(store.get_agent(aid), i18n.pick_now("Member", "成员"))
        store.delete_agent(aid)
        return {"ok": True}

    # ------------------------------------------------------------ groups
    @app.get("/api/groups")
    async def groups() -> list[dict]:
        return [templates.group_view(g) for g in store.list_groups()]  # type: ignore[misc]

    @app.post("/api/groups")
    async def create_group(body: GroupIn) -> dict:
        known = {a["id"] for a in store.list_agents()}
        if any(i not in known for i in [*body.member_ids, *([body.host_agent_id] if body.host_agent_id else [])]):
            raise HTTPException(400, i18n.pick_now("A member or the host does not exist", "成员或群主不存在"))
        check_host(body.host_agent_id)
        return templates.group_view(
            store.create_group(body.name, body.host_agent_id, body.member_ids, body.ext, body.prompt))

    def check_host(host_id: str | None) -> None:
        host = store.get_agent(host_id) if host_id else None
        if host and host.get("engine"):
            raise HTTPException(400, i18n.pick_now("An external agent cannot be the host (the host splits the work, so it has to be a model member)", "外部智能体不能当群主(群主负责分工,需要是模型成员)"))

    @app.patch("/api/groups/{gid}")
    async def patch_group(gid: str, body: GroupPatch) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        if body.host_agent_id:
            check_host(body.host_agent_id)
        return templates.group_view(
            store.update_group(gid, body.model_dump(exclude_unset=True)))  # type: ignore[arg-type]

    @app.delete("/api/groups/{gid}")
    async def del_group(gid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        store.delete_group(gid)
        return {"ok": True}

    @app.post("/api/groups/{gid}/members")
    async def add_member(gid: str, body: MemberIn) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        need(store.get_agent(body.agent_id), i18n.pick_now("Member", "成员"))
        store.add_member(gid, body.agent_id)
        return templates.group_view(store.get_group(gid))  # type: ignore[arg-type]

    @app.delete("/api/groups/{gid}/members/{aid}")
    async def remove_member(gid: str, aid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        store.remove_member(gid, aid)
        return templates.group_view(store.get_group(gid))  # type: ignore[arg-type]

    @app.get("/api/groups/{gid}/messages")
    async def messages(gid: str) -> list[dict]:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        return store.list_messages(gid)

    @app.delete("/api/groups/{gid}/messages")
    async def clear_messages(gid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        store.clear_messages(gid)
        return {"ok": True}

    @app.post("/api/groups/{gid}/messages")
    async def send_message(gid: str, body: MessageIn) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        text = body.text.strip()
        picked = images_for(gid, body.images)
        if not text and not picked:
            raise HTTPException(400, i18n.pick_now("A message cannot be empty", "消息不能为空"))

        async def emit(ev: dict) -> None:
            await hub.broadcast(gid, ev)

        async def run() -> None:
            try:
                await orch.handle_user_message(gid, text, emit, images=picked)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — what went wrong has to be visible in the UI, not just printed in the background
                print("orchestrator error:", repr(e))
                try:
                    await orch._system(gid, i18n.pick_now(f"Something went wrong in this round of collaboration: {e}", f"这一轮协作出错了:{e}"), emit)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                await emit({"type": "idle"})  # this round of collaboration is over (normally, with an error, or stopped)

        task = asyncio.create_task(run())
        tasks.setdefault(gid, set()).add(task)

        def _done(t: asyncio.Task) -> None:
            tasks.get(gid, set()).discard(t)
            if not t.cancelled() and t.exception():
                print("orchestrator error:", repr(t.exception()))

        task.add_done_callback(_done)
        return {"ok": True}

    @app.get("/api/groups/{gid}/status")
    async def group_status(gid: str) -> dict:
        need(store.get_group(gid), i18n.pick_now("Group chat", "群聊"))
        return {"busy": any(not t.done() for t in tasks.get(gid, ()))}

    @app.post("/api/groups/{gid}/stop")
    async def stop(gid: str) -> dict:
        n = 0
        for t in list(tasks.get(gid, ())):
            t.cancel()
            n += 1
        await hub.broadcast(gid, {"type": "stopped"})
        if not n:   # with nothing running (for instance the backend just restarted), the UI has to go back to
# idle as well
            await hub.broadcast(gid, {"type": "idle"})
        return {"cancelled": n}

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

    app.include_router(build_router(Ctx(store, router, orch, registry, mcp, library, memory, toolhub, prompts, updater, approvals, obsidian)))
    app.include_router(build_external_router(store, orch.external))
    app.include_router(build_gallery_router(store))
    # The WhatsApp webhook lives outside /api on purpose: token middleware does not cover it,
    # because the caller is Meta rather than the app's own front end, and a webhook has to
    # authenticate by signature. It is the only route here reachable from outside the machine.
    app.include_router(build_whatsapp_router(store, orch, hub))

    return app
