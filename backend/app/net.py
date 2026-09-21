"""按目标地址选择 HTTP 客户端。

为什么需要:httpx 默认 trust_env=True,会读取 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
用户开着 Clash 这类代理时,连 127.0.0.1 上的 Ollama、本机自建 OpenAI 兼容服务也会被塞进代理,
表现成「Ollama 没有在运行」或 502 Bad Gateway —— 其实服务是好的(实测:同一台机器上
/api/local/status 返回 502 Bad Gateway,加 --noproxy 后一切正常)。

规则:目标是回环/局域网地址(本机服务、内网工作站、LAN 上的 Ollama)时一律直连;
真正的外网(GitHub、云端模型服务)仍然尊重系统代理,因为公司网络里往往只有走代理才出得去。

与 main.ensure_loopback_no_proxy() 的分工:那个函数往 NO_PROXY 里塞回环地址,作用是兜住
litellm / mcp 这些第三方库自己建的客户端(我们没法逐个改);这里则管本程序自己的客户端,
并且能覆盖 192.168.x / 10.x 这类**局域网**地址——内网工作站、LAN 上的 Ollama 同样不该走代理。
两者不冲突,一起用。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import httpx


def _host_of(url: str) -> str:
    """从各种写法里取出主机名:完整 URL、host:port、裸 IPv6(::1)、[::1]:port 都要认。"""
    s = url.strip()
    if "://" in s:
        return (urlparse(s).hostname or "").lower()
    if s.startswith("["):                       # [::1]:11434
        end = s.find("]")
        return s[1:end].lower() if end > 0 else ""
    if s.count(":") >= 2:                       # 裸 IPv6(host:port 只有一个冒号)
        return s.split("/")[0].lower()
    return s.split("/")[0].split(":")[0].lower()


def is_local_url(url: str) -> bool:
    """回环或私有网段(localhost / 127.x / ::1 / 10.x / 172.16-31.x / 192.168.x / 169.254.x)都算「本机或局域网」。"""
    host = _host_of(url)
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def client(url: str = "", **kwargs: object) -> httpx.AsyncClient:
    """建一个异步客户端。传 url 时会自动判断要不要绕过系统代理。
    想强制直连(例如只用来探测本机服务的那个客户端)可显式传 trust_env=False。"""
    kwargs.setdefault("timeout", 15.0)
    if is_local_url(url) and "trust_env" not in kwargs:
        kwargs["trust_env"] = False
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]
