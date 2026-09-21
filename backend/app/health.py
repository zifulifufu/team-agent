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
import time
from typing import Any

import httpx

from .router import ModelRouter, has_credentials
from .store import Store

CHECK_CONCURRENCY = 3
STALE_SECONDS = 24 * 3600  # results older than a day are still shown, but flagged as "older"


def static_state(model: dict, provider: dict, external_ok: bool) -> tuple[str, str] | None:
    """Cases where "will not be called right now" can be decided without sending a request;
returns (off, reason), otherwise None."""
    if not model["enabled"] or not provider["enabled"]:
        return "off", "已停用"
    if not provider["is_local"] and not external_ok:
        return "off", "外呼已禁用"
    if not has_credentials(provider):
        return "off", "未配置 API Key"
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
            out[m["id"]] = {"state": rec["status"], "detail": rec["detail"], "latency_ms": rec["latency_ms"],
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
                    why = i18n.pick_now("Ollama is not running (or the address is wrong)", "Ollama 没有在运行(或地址不对)") if p["kind"] == "ollama" else i18n.pick_now(f"Could not reach {base}", f"连不上 {base}")
                    for m in models:
                        self.store.set_health(m["id"], "bad", f"{why}:{type(e).__name__}", 0, "probe")
                        n += 1
                    continue
                ms = int((time.time() - t0) * 1000)
                for m in models:
                    if installed is not None and not _installed(m["model_name"], installed):
                        self.store.set_health(m["id"], "bad", i18n.pick_now(f"Ollama does not have this model yet; pull it first: ollama pull {m['model_name']}", f"Ollama 里还没有这个模型,先下载:ollama pull {m['model_name']}"), ms, "probe")
                    else:
                        note = i18n.pick_now("Ollama is running and the model is downloaded (nothing was actually called; the first reply loads it into memory)", "Ollama 在运行,模型已下载(没有实际调用,首次回复要加载进内存)") if installed is not None else i18n.pick_now("The service is reachable", "服务可连接")
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
