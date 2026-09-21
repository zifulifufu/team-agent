import pytest

from app.router import AllRoutesFailed
from tests.conftest import FakeLLM


def key_deepseek(store):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})


async def test_no_key_skips_deepseek_and_uses_local(store, make_router):
    fake = FakeLLM()
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b"
    assert r.fallback_from == "deepseek/deepseek-flash"
    assert [(a.model_id, a.status) for a in r.attempts][0] == ("deepseek/deepseek-flash", "skipped")
    assert fake.calls[0][0] == "ollama_chat/qwen2.5:7b"


async def test_deepseek_first_when_configured(store, make_router):
    key_deepseek(store)
    fake = FakeLLM()
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "deepseek/deepseek-flash"
    assert r.fallback_from is None
    assert fake.calls[0][0] == "deepseek/deepseek-flash"


async def test_falls_back_when_deepseek_fails(store, make_router):
    key_deepseek(store)
    fake = FakeLLM({"deepseek/": RuntimeError("401 bad key")})
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b"
    assert r.fallback_from == "deepseek/deepseek-flash"
    assert r.attempts[0].status == "failed" and "401" in r.attempts[0].detail


async def test_external_calls_disabled_never_touches_cloud(store, make_router):
    key_deepseek(store)
    store.update_settings({"external_calls_enabled": False})
    fake = FakeLLM()
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b"
    assert all(not m.startswith("deepseek/") for m, _ in fake.calls)
    assert r.attempts[0].detail == "outbound calls are disabled"


async def test_local_safety_net_appended_when_chain_has_no_local(store, make_router):
    key_deepseek(store)
    store.update_settings({"route_chain": ["deepseek/deepseek-flash"]})
    fake = FakeLLM({"deepseek/": RuntimeError("down")})
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b"


async def test_agent_preferred_model_goes_first(store, make_router):
    key_deepseek(store)
    kimi = store.add_provider_from_preset("moonshot", api_key="sk-kimi-1234567890")
    fake = FakeLLM()
    r = await make_router(fake).complete(
        [{"role": "user", "content": "hi"}], preferred=f"{kimi['id']}/kimi-k3"
    )
    assert r.model_id == "moonshot/kimi-k3"
    assert fake.calls[0][0] == "openai/kimi-k3"
    # 首选失败 -> 回退到 DeepSeek,而不是直接本地
    fake2 = FakeLLM({"openai/": RuntimeError("kimi down")})
    r2 = await make_router(fake2).complete(
        [{"role": "user", "content": "hi"}], preferred="moonshot/kimi-k3"
    )
    assert r2.model_id == "deepseek/deepseek-flash"
    assert r2.fallback_from == "moonshot/kimi-k3"


async def test_partial_stream_failure_resets_then_falls_back(store, make_router):
    key_deepseek(store)
    fake = FakeLLM({"deepseek/": ("partial_then_fail", "半截")}, default="完整回答")
    deltas, resets = [], []

    async def on_delta(d):
        deltas.append(d)

    async def on_reset():
        resets.append(True)
        deltas.clear()

    r = await make_router(fake).complete(
        [{"role": "user", "content": "hi"}], on_delta=on_delta, on_reset=on_reset
    )
    assert resets == [True]
    assert "".join(deltas) == "完整回答" == r.text


async def test_circuit_breaker_skips_failing_model(store, make_router):
    key_deepseek(store)
    fake = FakeLLM({"deepseek/": RuntimeError("down")})
    router = make_router(fake)
    for _ in range(2):
        await router.complete([{"role": "user", "content": "hi"}])
    n = sum(1 for m, _ in fake.calls if m.startswith("deepseek/"))
    assert n == 2
    r = await router.complete([{"role": "user", "content": "hi"}])
    assert sum(1 for m, _ in fake.calls if m.startswith("deepseek/")) == 2  # 未再调用
    assert r.attempts[0].detail.startswith("Tripped")


async def test_all_fail_raises_with_attempts(store, make_router):
    key_deepseek(store)
    fake = FakeLLM(default=RuntimeError("everything down"))
    with pytest.raises(AllRoutesFailed) as ei:
        await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert len(ei.value.attempts) == 2


async def test_empty_reply_counts_as_failure(store, make_router):
    key_deepseek(store)
    fake = FakeLLM({"deepseek/": ""}, default="本地回答")
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b"


async def test_disabled_model_and_provider_are_skipped(store, make_router):
    key_deepseek(store)
    store.update_model("deepseek/deepseek-flash", {"enabled": False})
    r = await make_router(FakeLLM()).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b"
    assert r.attempts[0].detail == "disabled"


async def test_test_model_reports(store, make_router):
    ok = await make_router(FakeLLM(default="OK")).test_model("ollama/qwen2.5:7b")
    assert ok["ok"] and ok["reply"] == "OK"
    bad = await make_router(FakeLLM(default=RuntimeError("nope"))).test_model("ollama/qwen2.5:7b")
    assert not bad["ok"] and "nope" in bad["error"]


# ---------------------------------------------------------------- 限速 / 思考型模型 / 错误信息脱敏
KIMI_429 = RuntimeError(
    "litellm.RateLimitError: RateLimitError: OpenAIException - Your account org-29169032450440bca"
    "df0a8c05f8c1004<ak-fcr1mr8784ni11f6nrpi> request reached organization max RPM: 3, please try again after 1 seconds."
)


def test_short_redacts_secrets_and_adds_hint():
    from app.router import _short, redact

    e = type("RateLimitError", (Exception,), {})(str(KIMI_429))
    out = _short(e)
    assert "org-…" in out and "ak-…" in out and "fcr1mr8784" not in out and "29169032" not in out
    assert out.startswith("RateLimitError: Your account") and "litellm" not in out and "OpenAIException" not in out
    assert "rate-limiting" in out                                                  # 附一句中文解释
    assert redact("Authorization: Bearer abc123def456") == "Authorization: Bearer …"
    assert redact("bad key sk-abcdef1234567890") == "bad key sk-…"


def test_retry_after_only_for_short_rate_limits():
    from app.router import retry_after

    assert retry_after(KIMI_429) == pytest.approx(1.3)
    assert retry_after(RuntimeError("HTTP 429 too many requests")) == pytest.approx(1.8)
    assert retry_after(RuntimeError("rate limit, try again after 60 seconds")) is None       # 等太久:直接回退,不干等
    assert retry_after(RuntimeError("connection reset")) is None


async def test_rate_limit_is_retried_once_then_succeeds(store, make_router, monkeypatch):
    import app.router as rt

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(rt.asyncio, "sleep", fake_sleep)
    state = {"n": 0}

    def flaky(messages):
        state["n"] += 1
        if state["n"] == 1:
            raise KIMI_429
        return "好了"

    r = await make_router(FakeLLM({"ollama_chat/": flaky})).complete([{"role": "user", "content": "hi"}], only="ollama/qwen2.5:7b")
    assert r.text == "好了" and state["n"] == 2 and 1.0 < slept[0] < 2.0
    assert [a.status for a in r.attempts] == ["ok"]


async def test_rate_limit_that_persists_falls_back(store, make_router, monkeypatch):
    import app.router as rt

    async def fake_sleep(s):
        return None

    monkeypatch.setattr(rt.asyncio, "sleep", fake_sleep)
    key_deepseek(store)
    fake = FakeLLM({"deepseek/": KIMI_429}, default="来自本地")
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "ollama/qwen2.5:7b" and r.attempts[0].status == "failed" and "org-…" in r.attempts[0].detail
    assert sum(1 for m, _ in fake.calls if m.startswith("deepseek/")) == 2      # 首次 + 重试一次,不无限重试


def _thinking_llm(reasoning_only: bool):
    from types import SimpleNamespace

    async def fn(**kw):
        async def gen():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="让我想想…"))])
            if not reasoning_only:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="OK", reasoning_content=None))])
        return gen()
    return fn


async def test_reasoning_only_reply_is_reported_clearly(store, make_router):
    router = make_router(_thinking_llm(True))
    with pytest.raises(Exception) as e:
        await router.complete([{"role": "user", "content": "hi"}], only="ollama/qwen2.5:7b")
    assert "ReasoningOnlyError" in e.value.attempts[-1].detail and "produced only its reasoning" in e.value.attempts[-1].detail
    # 测试按钮:连接、密钥都没问题,不应显示成失败
    t = await router.test_model("ollama/qwen2.5:7b")
    assert t["ok"] is True and "reasoning model" in t["reply"]
    assert (await make_router(_thinking_llm(False)).test_model("ollama/qwen2.5:7b"))["reply"] == "OK"
