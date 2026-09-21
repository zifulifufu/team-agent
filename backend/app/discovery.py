"""「获取模型列表」:向服务商查询它当前提供哪些模型(类似 Cherry Studio 的 Manage / 获取模型列表)。

新模型 ID 天天在变,预设里写死没有意义 —— 直接问服务商。
各家协议不同,这里按 kind 分别适配;返回统一的模型 ID 列表。
"""

from __future__ import annotations

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
    """可直接展示给用户的错误信息(不会包含 API Key)。"""


def _key(provider: dict) -> str:
    return provider.get("api_key") or os.environ.get(ENV_KEYS.get(provider["kind"], ""), "")


def _ids_from_openai_like(data: Any) -> list[str]:
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise DiscoveryError("返回格式不是模型列表")
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
        headers = {"x-goog-api-key": key}  # 放请求头而不是 URL,避免 Key 出现在错误信息里
    elif kind == "deepseek":
        url = (base or DEEPSEEK_BASE) + "/models"
        headers = {"Authorization": f"Bearer {key}"}
    else:  # openai_compatible
        if not base:
            raise DiscoveryError("请先填写 API 地址")
        url = base + "/models"
        if key:
            headers = {"Authorization": f"Bearer {key}"}

    if kind in ("anthropic", "gemini", "deepseek") and not key:
        raise DiscoveryError("请先填写 API Key")

    own = client is None
    # 本机 / 局域网地址直连,云端地址仍走系统代理
    c = client or net.client(url, timeout=timeout)
    try:
        r = await c.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        raise DiscoveryError(f"无法连接 {url.split('?')[0]}:{type(e).__name__}") from None
    finally:
        if own:
            await c.aclose()
    if r.status_code in (401, 403):
        raise DiscoveryError("鉴权失败:API Key 不正确或没有权限")
    if r.status_code >= 400:
        raise DiscoveryError(f"服务商返回 HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise DiscoveryError("返回内容不是 JSON,请检查 API 地址是否以 /v1 结尾") from None

    if kind == "ollama":
        ids = [m["name"] for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]
    elif kind == "gemini":
        ids = []
        for m in data.get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue  # 过滤掉 embedding 等不能聊天的模型
            ids.append(str(m.get("name", "")).removeprefix("models/"))
        ids = [i for i in ids if i]
    else:
        ids = _ids_from_openai_like(data)
    return sorted(set(ids))
