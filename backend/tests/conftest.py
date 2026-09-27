import asyncio
import os
from types import SimpleNamespace

import pytest

# Tests stay away from the real keychain by default: otherwise every api_key write
# would drop a fake test key into your login keychain. Tests that really need the
# real keychain set TEAM_AGENT_KEYCHAIN_TEST=1 (see tests/test_compliance.py).
os.environ.setdefault("TEAM_AGENT_NO_KEYCHAIN", "1")

from app.router import ModelRouter
from app.store import Store


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "data")
    st.update_settings({"memory_auto_extract": False, "perm_mode": "allow_all"})  # automatic extraction costs an extra model call; tests that need it turn it on
    return st


# Protocol markers the backend puts in its prompts (see planner.py). The wording follows
# the request language, so tests match either spelling instead of pinning one of them.
PLAN_MODE = ("[Plan mode]", "【分工模式】")
ASSIGNMENT = ("[Assignment]", "【分工】")
TASK_HEAD = ("[Task ", "【分工任务")
INTEGRATE = ("[Integration]", "【整合】")
HOST_MARK = ("(host)", "〔群主〕")
UPSTREAM = ("<- you", "← 你")
TURN_NOW = ("it is now your turn", "现在轮到你")
MEMORY_HEAD = ("[Memory]", "【记忆】")          # memory block injected into the prompt
FELLBACK = ("fell back", "回退")                # behavioural log: model fallback happened
EXTRACT_PROMPT = ("You are a memory editor", "你是记忆整理员")   # background memory extraction


def has(text: str, marker: tuple[str, ...]) -> bool:
    return any(m in text for m in marker)


def chunk(text: str, reasoning: str | None = None):
    """One streamed delta. `reasoning` puts text in the field a reasoning model uses for its working
    (it arrives with an empty `content`), which is what the live "thinking" view is fed from."""
    delta = SimpleNamespace(content=text)
    if reasoning is not None:
        delta.reasoning_content = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class FakeLLM:
    """Scriptable completion_fn.

    script maps {litellm model string prefix: behaviour}, where a behaviour is:
    a string -> streamed back as-is; an Exception instance -> raised;
    ("partial_then_fail", text) -> emits one chunk then fails;
    ("reasoning", working, answer) -> streams `working` in the reasoning field, then the answer;
    callable(messages) -> returns a string.
    """

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
            if isinstance(behavior, tuple) and behavior[0] == "reasoning":
                working, answer = behavior[1], behavior[2]
                for i in range(0, len(working), 3):
                    yield chunk("", working[i : i + 3])
                for i in range(0, len(answer), 3):
                    yield chunk(answer[i : i + 3])
                return
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
