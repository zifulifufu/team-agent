"""MCP client: connects to MCP servers with the official `mcp` package and hands their
tools to agents.

Each server's connection is held by a single background task (anyio requires entering and
exiting the context in the same task); other tasks send requests through call_tool().
Connections are established lazily (first use by a group chat, or the "test connection"
button), and the server process exits when the background task ends. Transports: stdio
(local command), streamable HTTP, SSE.
"""

from __future__ import annotations

from . import i18n

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any


def slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_]+", "_", text).strip("_").lower()
    return s or "server"


def pick_transport(cfg: dict) -> str:
    t = (cfg.get("transport") or "").lower()
    if t in ("stdio", "sse", "http"):
        return t
    if cfg.get("command"):
        return "stdio"
    url = cfg.get("url") or ""
    return "sse" if re.search(r"/sse/?$", url) else "http"


def parse_mcp_json(text: str) -> tuple[list[dict], list[str]]:
    """Parse an MCP config JSON exported elsewhere (the mcpServers form shared by Claude
    Desktop / Cherry Studio / Cursor and others). Parsing only: nothing is saved or run.
    Returns (server list, note)."""
    warnings: list[str] = []
    if len(text) > 200_000:
        raise ValueError(i18n.pick_now("The content is too long (over 200KB)", "内容太长(超过 200KB)"))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(i18n.pick_now(f"Not valid JSON: {e}", f"不是合法的 JSON:{e}")) from None
    if not isinstance(data, dict):
        raise ValueError(i18n.pick_now("the top level should be an object, such as {\"mcpServers\": {...}}", "顶层应该是一个对象,如 {\"mcpServers\": {...}}"))
    servers = data["mcpServers"] if "mcpServers" in data else data.get("servers")
    if servers is None:
        # accept three shorthands: {name: config} / a single config (with command or url)
        servers = {"": data} if ("command" in data or "url" in data) else data
    if not isinstance(servers, dict) or not servers:
        raise ValueError(i18n.pick_now("No mcpServers found", "没有找到 mcpServers"))
    out: list[dict] = []
    for name, cfg in list(servers.items())[:50]:
        label = name or i18n.pick_now("Untitled", "未命名")
        if not isinstance(cfg, dict):
            warnings.append(i18n.pick_now(f"the configuration for \"{label}\" is not an object, so it was skipped", f"「{label}」的配置不是对象,已跳过"))
            continue
        command = cfg.get("command") or ""
        url = cfg.get("url") or cfg.get("serverUrl") or cfg.get("baseUrl") or ""
        if not (isinstance(command, str) and isinstance(url, str)) or not (command or url):
            warnings.append(i18n.pick_now(f"\"{label}\" has neither a command nor a url, so it was skipped", f"「{label}」既没有 command 也没有 url,已跳过"))
            continue
        args = cfg.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
            warnings.append(i18n.pick_now(f"the args of \"{label}\" have the wrong shape, so it was skipped", f"「{label}」的 args 格式不对,已跳过"))
            continue
        env, headers = cfg.get("env") or {}, cfg.get("headers") or {}
        if not (isinstance(env, dict) and isinstance(headers, dict)):
            warnings.append(i18n.pick_now(f"the env/headers of \"{label}\" have the wrong shape, so it was skipped", f"「{label}」的 env/headers 格式不对,已跳过"))
            continue
        t = str(cfg.get("type") or cfg.get("transport") or "").lower().replace("_", "-")
        transport = {"stdio": "stdio", "sse": "sse", "http": "http", "streamable-http": "http", "streamablehttp": "http"}.get(t, "")
        if t and not transport:
            warnings.append(i18n.pick_now(f"the type {t} of \"{label}\" is not recognised, so it was inferred from the address", f"「{label}」的类型 {t} 不认识,已按地址自动判断"))
        if url and not re.match(r"^https?://", url):
            warnings.append(i18n.pick_now(f"the address of \"{label}\" is not http(s), so it was skipped", f"「{label}」的地址不是 http(s),已跳过"))
            continue
        out.append({
            "name": (name or (command or url))[:60], "command": command.strip(), "args": [str(a) for a in args],
            "env": {str(k): str(v) for k, v in env.items()}, "url": url.strip(), "transport": transport,
            "headers": {str(k): str(v) for k, v in headers.items()},
            "description": str(cfg.get("description") or "")[:200],
            "enabled": not (cfg.get("disabled") is True or cfg.get("isActive") is False),
        })
    if not out:
        raise ValueError(i18n.pick_now("There are no servers to import", "没有可导入的服务器") + (":" + ";".join(warnings) if warnings else ""))
    if len(servers) > 50:
        warnings.append(i18n.pick_now("Only the first 50 are handled", "只处理前 50 个"))
    return out, warnings


def validate_cfg(name: str, command: str, url: str, transport: str) -> tuple[str, str] | None:
    """Check a server definition before it is stored. Returns an (en, zh) message, or None.

    Lives here rather than in the API layer because there are now three ways in — typing
    one, pasting JSON, and importing from another application's config — and a validation
    rule that exists in one of them but not the others is the kind of gap that only shows
    up as "the same server imports fine over there".
    """
    if not (name or "").strip():
        return ("A name is required", "请填写名称")
    if not (command or "").strip() and not (url or "").strip():
        return ("Enter a start command (local) or a service URL (remote)",
                "请填写启动命令(本地)或服务地址(远程)")
    if transport and transport not in ("stdio", "sse", "http"):
        return ("The transport must be one of stdio / sse / http", "传输方式只能是 stdio / sse / http")
    if url and not re.match(r"^https?://", url):
        return ("The service URL must start with http:// or https://",
                "服务地址必须以 http:// 或 https:// 开头")
    return None


def import_servers(store: object, servers: list[dict], names: list[str] | None = None) -> dict:
    """Add parsed servers to the store, skipping names that already exist.

    Everything is validated **before anything is saved**: a list where the third entry is
    malformed has to fail as a whole, otherwise an import leaves the store half-changed and
    the UI reports an error over a state that did partly change.

    Imported servers keep the `enabled` flag from their source config but are never started
    here — connecting is a separate, explicit action.
    """
    have = {m["name"] for m in store.list_mcp()}          # type: ignore[attr-defined]
    todo, skipped, bad = [], [], []
    for s in servers:
        if names is not None and s["name"] not in names:
            continue
        if s["name"] in have:
            skipped.append(s["name"])
            continue
        message = validate_cfg(s["name"], s["command"], s.get("url", ""), s.get("transport", ""))
        if message:
            bad.append(f"{s['name']}: {i18n.pick_now(*message)}")
            continue
        todo.append(s)
        have.add(s["name"])
    if bad:
        raise ValueError("; ".join(bad))
    added = []
    for s in todo:
        m = store.add_mcp(s["name"], s["command"], s["args"], s["env"], s["url"],       # type: ignore[attr-defined]
                          s["transport"], s["headers"], s.get("description", ""))
        if not s.get("enabled", True):
            m = store.update_mcp(m["id"], {"enabled": False})                            # type: ignore[attr-defined]
        added.append(m)
    return {"added": added, "skipped": skipped}


@dataclass
class ServerState:
    id: str
    name: str
    status: str = "idle"          # idle | connecting | ready | error
    error: str = ""
    tools: list[dict] = field(default_factory=list)   # {name, description, parameters, read_only}
    connected_at: float = 0.0


class _Conn:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = ServerState(id=cfg["id"], name=cfg["name"])
        self.session: Any = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self, timeout: float) -> None:
        if self._task and not self._task.done():
            pass
        else:
            self._stop.clear()
            self._ready.clear()
            self.state.status, self.state.error = "connecting", ""
            self._task = asyncio.create_task(self._run(), name=f"mcp-{self.cfg['id']}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            self.state.status, self.state.error = "error", i18n.pick_now(f"Connection timed out ({int(timeout)}s)", f"连接超时({int(timeout)} 秒)")
            await self.stop()

    async def _run(self) -> None:
        cfg = self.cfg
        try:
            from mcp import ClientSession, StdioServerParameters

            async with AsyncExitStack() as stack:
                kind = pick_transport(cfg)
                if kind == "stdio":
                    from mcp.client.stdio import stdio_client

                    if not cfg.get("command"):
                        raise RuntimeError(i18n.pick_now("The stdio transport needs a start command", "stdio 方式需要填写启动命令"))
                    params = StdioServerParameters(
                        command=cfg["command"], args=list(cfg.get("args") or []), env=dict(cfg.get("env") or {}) or None
                    )
                    streams = await stack.enter_async_context(stdio_client(params))
                elif kind == "sse":
                    from mcp.client.sse import sse_client

                    streams = await stack.enter_async_context(sse_client(cfg["url"], headers=cfg.get("headers") or None))
                else:
                    try:  # mcp 1.x
                        from mcp.client.streamable_http import streamablehttp_client

                        cm = streamablehttp_client(cfg["url"], headers=cfg.get("headers") or None)
                    except ImportError:  # mcp 2.x: renamed, and headers must be passed through http_client
                        import httpx2
                        from mcp.client.streamable_http import streamable_http_client

                        cm = streamable_http_client(
                            cfg["url"], http_client=httpx2.AsyncClient(headers=cfg.get("headers") or None)
                        )
                    streams = await stack.enter_async_context(cm)
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                listed = await session.list_tools()
                self.state.tools = [
                    {
                        "name": t.name,
                        "description": (t.description or "").strip(),
                        "parameters": t.inputSchema or {"type": "object", "properties": {}},
                        "read_only": bool(getattr(getattr(t, "annotations", None), "readOnlyHint", False)),
                    }
                    for t in listed.tools
                ]
                self.session = session
                self.state.status, self.state.connected_at = "ready", time.time()
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 — ExceptionGroup has to be caught as well
            self.state.status = "error"
            self.state.error = _describe(e)
        finally:
            self.session = None
            if self.state.status != "error":
                self.state.status = "idle"
            self._ready.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._task.cancel()
                try:
                    await self._task
                except BaseException:  # noqa: BLE001
                    pass
            self._task = None
        if self.state.status != "error":
            self.state.status = "idle"


def _describe(e: BaseException) -> str:
    subs = getattr(e, "exceptions", None)
    if subs:
        return _describe(subs[0])
    msg = str(e) or type(e).__name__
    return f"{type(e).__name__}: {msg}"[:400]


def flatten_result(result: Any) -> tuple[str, bool]:
    """Turn an MCP CallToolResult into plain text. Returns (text, whether it errored)."""
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "type", "")
        if t == "text":
            parts.append(c.text)
        elif t == "image":
            parts.append(i18n.pick_now("[image content omitted]", "[图片内容,已省略]"))
        elif t == "resource":
            res = getattr(c, "resource", None)
            parts.append(getattr(res, "text", None) or i18n.pick_now(f"[resource {getattr(res, 'uri', '')}]", f"[资源 {getattr(res, 'uri', '')}]"))
        else:
            parts.append(i18n.pick_now(f"[{t or 'unknown'} content]", f"[{t or '未知'}内容]"))
    text = "\n".join(p for p in parts if p).strip()
    if not text and getattr(result, "structuredContent", None):
        import json

        text = json.dumps(result.structuredContent, ensure_ascii=False)
    return text or i18n.pick_now("(no output)", "(无输出)"), bool(getattr(result, "isError", False))


class McpManager:
    def __init__(self) -> None:
        self._conns: dict[str, _Conn] = {}

    def state(self, sid: str) -> ServerState | None:
        c = self._conns.get(sid)
        return c.state if c else None

    async def connect(self, cfg: dict, timeout: float = 30) -> ServerState:
        c = self._conns.get(cfg["id"])
        if c and c.cfg != cfg:  # config changed: disconnect and reconnect
            await c.stop()
            c = None
        if c is None:
            c = self._conns[cfg["id"]] = _Conn(cfg)
        await c.start(timeout)
        return c.state

    async def disconnect(self, sid: str) -> None:
        c = self._conns.pop(sid, None)
        if c:
            await c.stop()

    async def shutdown(self) -> None:
        for sid in list(self._conns):
            await self.disconnect(sid)

    async def call_tool(self, sid: str, tool: str, args: dict, timeout: float) -> tuple[str, bool]:
        c = self._conns.get(sid)
        if not c or not c.session:
            raise RuntimeError(i18n.pick_now("The MCP server is not connected", "MCP 服务器未连接"))
        try:
            res = await asyncio.wait_for(c.session.call_tool(tool, args), timeout)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("ClosedResourceError", "BrokenResourceError", "EndOfStream"):
                # server process is gone: mark it disconnected; the next use reconnects automatically
# instead of showing "connected" forever
                c.state.status, c.state.error = "error", "服务器进程已退出或连接断开"
                await c.stop()
                raise RuntimeError(i18n.pick_now("The MCP server connection dropped; the next call will reconnect automatically", "MCP 服务器连接已断开,下次调用会自动重连")) from None
            raise
        text, is_error = flatten_result(res)
        return text, not is_error
