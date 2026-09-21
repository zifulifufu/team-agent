import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    fake = FakeLLM(default="你好,我是助手")
    app = create_app(tmp_path / "data", completion_fn=fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.fake = fake
        yield c


def test_seed_data(client):
    provs = client.get("/api/providers").json()
    assert [p["id"] for p in provs] == ["deepseek", "ollama"]
    assert all("api_key" not in p for p in provs)
    assert len(client.get("/api/agents").json()) == 4
    g = client.get("/api/groups").json()
    assert len(g) == 1 and len(g[0]["member_ids"]) == 4


def test_api_key_is_masked_and_route_preview(client):
    client.patch("/api/providers/deepseek", json={"api_key": "sk-abcdef123456"})
    p = client.get("/api/providers").json()[0]
    assert p["has_key"] and p["key_hint"] == "…3456" and "sk-abcdef" not in str(p)
    prev = client.get("/api/route/preview").json()
    assert prev["chain"] == ["deepseek/deepseek-flash", "ollama/qwen2.5:7b"]
    client.put("/api/settings", json={"external_calls_enabled": False})
    prev = client.get("/api/route/preview").json()
    assert prev["chain"] == ["ollama/qwen2.5:7b"]
    assert prev["skipped"][0]["detail"] == "outbound calls are disabled"


def test_add_and_remove_models_and_providers(client):
    p = client.post("/api/providers", json={"preset": "moonshot", "api_key": "sk-x"}).json()
    assert p["id"] == "moonshot" and len(p["models"]) == 1
    m = client.post("/api/providers/moonshot/models", json={"model_name": "kimi-k2-0905-preview"}).json()
    assert m["id"] == "moonshot/kimi-k2-0905-preview"
    assert client.patch("/api/models/moonshot/kimi-k2-0905-preview", json={"enabled": False}).json()["enabled"] is False
    assert client.delete("/api/models/moonshot/kimi-k2-0905-preview").json()["ok"]
    ids = [x["id"] for x in client.get("/api/models").json()]
    assert "moonshot/kimi-k2-0905-preview" not in ids
    # 自定义服务商
    c = client.post("/api/providers", json={
        "name": "我的网关", "kind": "openai_compatible", "base_url": "http://x/v1", "api_key": "k"}).json()
    assert c["id"] and "/" not in c["id"]  # 非 ASCII 名称会回退到随机 id,且 id 不能含 /
    assert client.delete(f"/api/providers/{c['id']}").json()["ok"]


def test_deleting_model_unpins_agents(client):
    client.post("/api/providers", json={"preset": "moonshot"})
    a = client.get("/api/agents").json()[1]
    client.patch(f"/api/agents/{a['id']}", json={"model_id": "moonshot/kimi-k3"})
    client.delete("/api/providers/moonshot")
    assert next(x for x in client.get("/api/agents").json() if x["id"] == a["id"])["model_id"] is None


def test_agent_name_validation(client):
    assert client.post("/api/agents", json={"name": "有 空格"}).status_code == 400
    assert client.post("/api/agents", json={"name": "Copywriter"}).status_code == 409
    assert client.post("/api/agents", json={"name": "配音", "avatar": "🎙️"}).status_code == 200


def test_send_message_streams_over_websocket(client):
    client.patch("/api/providers/deepseek", json={"api_key": "sk-abcdef123456"})
    gid = client.get("/api/groups").json()[0]["id"]
    with client.websocket_connect(f"ws://127.0.0.1/ws/groups/{gid}") as ws:
        assert client.post(f"/api/groups/{gid}/messages", json={"text": "@Copywriter 你好"}).json()["ok"]
        seen = []
        for _ in range(500):                      # 有上限;出错时后端会发 message_discard / idle,一样会收尾,不会永远等下去
            ev = ws.receive_json()
            seen.append(ev["type"])
            if ev["type"] in ("message_end", "message_discard", "idle", "stopped"):
                break
    assert ev["type"] == "message_end", f"没有等到成员回复,收到的事件:{seen}"
    assert seen[0] == "message" and "message_start" in seen and "delta" in seen
    assert ev["message"]["sender_name"] == "Copywriter" and ev["message"]["content"] == "你好,我是助手"
    msgs = client.get(f"/api/groups/{gid}/messages").json()
    assert msgs[-2]["sender_type"] == "user" and msgs[-1]["sender_type"] == "agent"


def test_empty_message_rejected(client):
    gid = client.get("/api/groups").json()[0]["id"]
    assert client.post(f"/api/groups/{gid}/messages", json={"text": "  "}).status_code == 400


def test_skills_tools_mcp(client):
    # 内置技能默认以英文返回;中文界面下同一批技能以中文名返回
    assert {s["name"] for s in client.get("/api/skills").json()} >= {
        "Office writing conventions", "Short video storyboards"}
    assert {s["name"] for s in client.get("/api/skills", params={"lang": "zh"}).json()} >= {
        "公文写作规范", "短视频分镜规范"}
    assert "current_time" in [t["name"] for t in client.get("/api/tools").json()["tools"]]
    m = client.post("/api/mcp", json={"name": "fs", "command": "npx", "args": ["-y", "x"]}).json()
    assert client.get("/api/mcp").json()[0]["args"] == ["-y", "x"]
    client.delete(f"/api/mcp/{m['id']}")
    assert client.get("/api/mcp").json() == []


def test_plugin_registration(tmp_path):
    (tmp_path / "data" / "plugins").mkdir(parents=True)
    (tmp_path / "data" / "plugins" / "hello.py").write_text(
        "def register(r):\n    r.register('hello','say hi',None,lambda a: 'hi ' + a.get('who','you'))\n"
    )
    (tmp_path / "data" / "plugins" / "broken.py").write_text("raise RuntimeError('boom')\n")
    with TestClient(create_app(tmp_path / "data", completion_fn=FakeLLM()), base_url="http://127.0.0.1") as c:
        info = c.get("/api/tools").json()
        assert "hello" in [t["name"] for t in info["tools"]]
        assert any("broken.py" in e for e in info["errors"])


def test_stop_endpoint(client):
    gid = client.get("/api/groups").json()[0]["id"]
    assert client.post(f"/api/groups/{gid}/stop").json() == {"cancelled": 0}


# ------------------------------------------------------------- 本机后端的访问控制
def test_token_required_when_configured(tmp_path):
    app = create_app(tmp_path / "d", completion_fn=FakeLLM(), token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/health").status_code == 200  # 健康检查不需要 token
        assert c.get("/api/agents").status_code == 401
        assert c.get("/api/agents", headers={"X-Team-Agent-Token": "wrong"}).status_code == 401
        assert c.get("/api/agents", headers={"X-Team-Agent-Token": "s3cret"}).status_code == 200
        # 预检请求不带 token 也必须放行,否则浏览器/渲染进程无法发起带自定义头的请求
        pre = c.options("/api/agents", headers={
            "Origin": "null", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-team-agent-token"})
        assert pre.status_code == 200
        gid = c.get("/api/groups", headers={"X-Team-Agent-Token": "s3cret"}).json()[0]["id"]
        from starlette.websockets import WebSocketDisconnect as Denied
        with pytest.raises(Denied):
            with c.websocket_connect(f"ws://127.0.0.1/ws/groups/{gid}"):
                pass
        with c.websocket_connect(f"ws://127.0.0.1/ws/groups/{gid}?token=s3cret"):
            pass


def test_dev_mode_blocks_foreign_origins_and_hosts(tmp_path):
    app = create_app(tmp_path / "d", completion_fn=FakeLLM(), token="")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        ok = c.options("/api/settings", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT"})
        assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
        evil = c.options("/api/settings", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"})
        assert evil.status_code == 400 and "access-control-allow-origin" not in evil.headers
        # DNS 重绑定:Host 头不是本机
        assert c.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
        gid = c.get("/api/groups").json()[0]["id"]
        from starlette.websockets import WebSocketDisconnect as Denied
        with pytest.raises(Denied):
            with c.websocket_connect(f"ws://127.0.0.1/ws/groups/{gid}", headers={"Origin": "https://evil.example"}):
                pass
