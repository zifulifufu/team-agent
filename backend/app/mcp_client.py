"""MCP 客户端:用官方 `mcp` 包连接 MCP 服务器,把它们的工具交给 agent 调用。

每个服务器由一个后台任务持有连接(anyio 要求进入和退出上下文在同一个任务里),
其它任务通过 call_tool() 发请求。连接是「用到时才建立」(首次被群聊使用或点「连接测试」),
服务器进程随后台任务结束而退出。传输方式:stdio(本地命令)、streamable HTTP、SSE。
"""

from __future__ import annotations

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
    """解析别处导出的 MCP 配置 JSON(Claude Desktop / Cherry Studio / Cursor 等通用的 mcpServers 写法)。
    只解析、不保存、不运行任何东西。返回 (服务器列表, 提示)。"""
    warnings: list[str] = []
    if len(text) > 200_000:
        raise ValueError("内容太长(超过 200KB)")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"不是合法的 JSON:{e}") from None
    if not isinstance(data, dict):
        raise ValueError("顶层应该是一个对象,如 {\"mcpServers\": {...}}")
    servers = data["mcpServers"] if "mcpServers" in data else data.get("servers")
    if servers is None:
        # 兼容三种简写:{名字: 配置} / 单个配置(带 command 或 url)
        servers = {"": data} if ("command" in data or "url" in data) else data
    if not isinstance(servers, dict) or not servers:
        raise ValueError("没有找到 mcpServers")
    out: list[dict] = []
    for name, cfg in list(servers.items())[:50]:
        label = name or "未命名"
        if not isinstance(cfg, dict):
            warnings.append(f"「{label}」的配置不是对象,已跳过")
            continue
        command = cfg.get("command") or ""
        url = cfg.get("url") or cfg.get("serverUrl") or cfg.get("baseUrl") or ""
        if not (isinstance(command, str) and isinstance(url, str)) or not (command or url):
            warnings.append(f"「{label}」既没有 command 也没有 url,已跳过")
            continue
        args = cfg.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
            warnings.append(f"「{label}」的 args 格式不对,已跳过")
            continue
        env, headers = cfg.get("env") or {}, cfg.get("headers") or {}
        if not (isinstance(env, dict) and isinstance(headers, dict)):
            warnings.append(f"「{label}」的 env/headers 格式不对,已跳过")
            continue
        t = str(cfg.get("type") or cfg.get("transport") or "").lower().replace("_", "-")
        transport = {"stdio": "stdio", "sse": "sse", "http": "http", "streamable-http": "http", "streamablehttp": "http"}.get(t, "")
        if t and not transport:
            warnings.append(f"「{label}」的类型 {t} 不认识,已按地址自动判断")
        if url and not re.match(r"^https?://", url):
            warnings.append(f"「{label}」的地址不是 http(s),已跳过")
            continue
        out.append({
            "name": (name or (command or url))[:60], "command": command.strip(), "args": [str(a) for a in args],
            "env": {str(k): str(v) for k, v in env.items()}, "url": url.strip(), "transport": transport,
            "headers": {str(k): str(v) for k, v in headers.items()},
            "description": str(cfg.get("description") or "")[:200],
            "enabled": not (cfg.get("disabled") is True or cfg.get("isActive") is False),
        })
    if not out:
        raise ValueError("没有可导入的服务器" + (":" + ";".join(warnings) if warnings else ""))
    if len(servers) > 50:
        warnings.append("只处理前 50 个")
    return out, warnings


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
            self.state.status, self.state.error = "error", f"连接超时({int(timeout)} 秒)"
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
                        raise RuntimeError("stdio 方式需要填写启动命令")
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
                    except ImportError:  # mcp 2.x:改名,且请求头要通过 http_client 传入
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
        except BaseException as e:  # noqa: BLE001 — ExceptionGroup 也要兜住
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
    """把 MCP 的 CallToolResult 变成纯文本。返回 (文本, 是否出错)。"""
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "type", "")
        if t == "text":
            parts.append(c.text)
        elif t == "image":
            parts.append("[图片内容,已省略]")
        elif t == "resource":
            res = getattr(c, "resource", None)
            parts.append(getattr(res, "text", None) or f"[资源 {getattr(res, 'uri', '')}]")
        else:
            parts.append(f"[{t or '未知'}内容]")
    text = "\n".join(p for p in parts if p).strip()
    if not text and getattr(result, "structuredContent", None):
        import json

        text = json.dumps(result.structuredContent, ensure_ascii=False)
    return text or "(无输出)", bool(getattr(result, "isError", False))


class McpManager:
    def __init__(self) -> None:
        self._conns: dict[str, _Conn] = {}

    def state(self, sid: str) -> ServerState | None:
        c = self._conns.get(sid)
        return c.state if c else None

    async def connect(self, cfg: dict, timeout: float = 30) -> ServerState:
        c = self._conns.get(cfg["id"])
        if c and c.cfg != cfg:  # 配置改过:断开重连
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
            raise RuntimeError("MCP 服务器未连接")
        try:
            res = await asyncio.wait_for(c.session.call_tool(tool, args), timeout)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("ClosedResourceError", "BrokenResourceError", "EndOfStream"):
                # 服务器进程没了:标成断开,下一次使用时会自动重连,而不是一直显示「已连接」
                c.state.status, c.state.error = "error", "服务器进程已退出或连接断开"
                await c.stop()
                raise RuntimeError("MCP 服务器连接已断开,下次调用会自动重连") from None
            raise
        text, is_error = flatten_result(res)
        return text, not is_error
