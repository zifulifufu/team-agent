"""获取模型列表 / 使用统计 / 系统信息 / 数据导出。服务商用本机真实 HTTP 假服务模拟。"""
import sqlite3
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FakeLLM
from tests.fakes import FakeServer, free_port


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM())
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.store = app.state.store
        yield c


def openai_models(require_key: str | None = None):
    app = FastAPI()
    app.state.seen = []

    @app.get("/v1/models")
    async def models(request: Request):
        app.state.seen.append(request.headers.get("authorization"))
        if require_key and request.headers.get("authorization") != f"Bearer {require_key}":
            return JSONResponse({"error": "bad key"}, status_code=401)
        return {"object": "list", "data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-flash"}, {"id": "deepseek-flash"}]}

    return app


def test_fetch_models_openai_compatible_marks_added(client):
    with FakeServer(openai_models("sk-secret-123456")) as srv:
        client.post("/api/providers", json={"name": "gw", "kind": "openai_compatible",
                                            "base_url": srv.url + "/v1", "api_key": "sk-secret-123456"})
        pid = next(p["id"] for p in client.get("/api/providers").json() if p["name"] == "gw")
        client.post(f"/api/providers/{pid}/models", json={"model_name": "deepseek-flash"})
        r = client.post(f"/api/providers/{pid}/fetch-models")
        assert r.status_code == 200
        assert r.json()["models"] == [{"id": "deepseek-flash", "added": True},
                                      {"id": "deepseek-v4-pro", "added": False}]  # 去重 + 排序
        assert srv.server.config.app.state.seen == ["Bearer sk-secret-123456"]


def test_fetch_models_bad_key_message_has_no_secret(client):
    with FakeServer(openai_models("right-key")) as srv:
        client.post("/api/providers", json={"name": "gw", "kind": "openai_compatible",
                                            "base_url": srv.url + "/v1", "api_key": "wrong-key-9999"})
        pid = next(p["id"] for p in client.get("/api/providers").json() if p["name"] == "gw")
        r = client.post(f"/api/providers/{pid}/fetch-models")
        assert r.status_code == 502 and "鉴权失败" in r.json()["detail"]
        assert "wrong-key-9999" not in r.text


def test_fetch_models_unreachable_and_missing_config(client):
    client.post("/api/providers", json={"name": "dead", "kind": "openai_compatible",
                                        "base_url": f"http://127.0.0.1:{free_port()}/v1"})
    client.post("/api/providers", json={"name": "nobase", "kind": "openai_compatible"})
    by = {p["name"]: p["id"] for p in client.get("/api/providers").json()}
    r = client.post(f"/api/providers/{by['dead']}/fetch-models")
    assert r.status_code == 502 and "无法连接" in r.json()["detail"]
    r = client.post(f"/api/providers/{by['nobase']}/fetch-models")
    assert r.status_code == 502 and "API 地址" in r.json()["detail"]
    r = client.post("/api/providers/deepseek/fetch-models")  # 没填 Key(且环境变量未设置)
    assert r.status_code == 502 and "API Key" in r.json()["detail"]


def test_fetch_models_ollama_and_local_still_allowed_offline(client):
    app = FastAPI()

    @app.get("/api/tags")
    async def tags():
        return {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.2:3b"}]}

    with FakeServer(app) as srv:
        client.patch("/api/providers/ollama", json={"base_url": srv.url})
        client.put("/api/settings", json={"external_calls_enabled": False})
        r = client.post("/api/providers/ollama/fetch-models")
        assert [m["id"] for m in r.json()["models"]] == ["llama3.2:3b", "qwen2.5:7b"]
        assert r.json()["models"][1]["added"] is True  # 种子数据里已有 qwen2.5:7b


def test_fetch_models_blocked_for_cloud_when_external_calls_disabled(client):
    with FakeServer(openai_models()) as srv:
        client.post("/api/providers", json={"name": "gw", "kind": "openai_compatible", "base_url": srv.url + "/v1"})
        pid = next(p["id"] for p in client.get("/api/providers").json() if p["name"] == "gw")
        client.put("/api/settings", json={"external_calls_enabled": False})
        r = client.post(f"/api/providers/{pid}/fetch-models")
        assert r.status_code == 403
        assert srv.server.config.app.state.seen == []  # 根本没有发出请求


def test_fetch_models_anthropic_and_gemini_protocols(client):
    app = FastAPI()
    app.state.headers = {}

    @app.get("/v1/models")
    async def a(request: Request):
        app.state.headers["anthropic"] = (request.headers.get("x-api-key"), request.headers.get("anthropic-version"))
        return {"data": [{"id": "claude-sonnet-x"}, {"id": "claude-haiku-x"}]}

    @app.get("/v1beta/models")
    async def g(request: Request):
        app.state.headers["gemini"] = (request.headers.get("x-goog-api-key"), dict(request.query_params))
        return {"models": [
            {"name": "models/gemini-x-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-x", "supportedGenerationMethods": ["embedContent"]},
        ]}

    with FakeServer(app) as srv:
        client.post("/api/providers", json={"name": "cl", "kind": "anthropic", "base_url": srv.url, "api_key": "ak-1"})
        client.post("/api/providers", json={"name": "ge", "kind": "gemini", "base_url": srv.url, "api_key": "gk-2"})
        by = {p["name"]: p["id"] for p in client.get("/api/providers").json()}
        assert [m["id"] for m in client.post(f"/api/providers/{by['cl']}/fetch-models").json()["models"]] == [
            "claude-haiku-x", "claude-sonnet-x"]
        assert [m["id"] for m in client.post(f"/api/providers/{by['ge']}/fetch-models").json()["models"]] == [
            "gemini-x-flash"]  # 过滤掉 embedding,并去掉 models/ 前缀
        h = app.state.headers
        assert h["anthropic"] == ("ak-1", "2023-06-01")
        assert h["gemini"][0] == "gk-2" and "key" not in h["gemini"][1]  # Key 只走请求头,不进 URL


def test_batch_add_models_dedupes(client):
    r = client.post("/api/providers/deepseek/models/batch", json={"model_names": ["a", " b ", "a", ""]})
    assert [m["id"] for m in r.json()] == ["deepseek/a", "deepseek/b"]
    assert client.post("/api/providers/nope/models/batch", json={"model_names": ["x"]}).status_code == 404


# --------------------------------------------------------------------- stats
def _agent_msg(store, gid, model_id, *, ts, fallback_from=None, latency=100):
    m = store.add_message(gid, "agent", None, "文案", "x", model_id=model_id, fallback_from=fallback_from,
                          meta={"attempts": [{"model_id": model_id, "status": "ok", "detail": "", "latency_ms": latency}]})
    store._x("UPDATE messages SET created_at=? WHERE id=?", (ts, m["id"]))


def test_stats_counts_days_models_fallbacks(client):
    st = client.store
    gid = client.get("/api/groups").json()[0]["id"]
    now = time.time()
    _agent_msg(st, gid, "deepseek/deepseek-flash", ts=now, latency=200)
    _agent_msg(st, gid, "deepseek/deepseek-flash", ts=now, latency=400)
    _agent_msg(st, gid, "ollama/qwen2.5:7b", ts=now - 86400, fallback_from="deepseek/deepseek-flash", latency=900)
    _agent_msg(st, gid, "deepseek/deepseek-flash", ts=now - 40 * 86400)  # 超出统计窗口
    s = client.get("/api/stats?days=7").json()
    assert s["days"] == 7 and len(s["by_day"]) == 7
    assert s["total_requests"] == 3 and s["fallbacks"] == 1 and s["local_calls"] == 1
    assert s["avg_latency_ms"] == round((200 + 400 + 900) / 3)
    top = s["by_model"][0]
    assert top["model_id"] == "deepseek/deepseek-flash" and top["count"] == 2 and top["avg_latency_ms"] == 300
    assert s["by_model"][1]["is_local"] is True
    assert s["by_day"][-1]["count"] == 2 and s["by_day"][-2]["count"] == 1 and s["by_day"][-2]["fallbacks"] == 1
    assert sum(d["count"] for d in s["by_day"]) == 3


def test_stats_empty_and_clamped(client):
    s = client.get("/api/stats?days=9999").json()
    assert s["days"] == 90 and s["total_requests"] == 0 and s["avg_latency_ms"] is None and s["by_model"] == []


# ------------------------------------------------------------ system / data
def test_system_info(client):
    s = client.get("/api/system").json()
    assert s["litellm"] and s["python"] and s["app_version"] and s["data_dir"].endswith("data")
    assert s["db_bytes"] > 0 and s["auth"] is False


def test_export_strips_keys_by_default(client, tmp_path):
    client.patch("/api/providers/deepseek", json={"api_key": "sk-very-secret-1234"})

    def dump(url):
        r = client.get(url)
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        f = tmp_path / "dl.db"
        f.write_bytes(r.content)
        con = sqlite3.connect(f)
        try:
            return con.execute("SELECT api_key FROM providers WHERE id='deepseek'").fetchone()[0], \
                con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        finally:
            con.close()

    key, agents = dump("/api/data/export")
    assert key == "" and agents == 4
    assert dump("/api/data/export?include_keys=true")[0] == "sk-very-secret-1234"
    # 导出不能影响线上数据库里的密钥
    assert client.get("/api/providers").json()[0]["has_key"] is True


def test_clear_all_messages(client):
    gid = client.get("/api/groups").json()[0]["id"]
    assert len(client.get(f"/api/groups/{gid}/messages").json()) >= 1  # 欢迎消息
    assert client.delete("/api/data/messages").json()["deleted"] >= 1
    assert client.get(f"/api/groups/{gid}/messages").json() == []


def test_export_filename_is_readable_by_browser_frontend(client):
    """跨域时前端只能读到被 expose 的响应头;否则下载的备份永远叫默认名。"""
    r = client.get("/api/data/export", headers={"Origin": "http://localhost:5173"})
    assert "content-disposition" in r.headers["access-control-expose-headers"].lower()
    assert "team-agent-backup-" in r.headers["content-disposition"]


def test_new_endpoints_require_token(tmp_path):
    app = create_app(tmp_path / "d", completion_fn=FakeLLM(), token="tok")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        for method, path in [("get", "/api/stats"), ("get", "/api/system"), ("get", "/api/data/export"),
                             ("delete", "/api/data/messages"), ("post", "/api/providers/deepseek/fetch-models")]:
            assert getattr(c, method)(path).status_code == 401, path
