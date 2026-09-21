"""模型路由层。

规则(按顺序):
  1. 候选链 = [agent 指定模型] + 设置里的 route_chain(默认: DeepSeek -> 本地 Ollama),去重。
  2. 「外呼被禁用」时,所有非本地服务商直接跳过。
  3. 未配置 API Key / 已停用 / 熔断中的模型跳过。
  4. 逐个尝试,任何异常(鉴权、网络、超时、限流、空回复)都回退到下一个。
  5. 链里没有本地模型时,自动把一个已启用的本地模型追加到末尾作为兜底。

底层调用统一走 LiteLLM,所以增删服务商/模型只是改数据库,不需要改代码。
"""

from __future__ import annotations

from . import i18n

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import strengths as strength_lib
from .store import Store

ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

DeltaCb = Callable[[str], Awaitable[None]]
ResetCb = Callable[[], Awaitable[None]]


class AllRoutesFailed(Exception):
    def __init__(self, attempts: list["Attempt"]):
        self.attempts = attempts
        super().__init__(i18n.pick_now("No model is available: ", "所有模型均不可用: ") + "; ".join(f"{a.model_id}: {a.detail}" for a in attempts))


@dataclass
class Attempt:
    model_id: str
    status: str  # ok | failed | skipped
    detail: str = ""
    latency_ms: int = 0
    # A stable code for *why* a model was skipped: "" | "offline" | "tripped".
    # `detail` is shown to the user and therefore follows the request language, so clients
    # must branch on this instead of matching the message text.
    reason: str = ""

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "status": self.status, "detail": self.detail,
                "latency_ms": self.latency_ms, "reason": self.reason}


@dataclass
class RouteResult:
    text: str
    model_id: str
    first_choice: str | None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def fallback_from(self) -> str | None:
        return self.first_choice if self.first_choice and self.first_choice != self.model_id else None


def litellm_params(provider: dict, model: dict) -> dict[str, Any]:
    """把「服务商 + 模型」映射成 LiteLLM 的调用参数。"""
    kind, name = provider["kind"], model["model_name"]
    params: dict[str, Any] = {}
    if kind == "deepseek":
        params["model"] = f"deepseek/{name}"
        if provider["base_url"]:
            params["api_base"] = provider["base_url"]
    elif kind == "anthropic":
        params["model"] = f"anthropic/{name}"
        if provider["base_url"]:
            params["api_base"] = provider["base_url"]
    elif kind == "gemini":
        params["model"] = f"gemini/{name}"
        if provider["base_url"]:
            params["api_base"] = provider["base_url"]
    elif kind == "ollama":
        params["model"] = f"ollama_chat/{name}"
        params["api_base"] = provider["base_url"] or "http://127.0.0.1:11434"
    else:  # openai_compatible
        params["model"] = f"openai/{name}"
        params["api_base"] = provider["base_url"]
    if kind != "ollama":
        # 关掉 SDK 自带的静默重试:它遇到 429 会连发好几次,对「每分钟只允许 3 次」的账号是火上浇油。
        # 限速由路由层统一处理(等服务商说的秒数后重试一次,不行就回退)。
        params["max_retries"] = 0
    key = provider["api_key"]
    if key:
        params["api_key"] = key
    elif kind == "openai_compatible":
        params["api_key"] = "sk-none"  # 本地 OpenAI 兼容服务通常不校验 key,但 SDK 要求非空
    return params


def has_credentials(provider: dict) -> bool:
    if provider["is_local"] or provider["api_key"]:
        return True
    env = ENV_KEYS.get(provider["kind"])
    return bool(env and os.environ.get(env))


async def _default_completion(**kwargs: Any) -> Any:
    import litellm  # 延迟导入:litellm 导入较慢

    litellm.drop_params = True
    return await litellm.acompletion(**kwargs)


class ModelRouter:
    def __init__(self, store: Store, completion_fn: Callable[..., Awaitable[Any]] | None = None):
        self.store = store
        self._fn = completion_fn or _default_completion
        self._circuit: dict[str, tuple[int, float]] = {}  # model_id -> (连续失败数, 熔断截止时间)

    # ------------------------------------------------------------------ chain
    def usable_models(self) -> list[dict]:
        """现在真的能调用的模型(已启用、有密钥、外呼开关允许)。"""
        cfg = self.store.get_settings()
        external_ok = bool(cfg["external_calls_enabled"])
        providers = {p["id"]: p for p in self.store.list_providers()}
        out = []
        for m in self.store.list_models():
            p = providers[m["provider_id"]]
            if m["enabled"] and p["enabled"] and (p["is_local"] or external_ok) and has_credentials(p):
                out.append(m)
        return out

    def rank_by_tags(self, tags: list[str], limit: int = 5) -> list[dict]:
        """按强项给可用模型排序。同分时:云端优先于本地(本地留作兜底),再按设置里优先级链的顺序。
        除非明确要「本地」强项,或者根本没有可用的云端模型,否则不会把本地小模型排在云端前面。
        标签会先规范化:旧版用中文标签名传来的值("代码")要照样认(见 strengths.ALIASES)。"""
        tags = strength_lib.clean_tags(tags)
        if not tags:
            return []
        chain = list(self.store.get_settings()["route_chain"])
        pool = self.usable_models()
        if "local" not in tags and any(not m["is_local"] for m in pool):
            pool = [m for m in pool if not m["is_local"]]
        ranked = []
        for m in pool:
            sc = strength_lib.score(m["strengths"], tags)
            if sc > 0:
                pos = chain.index(m["id"]) if m["id"] in chain else len(chain)
                ranked.append((-sc, m["is_local"], pos, m["id"], {**m, "score": round(sc, 2)}))
        ranked.sort(key=lambda x: x[:4])
        return [r[4] for r in ranked[:limit]]

    def build_chain(self, preferred: str | None = None, tags: list[str] | None = None) -> tuple[list[dict], list[Attempt]]:
        """返回 (可尝试的候选模型, 被跳过的记录)。
        preferred 是成员手动指定的模型;没指定但有 tags(岗位需要的强项)时,按强项挑 2 个最合适的排在前面。"""
        cfg = self.store.get_settings()
        external_ok = bool(cfg["external_calls_enabled"])
        models = {m["id"]: m for m in self.store.list_models()}
        providers = {p["id"]: p for p in self.store.list_providers()}

        front: list[str] = [preferred] if preferred else [m["id"] for m in self.rank_by_tags(tags or [], 2)]
        ids: list[str] = []
        for mid in front + list(cfg["route_chain"]):
            if mid and mid not in ids:
                ids.append(mid)

        candidates: list[dict] = []
        skipped: list[Attempt] = []

        def consider(mid: str) -> None:
            m = models.get(mid)
            if not m:
                skipped.append(Attempt(mid, "skipped", i18n.pick_now("Model not found", "模型不存在")))
                return
            p = providers[m["provider_id"]]
            if not m["enabled"] or not p["enabled"]:
                skipped.append(Attempt(mid, "skipped", i18n.pick_now("disabled", "已停用")))
            elif not p["is_local"] and not external_ok:
                skipped.append(Attempt(mid, "skipped",
                                    i18n.pick_now("outbound calls are disabled", "外呼已禁用"),
                                    reason="offline"))
            elif not has_credentials(p):
                skipped.append(Attempt(mid, "skipped", i18n.pick_now("no API key configured", "未配置 API Key")))
            else:
                candidates.append({**m, "_provider": p})

        for mid in ids:
            consider(mid)

        if not any(c["_provider"]["is_local"] for c in candidates):
            # 兜底:Ollama(小模型、最可靠)排在自建的大模型服务前面;都不可用才轮到后者
            locals_ = [
                m for m in models.values()
                if providers[m["provider_id"]]["is_local"] and providers[m["provider_id"]]["enabled"]
                and m["enabled"] and m["id"] not in ids
            ]
            locals_.sort(key=lambda m: providers[m["provider_id"]]["kind"] != "ollama")
            for m in locals_:
                consider(m["id"])
        return candidates, skipped

    def resolve(self, preferred: str | None = None, tags: list[str] | None = None) -> dict | None:
        """这个成员现在实际会用哪个模型(不发请求),给界面和分工表展示用。"""
        cands, _ = self.build_chain(preferred, tags)
        return cands[0] if cands else None

    # ---------------------------------------------------------------- circuit
    def _is_open(self, mid: str) -> bool:
        _, until = self._circuit.get(mid, (0, 0.0))
        if until and until <= time.time():
            self._circuit.pop(mid, None)      # 冷却结束:失败计数一起清零,重新攒够阈值才会再次熔断
            return False
        return until > time.time()

    def _record(self, mid: str, ok: bool) -> None:
        cfg = self.store.get_settings()
        if ok:
            self._circuit.pop(mid, None)
            return
        n, _ = self._circuit.get(mid, (0, 0.0))
        n += 1
        until = time.time() + cfg["circuit_cooldown"] if n >= cfg["circuit_threshold"] else 0.0
        self._circuit[mid] = (n, until)

    def _note_health(self, mid: str, status: str, detail: str, latency_ms: int, source: str) -> None:
        """每次真实调用顺带记下结果,指示灯不用额外花 token 就能保持新鲜。"""
        try:
            self.store.set_health(mid, status, detail, latency_ms, source)
        except Exception:  # noqa: BLE001 — 记录失败不能影响聊天
            pass

    def circuit_open(self, mid: str) -> bool:
        return self._is_open(mid)

    def reset_circuit(self) -> None:
        self._circuit.clear()

    # ------------------------------------------------------------------- call
    async def _stream_one(
        self, params: dict, messages: list[dict], timeout: float,
        on_delta: DeltaCb | None, extra: dict[str, Any],
    ) -> str:
        resp = await asyncio.wait_for(
            self._fn(messages=messages, stream=True, timeout=timeout, **params, **extra), timeout
        )
        parts: list[str] = []
        thinking = False
        it = resp.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout)
            except StopAsyncIteration:
                break
            try:
                d = chunk.choices[0].delta
                delta = d.content
                thinking = thinking or bool(getattr(d, "reasoning_content", None))
            except (AttributeError, IndexError):
                delta = None
            if delta:
                parts.append(delta)
                if on_delta:
                    await on_delta(delta)
        text = "".join(parts).strip()
        if not text:
            if thinking:
                # 思考型模型(Kimi K2.x、DeepSeek-R1 等)把 token 都花在思考上、没来得及写正文
                raise ReasoningOnlyError(i18n.pick_now("The model produced only its reasoning and no answer (it may have been cut off by max_tokens)", "模型只输出了思考过程,没有正文(可能被 max_tokens 截断)"))
            raise RuntimeError(i18n.pick_now("The model returned nothing", "模型返回了空内容"))
        return text

    async def complete(
        self, messages: list[dict], preferred: str | None = None, *,
        only: str | None = None, on_delta: DeltaCb | None = None,
        on_reset: ResetCb | None = None, tags: list[str] | None = None,
        source: str = "chat", **extra: Any,
    ) -> RouteResult:
        cfg = self.store.get_settings()
        timeout = float(cfg["request_timeout"])
        if only:
            m = self.store.get_model(only)
            p = self.store.get_provider(m["provider_id"]) if m else None
            if not m or not p:
                raise AllRoutesFailed([Attempt(only, "failed", i18n.pick_now("Model not found", "模型不存在"))])
            if not p["is_local"] and not cfg["external_calls_enabled"]:   # 手动检测也不能绕过「禁止外呼」
                raise AllRoutesFailed([Attempt(only, "skipped", i18n.pick_now("Outbound calls are disabled (Allow outbound calls is off in Settings), so no request was sent", "外呼已禁用(设置里的「允许外呼」是关的),没有发出请求"))])
            if not has_credentials(p):
                raise AllRoutesFailed([Attempt(only, "skipped", i18n.pick_now("no API key configured", "未配置 API Key"))])
            candidates, attempts = [{**m, "_provider": p}], []
        else:
            candidates, attempts = self.build_chain(preferred, tags)
        first_choice = (preferred or (candidates[0]["id"] if tags and candidates else None)
                        or (cfg["route_chain"][0] if cfg["route_chain"] else None))

        for i, cand in enumerate(candidates):
            mid = cand["id"]
            is_last = i == len(candidates) - 1
            if not only and not is_last and self._is_open(mid):
                attempts.append(Attempt(mid, "skipped",
                                    i18n.pick_now("Tripped and cooling down; retry shortly", "熔断中,稍后重试"),
                                    reason="tripped"))
                continue

            emitted = False

            async def _relay(d: str) -> None:
                nonlocal emitted
                emitted = True
                if on_delta:
                    await on_delta(d)

            t0 = time.time()
            try:
                try:
                    text = await self._stream_one(
                        litellm_params(cand["_provider"], cand), messages, timeout, _relay, extra
                    )
                except Exception as e:  # noqa: BLE001
                    wait = retry_after(e)
                    if wait is None or emitted:
                        raise
                    # 服务商说「N 秒后再试」(每分钟请求数太低的账号常见):等一下再试一次,再不行才回退
                    await asyncio.sleep(wait)
                    text = await self._stream_one(
                        litellm_params(cand["_provider"], cand), messages, timeout, _relay, extra
                    )
            except Exception as e:  # noqa: BLE001 — 任何失败都应触发回退
                if not (source == "chat" and is_request_problem(e)):   # 请求本身的问题(上下文太长等)不算模型故障,不该熔断
                    self._record(mid, False)
                detail = _short(e)
                attempts.append(Attempt(mid, "failed", detail, int((time.time() - t0) * 1000)))
                if isinstance(e, ReasoningOnlyError):  # 连得通,只是思考型模型没写出正文
                    self._note_health(mid, "ok", i18n.pick_now("Connected (a reasoning model; within the quota it returned only its reasoning)", "连接正常(思考型模型,额度内只返回了思考过程)"), attempts[-1].latency_ms, source)
                elif source != "chat" or not is_request_problem(e):
                    self._note_health(mid, classify_failure(e), detail, attempts[-1].latency_ms, source)
                if emitted and on_reset:
                    await on_reset()
                continue
            self._record(mid, True)
            attempts.append(Attempt(mid, "ok", "", int((time.time() - t0) * 1000)))
            self._note_health(mid, "ok", "", attempts[-1].latency_ms, source)
            return RouteResult(text, mid, first_choice, attempts)

        raise AllRoutesFailed(attempts)

    async def test_model(self, model_id: str) -> dict:
        try:
            r = await self.complete(
                [{"role": "user", "content": i18n.pick_now("Reply with exactly the two letters: OK.", "只回复 OK 两个字母。")}], only=model_id, max_tokens=256, source="test"
            )
            return {"ok": True, "latency_ms": r.attempts[-1].latency_ms, "reply": r.text[:80]}
        except AllRoutesFailed as e:
            last = e.attempts[-1] if e.attempts else None
            if last and last.detail.startswith("ReasoningOnlyError"):
                # 连接、密钥、模型 ID 都没问题;只是思考型模型在测试用的小额度里没写出正文
                return {"ok": True, "latency_ms": last.latency_ms, "reply": i18n.pick_now("(reasoning model: it returned only its reasoning; the connection is fine)", "(思考型模型:只返回了思考过程,连接正常)")}
            return {"ok": False, "error": last.detail if last else i18n.pick_now("unknown error", "未知错误")}


class ReasoningOnlyError(RuntimeError):
    pass


RATE_RE = re.compile(r"rate.?limit|too many requests|max rpm|\b429\b", re.I)
# 服务商建议的等待时间:"retry after 20s"、"in 20ms"、"try again in 2.5 seconds"、"请 3 秒后重试"、"3秒后"
AFTER_RES = [
    re.compile(r"(?:after|in)\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?)\b", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*(毫秒|秒)\s*(?:钟)?\s*(?:后|之后|内)"),  # i18n-keep: parses Chinese retry-after phrasing from upstream providers
    re.compile(r"请\s*(\d+(?:\.\d+)?)\s*(毫秒|秒)"),  # i18n-keep: parses Chinese retry-after phrasing from upstream providers
]
MAX_RETRY_WAIT = 5.0


def classify_failure(e: BaseException) -> str:
    """limited = 限速/额度(等一会儿就好);bad = 连不上、密钥不对、模型 ID 不对等需要人处理的问题。"""
    return "limited" if RATE_RE.search(f"{type(e).__name__} {e}") else "bad"


def is_request_problem(e: BaseException) -> bool:
    """这条请求本身有问题(上下文超长、内容被拒等),不代表模型连不通,聊天时不据此把指示灯标红。"""
    n = type(e).__name__
    return any(k in n for k in ("BadRequest", "ContextWindow", "ContentPolicy", "UnprocessableEntity"))


def retry_after(e: BaseException) -> float | None:
    """限速类错误且服务商建议的等待时间不长(≤5 秒)时,返回该等多久;否则不重试。"""
    text = f"{type(e).__name__} {e}"
    if not RATE_RE.search(text):
        return None
    wait = 1.5
    for rx in AFTER_RES:
        m = rx.search(text)
        if m:
            wait = float(m.group(1))
            if m.group(2).lower() in ("ms", "毫秒") or m.group(2).lower().startswith("millisecond"):
                wait /= 1000
            break
    return min(wait + 0.3, MAX_RETRY_WAIT) if wait <= MAX_RETRY_WAIT else None


_SECRET_RES = [
    (re.compile(r"\b(sk|ak|pk|key|tok)-[A-Za-z0-9_\-]{6,}"), r"\1-…"),  # i18n-keep: redaction placeholder; U+2026 ellipsis is not Chinese
    (re.compile(r"\borg-[A-Za-z0-9]{8,}"), "org-…"),  # i18n-keep: redaction placeholder; U+2026 ellipsis is not Chinese
    (re.compile(r"(Bearer\s+)\S+", re.I), r"\1…"),  # i18n-keep: redaction placeholder; U+2026 ellipsis is not Chinese
    (re.compile(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-]{6,}", re.I), r"\1…"),  # i18n-keep: redaction placeholder; U+2026 ellipsis is not Chinese
]
_HINTS = [
    ("RateLimit", " — the provider is rate-limiting you: most likely the account's per-minute request quota is too low (common on new or free plans). Wait a few seconds and retry, or raise the quota in the provider's console.", " —— 服务商限速了:多半是账号的每分钟请求数/额度太低(新账号或免费档常见),稍等几秒再试,或到服务商后台提高额度。"),
    ("Authentication", " — the key is invalid, expired, or lacks permission.", " —— 密钥无效、过期,或没有权限。"),
    ("PermissionDenied", " — this key is not allowed to use that model.", " —— 这个密钥没有权限使用该模型。"),
    ("NotFound", " — the provider has no such model ID, or your account cannot use it.", " —— 服务商那边没有这个模型 ID,或你的账号无权使用。"),
    ("Timeout", " — the request timed out.", " —— 请求超时。"),
    ("APIConnection", " — cannot reach the provider; check the network and the API base URL.", " —— 连接不上服务商,检查网络和 API 地址。"),
]


def redact(text: str) -> str:
    """错误信息里常带有服务商回显的密钥片段、组织 ID,展示或存库前先抹掉。"""
    for rx, rep in _SECRET_RES:
        text = rx.sub(rep, text)
    return text


def _short(e: BaseException) -> str:
    name = type(e).__name__
    msg = re.sub(r"litellm\.\w+: |\w*Exception - ", "", str(e)).replace("\n", " ")
    msg = re.sub(rf"^(?:{re.escape(name)}: )+", "", msg)
    hint = next((i18n.pick_now(en, zh) for k, en, zh in _HINTS if k in name), "")
    return (redact(f"{name}: {msg}")[:300] + hint)
