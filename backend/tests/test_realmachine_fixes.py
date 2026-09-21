"""Regressions for problems found while debugging WorkBuddy on a real user machine
(Apple silicon Mac, SQLite 3.51, Python 3.14, Clash running).
"""

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
from tests.conftest import PLAN_MODE, FakeLLM, has
from tests.test_collab import Collector, setup


# --------------------- SQLite >= 3.51: the outer query must not reference an inner rowid
def test_list_messages_does_not_reference_rowid_in_outer_query(store):
    """On newer SQLite, `SELECT * FROM (SELECT ...) ORDER BY rowid` fails with
    "no such column: rowid", which would 500 the whole message-list endpoint.

    This inspects the SQL actually executed: the subquery exposes rowid as _rid and
    the outer query only orders by _rid.
"""
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
    time.time = lambda: fixed                    # same timestamp, so only write order decides
    try:
        for i in range(6):
            store.add_message(g["id"], "user", "user", "我", f"第{i}条")
    finally:
        time.time = real
    got = [m["content"] for m in store.list_messages(g["id"], 4)]
    assert got == ["第2条", "第3条", "第4条", "第5条"]
    assert all("_rid" not in m for m in store.list_messages(g["id"], 4))


# ----------------------------- circuit breaker: the failure counter resets after cooldown
def test_circuit_counter_resets_after_cooldown(store, make_router):
    r = make_router(FakeLLM(default="x"))
    store.update_settings({"circuit_threshold": 2, "circuit_cooldown": 5})
    mid = "deepseek/deepseek-flash"
    r._record(mid, False)
    r._record(mid, False)
    assert r._is_open(mid)
    n, _ = r._circuit[mid]
    r._circuit[mid] = (n, time.time() - 1)         # cooldown elapsed
    assert not r._is_open(mid)
    r._record(mid, False)                          # one failure after cooldown must not trip it again
    assert not r._is_open(mid)
    r._record(mid, False)                          # it takes two in a row
    assert r._is_open(mid)


# -------------------------------------- parsing rate-limit retry delays
@pytest.mark.parametrize("msg,expected", [
    ("RateLimitError: 429 请 3 秒后重试", 3.3),
    ("429 Too Many Requests, try again in 2.5 seconds", 2.8),
    ("rate limit, retry in 20ms", 0.32),
    ("rate limit exceeded, please retry after 20s", None),      # wait longer than 5s: fall back instead
    ("429 too many requests", 1.8),                              # no delay stated: default of 1.5s
])
def test_retry_after_parsing(msg, expected):
    got = retry_after(RuntimeError(msg))
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=0.01)


# ----------------------- Intel (Rosetta) Python installed on Apple silicon
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


# ---------------------------- with a proxy running, loopback traffic must bypass it
def test_loopback_is_added_to_no_proxy_without_losing_existing_entries(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.delenv("no_proxy", raising=False)
    ensure_loopback_no_proxy()
    ensure_loopback_no_proxy()                     # calling it twice must not append twice
    for key in ("NO_PROXY", "no_proxy"):
        parts = os.environ[key].split(",")
        assert parts[0] == "example.com" and {"127.0.0.1", "localhost", "::1"} <= set(parts) and len(parts) == len(set(parts))


# ------------------------------------------------------ catalog version comparison
@pytest.mark.parametrize("new,old,expected", [
    ("2026-09-21", "2026-09-20", True),
    ("2026-09-20", "2026-09-20", False),
    ("1.10.0", "1.9.0", True),               # compared as strings, 1.10.0 used to look older
    ("1.9.0", "1.10.0", False),
    ("2", "1.9", True),
    ("", "1", False),
])
def test_version_compare(new, old, expected):
    assert is_newer(new, old) is expected


# --------------------------------------------- a custom Gemini gateway address
def test_gemini_base_url_is_passed_through(store):
    p = {"kind": "gemini", "base_url": "https://gw.example.com/v1beta", "api_key": "k", "extra": {}}
    m = {"model_name": "gemini-2.5-flash"}
    assert litellm_params(p, m)["api_base"] == "https://gw.example.com/v1beta"
    assert "api_base" not in litellm_params({**p, "base_url": ""}, m)


# ------- duplicate plugin tool names: the later plugin errors instead of silently winning
def test_duplicate_plugin_tool_name_is_reported(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    body = 'def register(r):\n    r.register("dup", "d", None, lambda a: "%s")\n'
    (d / "a.py").write_text(body % "A", encoding="utf-8")
    (d / "b.py").write_text(body % "B", encoding="utf-8")
    reg = ToolRegistry()
    reg.load_plugins(d)
    assert reg.plugins["a"].error == "" and reg.plugins["a"].tools == ["dup"]
    assert "already taken" in reg.plugins["b"].error and reg.plugins["b"].tools == []
    assert [t.plugin for t in reg.plugin_tools(["a", "b"])] == ["a"]


# --------------------- a failed plan must not leave two contradictory sentences behind
async def test_failed_plan_does_not_leave_a_misleading_done_message(store, make_router):
    bad = json.dumps({"goal": "x", "tasks": "not-a-list"})
    from tests.test_collab import role

    def script(messages):                          # the host sends only a plan with no prose, so the
                                                   # fallback line would be "plan is ready, see the task board"
        if role(messages) == "Aide" and has(messages[-1]["content"], PLAN_MODE):
            return "<plan>" + bad + "</plan>"
        return "好的"

    orch, g = setup(store, make_router, FakeLLM(default=script))
    c = Collector()
    await orch.handle_user_message(g["id"], "帮我出一份发布会通知", c)
    await orch.drain()
    msgs = store.list_messages(g["id"])
    host = next(m for m in msgs if m["sender_type"] == "agent")
    assert "see the task board" not in host["content"] and "did not take effect" in host["content"]
    assert any(m["sender_type"] == "system" and "ordinary turn-taking" in m["content"] for m in msgs)
    assert [e for e in c.events if e["type"] == "message_end"][-1]["message"]["content"] == host["content"]
