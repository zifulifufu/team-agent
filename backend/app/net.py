"""Pick an HTTP client based on the destination address.

Why this exists: httpx defaults to trust_env=True, which reads HTTP_PROXY /
HTTPS_PROXY / ALL_PROXY. With a proxy such as Clash running, even 127.0.0.1
(Ollama, a self-hosted OpenAI-compatible service) gets funneled through the proxy
and surfaces as "Ollama is not running" or 502 Bad Gateway — even though the
service is fine (observed: /api/local/status returned 502 Bad Gateway on the
same machine, everything worked once --noproxy was added).

Rule: loopback/LAN destinations (local services, intranet workstations, Ollama on
the LAN) always connect directly; real external hosts (GitHub, cloud model
providers) still honor the system proxy, since corporate networks often require it.

Division of labor with main.ensure_loopback_no_proxy(): that function injects
loopback addresses into NO_PROXY to catch clients created by third-party libraries
such as litellm / mcp (which we cannot patch one by one); this module governs our
own clients and additionally covers **LAN** ranges like 192.168.x / 10.x —
intranet workstations and LAN Ollama should not go through a proxy either.
The two do not conflict; they are meant to be used together.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import httpx


def _host_of(url: str) -> str:
    """Extract the hostname from any notation: full URL, host:port, bare IPv6 (::1),
    and [::1]:port are all recognized."""
    s = url.strip()
    if "://" in s:
        return (urlparse(s).hostname or "").lower()
    if s.startswith("["):                       # [::1]:11434
        end = s.find("]")
        return s[1:end].lower() if end > 0 else ""
    if s.count(":") >= 2:                       # bare IPv6 (host:port has only one colon)
        return s.split("/")[0].lower()
    return s.split("/")[0].split(":")[0].lower()


def is_local_url(url: str) -> bool:
    """Loopback or private ranges (localhost / 127.x / ::1 / 10.x / 172.16-31.x /
    192.168.x / 169.254.x) all count as "this machine or the LAN"."""
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
    """Build an async client. Passing url decides automatically whether to bypass the
    system proxy. Pass trust_env=False explicitly to force a direct connection
    (for example the client used only to probe local services)."""
    kwargs.setdefault("timeout", 15.0)
    if is_local_url(url) and "trust_env" not in kwargs:
        kwargs["trust_env"] = False
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]
