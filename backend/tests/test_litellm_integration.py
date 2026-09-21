"""不 mock litellm:DeepSeek / Ollama 都指向本机假服务,验证真实的 LiteLLM 调用与回退链。"""
import pytest

from app.router import ModelRouter
from tests.fakes import FakeServer, free_port, ollama_like, openai_like

MSG = [{"role": "user", "content": "你好"}]


def point(store, deepseek_url, ollama_url, key="sk-test-1234567890"):
    store.update_provider("deepseek", {"api_key": key, "base_url": deepseek_url})
    store.update_provider("ollama", {"base_url": ollama_url})


async def test_deepseek_ok_uses_cloud(store):
    with FakeServer(openai_like("来自云端")) as cloud, FakeServer(ollama_like()) as local:
        point(store, cloud.url, local.url)
        r = await ModelRouter(store).complete(MSG)
        assert r.text == "来自云端" and r.model_id == "deepseek/deepseek-flash"
        assert cloud.server.config.app.state.hits == 1 and local.server.config.app.state.hits == 0
        assert cloud.server.config.app.state.last_body["model"] == "deepseek-flash"


async def test_bad_key_401_falls_back_to_local_ollama(store):
    with FakeServer(openai_like(status=401)) as cloud, FakeServer(ollama_like("来自本地")) as local:
        point(store, cloud.url, local.url)
        r = await ModelRouter(store).complete(MSG)
        assert r.text == "来自本地" and r.model_id == "ollama/qwen2.5:7b"
        assert r.fallback_from == "deepseek/deepseek-flash"
        assert r.attempts[0].status == "failed" and "Authentication" in r.attempts[0].detail
        assert local.server.config.app.state.last_body["model"] == "qwen2.5:7b"


async def test_unreachable_cloud_falls_back_to_local(store):
    with FakeServer(ollama_like("来自本地")) as local:
        point(store, f"http://127.0.0.1:{free_port()}", local.url)  # 没有任何服务在监听
        r = await ModelRouter(store).complete(MSG)
        assert r.text == "来自本地" and r.model_id == "ollama/qwen2.5:7b"


async def test_external_disabled_makes_zero_cloud_requests(store):
    with FakeServer(openai_like("来自云端")) as cloud, FakeServer(ollama_like("来自本地")) as local:
        point(store, cloud.url, local.url)
        store.update_settings({"external_calls_enabled": False})
        r = await ModelRouter(store).complete(MSG)
        assert r.text == "来自本地"
        assert cloud.server.config.app.state.hits == 0


async def test_openai_compatible_provider_kimi_style(store):
    with FakeServer(openai_like("来自Kimi")) as kimi:
        p = store.add_provider("kimi-fake", "openai_compatible", kimi.url, "sk-kimi-1234567890")
        store.add_model(p["id"], "moonshot-v1-8k")
        r = await ModelRouter(store).complete(MSG, preferred=f"{p['id']}/moonshot-v1-8k")
        assert r.text == "来自Kimi" and r.fallback_from is None


async def test_streaming_deltas_arrive_incrementally(store):
    with FakeServer(openai_like("一二三四五六")) as cloud, FakeServer(ollama_like()) as local:
        point(store, cloud.url, local.url)
        got = []

        async def on_delta(d):
            got.append(d)

        await ModelRouter(store).complete(MSG, on_delta=on_delta)
        assert len(got) > 1 and "".join(got) == "一二三四五六"


async def test_no_local_server_and_no_cloud_reports_all_failed(store):
    from app.router import AllRoutesFailed

    point(store, f"http://127.0.0.1:{free_port()}", f"http://127.0.0.1:{free_port()}")
    with pytest.raises(AllRoutesFailed) as ei:
        await ModelRouter(store).complete(MSG)
    assert [a.status for a in ei.value.attempts] == ["failed", "failed"]


async def test_429_is_retried_once_not_hammered_then_falls_back(store):
    """每分钟只允许 3 次请求的账号:SDK 默认会静默连发 3 次,这里必须只有「首次 + 我们的一次重试」共 2 次。"""
    with FakeServer(openai_like(status=429)) as cloud, FakeServer(ollama_like("来自本地")) as local:
        point(store, cloud.url, local.url)
        r = await ModelRouter(store).complete(MSG)
        assert r.text == "来自本地" and r.model_id == "ollama/qwen2.5:7b"
        assert cloud.server.config.app.state.hits == 2
        assert "RateLimit" in r.attempts[0].detail and "限速" in r.attempts[0].detail
