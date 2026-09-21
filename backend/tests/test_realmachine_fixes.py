"""WorkBuddy 在用户真机(苹果芯片 Mac、SQLite 3.51、Python 3.14、开着 Clash)上联调发现的问题的回归测试。"""

from __future__ import annotations

import json
import os
import time

import pytest

from app import local_models
from app.main import ensure_loopback_no_proxy
from app.router import litellm_params, retry_after
from app.tools import ToolRegistry
from app.versions import is_newer
from tests.conftest import FakeLLM
from tests.test_collab import Collector, setup


# ------------------------------------------------------------ SQLite ≥3.51:外层不能引用子查询里的 rowid
def test_list_messages_does_not_reference_rowid_in_outer_query(store):
    """新版 SQLite 里 `SELECT * FROM (SELECT ...) ORDER BY rowid` 会报 no such column: rowid(会让整个聊天记录接口 500)。
    这里直接看真正执行的 SQL:子查询把 rowid 取成 _rid,外层只按 _rid 排。"""
    g = store.list_groups()[0]
    seen: list[str] = []
    orig = store._q

    def spy(sql, *a, **k):
        seen.append(sql)
        return orig(sql, *a, **k)

    store._q = spy  # type: ignore[method-assign]
    store.list_messages(g["id"], 10)
    outer = next(s for s in seen if "FROM (SELECT" in s).split(") ORDER BY", 1)[1]
    assert "rowid" not in outer and "_rid" in outer


def test_list_messages_keeps_order_for_same_timestamp_and_hides_helper_column(store):
    g = store.list_groups()[0]
    real = time.time
    fixed = real() + 1000
    time.time = lambda: fixed                    # 同一时刻写入,顺序只能靠写入先后
    try:
        for i in range(6):
            store.add_message(g["id"], "user", "user", "我", f"第{i}条")
    finally:
        time.time = real
    got = [m["content"] for m in store.list_messages(g["id"], 4)]
    assert got == ["第2条", "第3条", "第4条", "第5条"]
    assert all("_rid" not in m for m in store.list_messages(g["id"], 4))


# ------------------------------------------------------------ 熔断:冷却结束后失败计数要清零
def test_circuit_counter_resets_after_cooldown(store, make_router):
    r = make_router(FakeLLM(default="x"))
    store.update_settings({"circuit_threshold": 2, "circuit_cooldown": 5})
    mid = "deepseek/deepseek-flash"
    r._record(mid, False)
    r._record(mid, False)
    assert r._is_open(mid)
    n, _ = r._circuit[mid]
    r._circuit[mid] = (n, time.time() - 1)         # 冷却时间到了
    assert not r._is_open(mid)
    r._record(mid, False)                          # 冷却后只失败一次:不该立刻再熔断
    assert not r._is_open(mid)
    r._record(mid, False)                          # 连续两次才熔断
    assert r._is_open(mid)


# ------------------------------------------------------------ 限速重试时间解析
@pytest.mark.parametrize("msg,expected", [
    ("RateLimitError: 429 请 3 秒后重试", 3.3),
    ("429 Too Many Requests, try again in 2.5 seconds", 2.8),
    ("rate limit, retry in 20ms", 0.32),
    ("rate limit exceeded, please retry after 20s", None),      # 超过 5 秒不等,直接回退
    ("429 too many requests", 1.8),                              # 没说等多久:默认 1.5 秒
])
def test_retry_after_parsing(msg, expected):
    got = retry_after(RuntimeError(msg))
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=0.01)


# ------------------------------------------------------------ 苹果芯片上装了 Intel 版 Python(Rosetta)
def test_rosetta_python_on_apple_silicon_is_detected_as_metal(monkeypatch):
    monkeypatch.setattr(local_models.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(local_models.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_models, "_sysctl", lambda name: "1" if name == "sysctl.proc_translated" else "")
    hw = local_models.hardware()
    assert hw["machine"] == "arm64" and hw["translated"] is True and hw["accel"] == "metal"
    assert local_models.assess(40.0, {**hw, "ram_gb": 128.0, "disk_free_gb": 500.0})["slow"] is False


def test_real_intel_mac_is_still_slow_for_big_models(monkeypatch):
    monkeypatch.setattr(local_models.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(local_models.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_models, "_sysctl", lambda name: "0")
    hw = local_models.hardware()
    assert hw["machine"] == "x86_64" and hw["translated"] is False and hw["accel"] == "none"
    assert local_models.assess(40.0, {**hw, "ram_gb": 64.0, "disk_free_gb": 500.0})["slow"] is True


# ------------------------------------------------------------ 开着代理时,回环地址不能走代理
def test_loopback_is_added_to_no_proxy_without_losing_existing_entries(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.delenv("no_proxy", raising=False)
    ensure_loopback_no_proxy()
    ensure_loopback_no_proxy()                     # 重复调用不会重复追加
    for key in ("NO_PROXY", "no_proxy"):
        parts = os.environ[key].split(",")
        assert parts[0] == "example.com" and {"127.0.0.1", "localhost", "::1"} <= set(parts) and len(parts) == len(set(parts))


# ------------------------------------------------------------ 目录版本比较
@pytest.mark.parametrize("new,old,expected", [
    ("2026-09-21", "2026-09-20", True),
    ("2026-09-20", "2026-09-20", False),
    ("1.10.0", "1.9.0", True),               # 以前按字符串比,1.10.0 会被当成更旧
    ("1.9.0", "1.10.0", False),
    ("2", "1.9", True),
    ("", "1", False),
])
def test_version_compare(new, old, expected):
    assert is_newer(new, old) is expected


# ------------------------------------------------------------ Gemini 的自定义网关地址
def test_gemini_base_url_is_passed_through(store):
    p = {"kind": "gemini", "base_url": "https://gw.example.com/v1beta", "api_key": "k", "extra": {}}
    m = {"model_name": "gemini-2.5-flash"}
    assert litellm_params(p, m)["api_base"] == "https://gw.example.com/v1beta"
    assert "api_base" not in litellm_params({**p, "base_url": ""}, m)


# ------------------------------------------------------------ 插件工具重名:后来的插件报错,而不是悄悄顶掉先来的
def test_duplicate_plugin_tool_name_is_reported(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    body = 'def register(r):\n    r.register("dup", "d", None, lambda a: "%s")\n'
    (d / "a.py").write_text(body % "A", encoding="utf-8")
    (d / "b.py").write_text(body % "B", encoding="utf-8")
    reg = ToolRegistry()
    reg.load_plugins(d)
    assert reg.plugins["a"].error == "" and reg.plugins["a"].tools == ["dup"]
    assert "已被" in reg.plugins["b"].error and reg.plugins["b"].tools == []
    assert [t.plugin for t in reg.plugin_tools(["a", "b"])] == ["a"]


# ------------------------------------------------------------ 分工计划失败时不再出现互相矛盾的两句话
async def test_failed_plan_does_not_leave_a_misleading_done_message(store, make_router):
    bad = json.dumps({"goal": "x", "tasks": "not-a-list"})
    from tests.test_collab import role

    def script(messages):                          # 群主只给了计划、没有任何说明文字 → 会用「已做好分工,见任务板」兜底
        if role(messages) == "Aide" and "【分工模式】" in messages[-1]["content"]:
            return "<plan>" + bad + "</plan>"
        return "好的"

    orch, g = setup(store, make_router, FakeLLM(default=script))
    c = Collector()
    await orch.handle_user_message(g["id"], "帮我出一份发布会通知", c)
    await orch.drain()
    msgs = store.list_messages(g["id"])
    host = next(m for m in msgs if m["sender_type"] == "agent")
    assert "见任务板" not in host["content"] and "没能生效" in host["content"]
    assert any(m["sender_type"] == "system" and "普通接力" in m["content"] for m in msgs)
    assert [e for e in c.events if e["type"] == "message_end"][-1]["message"]["content"] == host["content"]
