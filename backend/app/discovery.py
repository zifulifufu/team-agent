""""Fetch model list": ask the provider which models it currently offers (similar to
Cherry Studio's Manage / fetch model list).

New model IDs change daily, so hardcoding them in presets is pointless — just ask
the provider. Protocols differ per vendor, so this adapts per kind; the result is
a unified list of model IDs.
"""

from __future__ import annotations

from . import i18n

import os
from typing import Any

import httpx

from . import net
from .router import ENV_KEYS

DEEPSEEK_BASE = "https://api.deepseek.com"
ANTHROPIC_BASE = "https://api.anthropic.com"
GEMINI_BASE = "https://generativelanguage.googleapis.com"
OLLAMA_BASE = "http://127.0.0.1:11434"


class DiscoveryError(Exception):
    """Error message that can be shown to the user directly (never contains the API key)."""


def _key(provider: dict) -> str:
    return provider.get("api_key") or os.environ.get(ENV_KEYS.get(provider["kind"], ""), "")


def _ids_from_openai_like(data: Any) -> list[str]:
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise DiscoveryError(i18n.pick_now("The response is not a list of models", "返回格式不是模型列表"))
    out = []
    for r in rows:
        mid = r.get("id") if isinstance(r, dict) else r
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


async def fetch_model_ids(provider: dict, timeout: float = 15.0, client: httpx.AsyncClient | None = None) -> list[str]:
    kind = provider["kind"]
    base = (provider.get("base_url") or "").rstrip("/")
    key = _key(provider)
    headers: dict[str, str] = {}
    params: dict[str, Any] = {}

    if kind == "ollama":
        url = (base or OLLAMA_BASE) + "/api/tags"
    elif kind == "anthropic":
        url = (base or ANTHROPIC_BASE) + "/v1/models"
        params = {"limit": 1000}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif kind == "gemini":
        url = (base or GEMINI_BASE) + "/v1beta/models"
        params = {"pageSize": 1000}
        headers = {"x-goog-api-key": key}  # put it in a header rather than the URL, so the key cannot leak into error messages
    elif kind == "deepseek":
        url = (base or DEEPSEEK_BASE) + "/models"
        headers = {"Authorization": f"Bearer {key}"}
    else:  # openai_compatible
        if not base:
            raise DiscoveryError(i18n.pick_now("Fill in the API address first", "请先填写 API 地址"))
        url = base + "/models"
        if key:
            headers = {"Authorization": f"Bearer {key}"}

    if kind in ("anthropic", "gemini", "deepseek") and not key:
        raise DiscoveryError(i18n.pick_now("Fill in the API key first", "请先填写 API Key"))

    own = client is None
    # local/LAN addresses connect directly, cloud addresses still use the system proxy
    c = client or net.client(url, timeout=timeout)
    try:
        r = await c.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        raise DiscoveryError(i18n.pick_now(f"Could not connect to {url.split('?')[0]}: {type(e).__name__}", f"无法连接 {url.split('?')[0]}:{type(e).__name__}")) from None
    finally:
        if own:
            await c.aclose()
    if r.status_code in (401, 403):
        raise DiscoveryError(i18n.pick_now("Authentication failed: the API key is wrong, or it lacks permission", "鉴权失败:API Key 不正确或没有权限"))
    if r.status_code >= 400:
        raise DiscoveryError(i18n.pick_now(f"The provider answered HTTP {r.status_code}", f"服务商返回 HTTP {r.status_code}"))
    try:
        data = r.json()
    except ValueError:
        raise DiscoveryError(i18n.pick_now("The response is not JSON; check whether the API address ends with /v1", "返回内容不是 JSON,请检查 API 地址是否以 /v1 结尾")) from None

    if kind == "ollama":
        ids = [m["name"] for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]
    elif kind == "gemini":
        ids = []
        for m in data.get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue  # skip embedding and other models that cannot chat
            ids.append(str(m.get("name", "")).removeprefix("models/"))
        ids = [i for i in ids if i]
    else:
        ids = _ids_from_openai_like(data)
    return sorted(set(ids))
