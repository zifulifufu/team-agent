"""模型连通指示灯:状态判定、聊天/检测顺带记录、本地探测、清理。"""

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
    h = health(c)
    ds = next(m for m in store.list_models() if m["provider_id"] == "deepseek")
    ol = next(m for m in store.list_models() if m["provider_id"] == "ollama")
    assert h[ds["id"]]["state"] == "off" and "API Key" in h[ds["id"]]["detail"]  # 没填 key
    assert h[ol["id"]]["state"] == "unknown"                                        # 本地能用但没检测过
    store.update_provider("deepseek", {"api_key": "sk-abcdef123456"})
    assert health(c)[ds["id"]]["state"] == "unknown"
    store.update_settings({"external_calls_enabled": False})
    assert health(c)[ds["id"]]["state"] == "off" and "外呼" in health(c)[ds["id"]]["detail"]
    store.update_model(ol["id"], {"enabled": False})
    assert health(c)[ol["id"]]["detail"] == "已停用"


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
    assert "sk-secret123456" not in h["deepseek/reasoner-x"]["detail"]  # 密钥被抹掉
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
    assert ol["id"] not in store.all_health()          # 聊天时的「请求有问题」不标红
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
    # 本地模型检测只做探测,这里 Ollama 没运行 → bad;用云端模型验证思考型
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
    assert h[ol["id"]]["state"] == "ok" and "已下载" in h[ol["id"]]["detail"]

    def handler2(req):
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr("app.health.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler2), **kw))
    h = c.post("/api/models-health/check", json={"cloud": False}).json()["health"]
    assert h[ol["id"]]["state"] == "bad" and "ollama pull" in h[ol["id"]]["detail"]

    def handler3(req):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("app.health.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler3), **kw))
    h = c.post("/api/models-health/check", json={"cloud": False}).json()["health"]
    assert h[ol["id"]]["state"] == "bad" and "没有在运行" in h[ol["id"]]["detail"]


def test_changing_key_or_deleting_clears_stale_health(tmp_path):
    c, store, _ = make(tmp_path)
    store.update_provider("deepseek", {"api_key": "sk-old-123456"})
    c.post("/api/models-health/check", json={"model_ids": ["deepseek/deepseek-v4-pro"]})
    assert "deepseek/deepseek-v4-pro" in store.all_health()
    store.update_provider("deepseek", {"api_key": "sk-new-123456"})
    assert "deepseek/deepseek-v4-pro" not in store.all_health()   # 旧结果对新密钥没意义
    c.post("/api/models-health/check", json={"model_ids": ["deepseek/deepseek-v4-pro"]})
    store.delete_model("deepseek/deepseek-v4-pro")
    assert "deepseek/deepseek-v4-pro" not in store.all_health()


def test_check_rejects_bad_body(tmp_path):
    c, _, _ = make(tmp_path)
    assert c.post("/api/models-health/check", json={"model_ids": "x"}).status_code == 400
