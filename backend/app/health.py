"""模型连通指示灯。

每个模型一个状态:
  ok       绿   最近一次调用/检测成功
  limited  黄   被限速或额度不足(等一会儿就好)
  bad      红   连不上、密钥无效、模型 ID 不对、本地服务没启动或模型没下载
  unknown  空心  可以用,但还没检测过(云端模型不会自动检测,避免悄悄花你的额度)
  off      灰   现在根本不会被调用:已停用 / 没填 Key / 外呼开关关着

结果来源:手动「检测」(发一条极短的请求)、每次真实聊天顺带记录、本地服务探测(不花 token)。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .router import ModelRouter, has_credentials
from .store import Store

CHECK_CONCURRENCY = 3
STALE_SECONDS = 24 * 3600  # 超过一天的结果仍显示,但标注「较旧」


def static_state(model: dict, provider: dict, external_ok: bool) -> tuple[str, str] | None:
    """不发请求就能判断「现在不会被调用」的情况;返回 (off, 原因),否则 None。"""
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
                out[m["id"]] = {"state": "limited", "detail": "连续失败,暂时熔断,稍后自动重试", "latency_ms": 0,
                                "checked_at": rec["checked_at"] if rec else 0, "source": rec["source"] if rec else "", "stale": False}
                continue
            if not rec:
                out[m["id"]] = {"state": "unknown", "detail": "还没检测过", "latency_ms": 0, "checked_at": 0, "source": "", "stale": False}
                continue
            out[m["id"]] = {"state": rec["status"], "detail": rec["detail"], "latency_ms": rec["latency_ms"],
                            "checked_at": rec["checked_at"], "source": rec["source"],
                            "stale": now - rec["checked_at"] > STALE_SECONDS}
        return out

    # ------------------------------------------------------------ local probe
    async def probe_local(self) -> int:
        """探测本地服务:Ollama 看 /api/tags(在不在跑、模型下没下载),其他自建服务看 /models 能不能连上。不消耗 token。"""
        providers = {p["id"]: p for p in self.store.list_providers()}
        by_provider: dict[str, list[dict]] = {}
        for m in self.store.list_models():
            p = providers[m["provider_id"]]
            if p["is_local"] and p["enabled"] and m["enabled"]:
                by_provider.setdefault(p["id"], []).append(m)
        n = 0
        # 只探测本机/局域网服务,显式绕开系统代理(否则开着 Clash 时会把 127.0.0.1 的请求也发给代理)
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
                    why = "Ollama 没有在运行(或地址不对)" if p["kind"] == "ollama" else f"连不上 {base}"
                    for m in models:
                        self.store.set_health(m["id"], "bad", f"{why}:{type(e).__name__}", 0, "probe")
                        n += 1
                    continue
                ms = int((time.time() - t0) * 1000)
                for m in models:
                    if installed is not None and not _installed(m["model_name"], installed):
                        self.store.set_health(m["id"], "bad", f"Ollama 里还没有这个模型,先下载:ollama pull {m['model_name']}", ms, "probe")
                    else:
                        note = "Ollama 在运行,模型已下载(没有实际调用,首次回复要加载进内存)" if installed is not None else "服务可连接"
                        self.store.set_health(m["id"], "ok", note, ms, "probe")
                    n += 1
        return n

    # ------------------------------------------------------------ real check
    async def check(self, model_ids: list[str] | None = None, *, cloud: bool = True) -> dict[str, Any]:
        """检测指定模型(默认全部现在会被调用的)。本地模型只做探测;云端模型发一条极短的请求(花几个 token)。"""
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
                    await self.router.test_model(m["id"])  # 结果由 router 记入健康表

            await asyncio.gather(*(one(m) for m in cloud_targets))
        return {"checked": len(cloud_targets) if cloud else 0, "health": self.view()}
