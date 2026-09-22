"""The model connectivity light: state resolution, recording during chat/check,
local probing, and cleanup.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FakeLLM


def make(tmp_path, fake=None):
    fake = fake or FakeLLM(default="OK")
    app = create_app(tmp_path / "data", completion_fn=fake)
    return TestClient(app, base_url="http://127.0.0.1"), app.state.store, fake


def health(c):
    return c.get("/api/models-health").json()["health"]


def test_states_off_and_unknown(tmp_path):
    c, store, _ = make(tmp_path)

    def detail(model_id, lang=""):
        url = "/api/models-health" if not lang else f"/api/models-health?lang={lang}"
        return c.get(url).json()["health"][model_id]["detail"]

    h = health(c)
    ds = next(m for m in store.list_models() if m["provider_id"] == "deepseek")
    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")
    assert h[ds["id"]]["state"] == "off" and "API key" in h[ds["id"]]["detail"]    # no key yet
    assert h[ol["id"]]["state"] == "unknown"                                        # usable locally, never checked
    # Why a model is not being called is worked out at request time, so it is also said in the
    # reader's language — not in the language whoever configured the provider happened to use.
    assert detail(ds["id"], "zh") == "未配置 API Key"
    store.update_provider("deepseek", {"api_key": "sk-abcdef123456"})
    assert health(c)[ds["id"]]["state"] == "unknown"
    store.update_settings({"external_calls_enabled": False})
    assert health(c)[ds["id"]]["state"] == "off" and "Outbound calls" in health(c)[ds["id"]]["detail"]
    assert detail(ds["id"], "zh") == "外呼已禁用"
    store.update_model(ol["id"], {"enabled": False})
    assert health(c)[ol["id"]]["detail"] == "Disabled"
    assert detail(ol["id"], "zh") == "已停用"


def test_a_stored_diagnostic_is_rendered_in_the_readers_language(tmp_path):
    """A health record outlives the run that wrote it and is read by whoever opens the page next,
    so it cannot hold the language of whoever pressed "check" — it holds one canonical form, and
    the reader's language is applied when the indicator is drawn."""
    from app import health

    c, store, _ = make(tmp_path)
    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")

    # A record written back when the interface was Chinese.
    store.set_health(ol["id"], "bad", "Ollama 里还没有这个模型,先下载:ollama pull qwen2.5:7b")

    def shown(lang):
        url = "/api/models-health" if lang == "en" else f"/api/models-health?lang={lang}"
        return c.get(url).json()["health"][ol["id"]]["detail"]

    assert shown("en") == "Ollama does not have this model yet; pull it first: ollama pull qwen2.5:7b"
    assert shown("zh") == "Ollama 里还没有这个模型,先下载:ollama pull qwen2.5:7b"

    # A failure detail is the provider's own words plus one of ours; only ours is translated.
    store.set_health(ol["id"], "bad", "AuthenticationError: invalid key —— 密钥无效、过期,或没有权限。")
    assert shown("en") == "AuthenticationError: invalid key — the key is invalid, expired, or lacks permission."
    store.set_health(ol["id"], "bad", "RateLimitError: 请求过于频繁,请稍后再试")
    assert shown("en") == "RateLimitError: 请求过于频繁,请稍后再试", "not ours to translate"


def test_the_local_probe_stores_its_verdict_in_one_language(tmp_path, monkeypatch):
    """The probe runs at startup and on every check, under whatever language is current — often a
    background one, where there is no reader to ask. What it writes outlives that, so it is written
    in one language and rendered in the reader's instead (see health.localize_detail)."""
    c, store, _ = make(tmp_path)
    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")

    def refused(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    real = httpx.AsyncClient
    monkeypatch.setattr("app.health.httpx.AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(refused), **kw))
    h = c.post("/api/models-health/check?lang=zh", json={"cloud": False}).json()["health"]

    assert store.all_health()[ol["id"]]["detail"] == \
        "Ollama is not running (or the address is wrong):ConnectError"
    assert h[ol["id"]]["detail"] == "Ollama 没有在运行(或地址不对):ConnectError"


def test_check_records_ok_and_bad_and_limited(tmp_path):
    fake = FakeLLM(script={
        "deepseek/deepseek-v4-pro": "OK",
        "deepseek/reasoner-x": RuntimeError("AuthenticationError: invalid key sk-secret123456"),
    })
    c, store, _ = make(tmp_path, fake)
    store.update_provider("deepseek", {"api_key": "sk-abcdef123456"})
    store.add_model("deepseek", "reasoner-x")
    store.add_model("deepseek", "slow-one")
    fake.script["deepseek/slow-one"] = RuntimeError("RateLimitError: too many requests, retry after 90 seconds")
    r = c.post("/api/models-health/check", json={"model_ids": ["deepseek/deepseek-v4-pro", "deepseek/reasoner-x", "deepseek/slow-one"]})
    assert r.status_code == 200
    h = r.json()["health"]
    assert h["deepseek/deepseek-v4-pro"]["state"] == "ok"
    assert h["deepseek/reasoner-x"]["state"] == "bad"
    assert "sk-secret123456" not in h["deepseek/reasoner-x"]["detail"]  # the key is scrubbed
    assert h["deepseek/slow-one"]["state"] == "limited"


def test_chat_records_health_but_request_problem_does_not_turn_red(tmp_path):
    from app.router import ModelRouter

    class BadRequestError(Exception):
        pass

    fake = FakeLLM(script={"ollama_chat/": BadRequestError("context length exceeded")})
    c, store, _ = make(tmp_path, fake)
    router: ModelRouter = c.app.state.router
    import asyncio

    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")
    try:
        asyncio.run(router.complete([{"role": "user", "content": "x"}], only=ol["id"]))
    except Exception:
        pass
    assert ol["id"] not in store.all_health()          # a bad request during chat must not turn it red
    fake.script["ollama_chat/"] = "好"
    asyncio.run(router.complete([{"role": "user", "content": "x"}], only=ol["id"]))
    assert store.all_health()[ol["id"]]["status"] == "ok"
    assert store.all_health()[ol["id"]]["source"] == "chat"


def test_reasoning_only_counts_as_connected(tmp_path):
    from types import SimpleNamespace

    class Fake(FakeLLM):
        async def __call__(self, **kw):
            async def gen():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="想…"))])
            return gen()

    c, store, _ = make(tmp_path, Fake())
    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")
    r = c.post("/api/models-health/check", json={"model_ids": [ol["id"]], "cloud": True}).json()
    # local checks only probe, and Ollama is not running here, hence bad; use a
    # cloud model to verify the reasoning-only case
    store.update_provider("deepseek", {"api_key": "sk-abcdef123456"})
    r = c.post("/api/models-health/check", json={"model_ids": ["deepseek/deepseek-v4-pro"]}).json()
    assert r["health"]["deepseek/deepseek-v4-pro"]["state"] == "ok"
    assert "reasoning" in r["health"]["deepseek/deepseek-v4-pro"]["detail"]


def test_local_probe_ollama(tmp_path, monkeypatch):
    c, store, _ = make(tmp_path)
    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": ol["model_name"]}]})

    real = httpx.AsyncClient
    monkeypatch.setattr("app.health.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    h = c.post("/api/models-health/check", json={"cloud": False}).json()["health"]
    assert h[ol["id"]]["state"] == "ok" and "is downloaded" in h[ol["id"]]["detail"]

    def handler2(req):
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr("app.health.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler2), **kw))
    h = c.post("/api/models-health/check", json={"cloud": False}).json()["health"]
    assert h[ol["id"]]["state"] == "bad" and "ollama pull" in h[ol["id"]]["detail"]

    def handler3(req):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("app.health.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler3), **kw))
    h = c.post("/api/models-health/check", json={"cloud": False}).json()["health"]
    assert h[ol["id"]]["state"] == "bad" and "is not running" in h[ol["id"]]["detail"]


def test_changing_key_or_deleting_clears_stale_health(tmp_path):
    c, store, _ = make(tmp_path)
    store.update_provider("deepseek", {"api_key": "sk-old-123456"})
    c.post("/api/models-health/check", json={"model_ids": ["deepseek/deepseek-v4-pro"]})
    assert "deepseek/deepseek-v4-pro" in store.all_health()
    store.update_provider("deepseek", {"api_key": "sk-new-123456"})
    assert "deepseek/deepseek-v4-pro" not in store.all_health()   # a stale result says nothing about a new key
    c.post("/api/models-health/check", json={"model_ids": ["deepseek/deepseek-v4-pro"]})
    store.delete_model("deepseek/deepseek-v4-pro")
    assert "deepseek/deepseek-v4-pro" not in store.all_health()


def test_check_rejects_bad_body(tmp_path):
    c, _, _ = make(tmp_path)
    assert c.post("/api/models-health/check", json={"model_ids": "x"}).status_code == 400
