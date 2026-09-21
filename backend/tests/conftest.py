import asyncio
import os
from types import SimpleNamespace

import pytest

# 测试默认不碰真实钥匙串:否则每次写 api_key 都会往你的登录钥匙串里塞测试用的假 Key。
# 需要验证真实钥匙串的用例请设 TEAM_AGENT_KEYCHAIN_TEST=1(见 tests/test_compliance.py)。
os.environ.setdefault("TEAM_AGENT_NO_KEYCHAIN", "1")

from app.router import ModelRouter
from app.store import Store


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "data")
    st.update_settings({"memory_auto_extract": False, "perm_mode": "allow_all"})  # 自动提炼会多发一次模型请求,需要的测试自己打开
    return st


def chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class FakeLLM:
    """可脚本化的 completion_fn。script: {litellm model 字符串前缀: 行为}
    行为: 字符串 -> 正常流式返回; Exception 实例 -> 抛出; ("partial_then_fail", 文本) -> 先吐一段再失败;
          callable(messages) -> 返回字符串。"""

    def __init__(self, script=None, default="ok"):
        self.script = script or {}
        self.default = default
        self.calls = []  # (model, messages)

    async def __call__(self, **kw):
        model, messages = kw["model"], kw["messages"]
        self.calls.append((model, messages))
        behavior = self.default
        for prefix, b in self.script.items():
            if model.startswith(prefix):
                behavior = b
                break
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            behavior = behavior(messages)

        async def gen():
            if isinstance(behavior, tuple) and behavior[0] == "partial_then_fail":
                yield chunk(behavior[1])
                raise ConnectionError("stream broke")
            text = behavior
            for i in range(0, len(text), 3):
                yield chunk(text[i : i + 3])
                await asyncio.sleep(0)

        return gen()


@pytest.fixture
def make_router(store):
    def _make(fake):
        return ModelRouter(store, fake)

    return _make
