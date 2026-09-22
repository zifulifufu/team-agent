"""Per-model connectivity indicator.

One status per model:
  ok       green    last call/check succeeded
  limited  yellow   rate limited or out of quota (fine after a short wait)
  bad      red      unreachable, invalid key, wrong model ID, local service not started or model not downloaded
  unknown  hollow   usable, but never checked (cloud models are not checked automatically, to avoid quietly spending your quota)
  off      gray     will not be called at all right now: disabled / no key / outbound switch off

Where results come from: a manual "check" (sends one very short request), every real
chat (recorded in passing), and local service probing (costs no tokens).
"""

from __future__ import annotations

from . import i18n

import asyncio
import re
import time
from typing import Any

import httpx

from .router import ModelRouter, has_credentials
from .store import Store

CHECK_CONCURRENCY = 3
STALE_SECONDS = 24 * 3600  # results older than a day are still shown, but flagged as "older"

# Every diagnostic this program writes *into a health record*. That record is persisted, so the
# text in it has to be readable whoever opens the file — but the reader's language is only known
# later, when the indicator is drawn. Hence the split: canonical English on disk, rendered in the
# reader's language here. Both spellings are recognised on the way in, which is why records
# written before this split need no migration — they simply start rendering correctly.
#
# Text this program did *not* write (a provider's own error, an exception's message) is absent on
# purpose: it has no translation, and inventing one is worse than showing it as it came.
DIAGNOSTICS: list[tuple[str, str]] = [
    ("Connected (a reasoning model; within the quota it returned only its reasoning)",
     "连接正常(思考型模型,额度内只返回了思考过程)"),
    ("Ollama is not running (or the address is wrong):{why}",
     "Ollama 没有在运行(或地址不对):{why}"),
    ("Could not reach {base}:{why}", "连不上 {base}:{why}"),
    ("Ollama does not have this model yet; pull it first: ollama pull {model}",
     "Ollama 里还没有这个模型,先下载:ollama pull {model}"),
    ("Ollama is running and the model is downloaded (nothing was actually called; the first reply "
     "loads it into memory)",
     "Ollama 在运行,模型已下载(没有实际调用,首次回复要加载进内存)"),
    ("The service is reachable", "服务可连接"),
    # A stored failure detail is "<provider's own message>" plus one of the hints the router
    # appends (see router._HINTS). Only the hint is ours, so only the hint is in this table: the
    # `{head}` before it is whatever the provider said, in whatever language it said it, and it is
    # left exactly as it came.
    ("{head} — the provider is rate-limiting you: most likely the account's per-minute request "
     "quota is too low (common on new or free plans). Wait a few seconds and retry, or raise the "
     "quota in the provider's console.",
     "{head} —— 服务商限速了:多半是账号的每分钟请求数/额度太低(新账号或免费档常见),稍等几秒再试,或到服务商后台提高额度。"),
    ("{head} — the key is invalid, expired, or lacks permission.",
     "{head} —— 密钥无效、过期,或没有权限。"),
    ("{head} — this key is not allowed to use that model.",
     "{head} —— 这个密钥没有权限使用该模型。"),
    ("{head} — the provider has no such model ID, or your account cannot use it.",
     "{head} —— 服务商那边没有这个模型 ID,或你的账号无权使用。"),
    ("{head} — the request timed out.", "{head} —— 请求超时。"),
    ("{head} — cannot reach the provider; check the network and the API base URL.",
     "{head} —— 连接不上服务商,检查网络和 API 地址。"),
]
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _template_re(template: str) -> re.Pattern[str]:
    """`{name}` becomes a capture; every other character is matched literally."""
    parts, pos = [], 0
    for m in _PLACEHOLDER.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))
        parts.append(f"(?P<{m.group(1)}>.*)")
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    return re.compile("".join(parts) + r"\Z", re.DOTALL)


_BY_TEMPLATE = [(_template_re(en), _template_re(zh), en, zh) for en, zh in DIAGNOSTICS]


def localize_detail(detail: str, lang: str | None = None) -> str:
    """Render a stored health diagnostic in the reader's language.

    Called when the indicator is built, not when the record is written: one record is read by
    whoever happens to open the page, and that is not necessarily the person who pressed "check".
    """
    if not detail:
        return detail
    want_zh = (lang or i18n.current()) != "en"
    for en_re, zh_re, en, zh in _BY_TEMPLATE:
        m = en_re.fullmatch(detail) or zh_re.fullmatch(detail)
        if m:
            return (zh if want_zh else en).format(**m.groupdict())
    return detail


def static_state(model: dict, provider: dict, external_ok: bool) -> tuple[str, str] | None:
    """Cases where "will not be called right now" can be decided without sending a request;
returns (off, reason), otherwise None. The reason is worked out on the spot and never stored, so
it can simply follow the language of the request that asked for it."""
    if not model["enabled"] or not provider["enabled"]:
        return "off", i18n.pick_now("Disabled", "已停用")
    if not provider["is_local"] and not external_ok:
        return "off", i18n.pick_now("Outbound calls are off", "外呼已禁用")
    if not has_credentials(provider):
        return "off", i18n.pick_now("No API key configured", "未配置 API Key")
    return None


def _installed(name: str, installed: set[str]) -> bool:
    return name in installed or (":" not in name and f"{name}:latest" in installed)


class HealthBoard:
    def __init__(self, store: Store, router: ModelRouter):
        self.store, self.router = store, router

    # ------------------------------------------------------------------ view
    def view(self) -> dict[str, dict]:
        cfg = self.store.get_settings()
        ext_ok = bool(cfg["external_calls_enabled"])
        providers = {p["id"]: p for p in self.store.list_providers()}
        recs = self.store.all_health()
        now = time.time()
        out: dict[str, dict] = {}
        for m in self.store.list_models():
            p = providers[m["provider_id"]]
            off = static_state(m, p, ext_ok)
            rec = recs.get(m["id"])
            if off:
                out[m["id"]] = {"state": off[0], "detail": off[1], "latency_ms": 0, "checked_at": rec["checked_at"] if rec else 0,
                                "source": rec["source"] if rec else "", "stale": False}
                continue
            if self.router.circuit_open(m["id"]):
                out[m["id"]] = {"state": "limited", "detail": i18n.pick_now("Failed repeatedly, so it is temporarily tripped; it retries automatically", "连续失败,暂时熔断,稍后自动重试"), "latency_ms": 0,
                                "checked_at": rec["checked_at"] if rec else 0, "source": rec["source"] if rec else "", "stale": False}
                continue
            if not rec:
                out[m["id"]] = {"state": "unknown", "detail": i18n.pick_now("Not checked yet", "还没检测过"), "latency_ms": 0, "checked_at": 0, "source": "", "stale": False}
                continue
            out[m["id"]] = {"state": rec["status"], "detail": localize_detail(rec["detail"]), "latency_ms": rec["latency_ms"],
                            "checked_at": rec["checked_at"], "source": rec["source"],
                            "stale": now - rec["checked_at"] > STALE_SECONDS}
        return out

    # ------------------------------------------------------------ local probe
    async def probe_local(self) -> int:
        """Probe a local service: Ollama is checked via /api/tags (is it running, is the model
downloaded), other self-hosted services via whether /models can be reached.
Consumes no tokens."""
        providers = {p["id"]: p for p in self.store.list_providers()}
        by_provider: dict[str, list[dict]] = {}
        for m in self.store.list_models():
            p = providers[m["provider_id"]]
            if p["is_local"] and p["enabled"] and m["enabled"]:
                by_provider.setdefault(p["id"], []).append(m)
        n = 0
        # Everything written below goes into a health record, which outlives this request and is
        # read by whoever opens the page next — so it is written in one language, always, and
        # `localize_detail` puts it into the reader's. See DIAGNOSTICS.
        # probe only local/LAN services, explicitly bypassing the system proxy (otherwise a
# running Clash would send 127.0.0.1 requests to the proxy too)
        async with httpx.AsyncClient(timeout=2.5, trust_env=False) as c:
            for pid, models in by_provider.items():
                p = providers[pid]
                base = (p["base_url"] or ("http://127.0.0.1:11434" if p["kind"] == "ollama" else "")).rstrip("/")
                t0 = time.time()
                try:
                    if p["kind"] == "ollama":
                        r = await c.get(base + "/api/tags")
                        r.raise_for_status()
                        installed = {x["name"] for x in r.json().get("models", [])}
                    else:
                        r = await c.get(base + "/models")
                        r.raise_for_status()
                        installed = None
                except Exception as e:  # noqa: BLE001
                    why = ("Ollama is not running (or the address is wrong)"
                           if p["kind"] == "ollama" else f"Could not reach {base}")
                    for m in models:
                        self.store.set_health(m["id"], "bad", f"{why}:{type(e).__name__}", 0, "probe")
                        n += 1
                    continue
                ms = int((time.time() - t0) * 1000)
                for m in models:
                    if installed is not None and not _installed(m["model_name"], installed):
                        self.store.set_health(m["id"], "bad", f"Ollama does not have this model yet; pull it first: ollama pull {m['model_name']}", ms, "probe")
                    else:
                        note = "Ollama is running and the model is downloaded (nothing was actually called; the first reply loads it into memory)" if installed is not None else "The service is reachable"
                        self.store.set_health(m["id"], "ok", note, ms, "probe")
                    n += 1
        return n

    # ------------------------------------------------------------ real check
    async def check(self, model_ids: list[str] | None = None, *, cloud: bool = True) -> dict[str, Any]:
        """Check the given models (by default every model that would be called right now).
Local models are only probed; cloud models get one very short request (a few tokens)."""
        cfg = self.store.get_settings()
        ext_ok = bool(cfg["external_calls_enabled"])
        providers = {p["id"]: p for p in self.store.list_providers()}
        models = [m for m in self.store.list_models() if model_ids is None or m["id"] in model_ids]
        cloud_targets = [
            m for m in models
            if not providers[m["provider_id"]]["is_local"] and static_state(m, providers[m["provider_id"]], ext_ok) is None
        ]
        await self.probe_local()
        if cloud:
            sem = asyncio.Semaphore(CHECK_CONCURRENCY)

            async def one(m: dict) -> None:
                async with sem:
                    await self.router.test_model(m["id"])  # results are recorded into the health table by the router

            await asyncio.gather(*(one(m) for m in cloud_targets))
        return {"checked": len(cloud_targets) if cloud else 0, "health": self.view()}
