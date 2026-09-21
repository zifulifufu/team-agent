"""Bilingual behaviour of the built-in member names.

Built-in members use the English name as the canonical value (that is what the
database stores) and keep the Chinese one in `<field>_zh`;
`presets.localize_agent()` swaps the value at display time according to the request
language. Three things are covered here:
  1. the default (English) view and the `?lang=zh` view;
  2. a user-edited name/role/prompt surviving untouched in both languages;
  3. both the Chinese and the English name reaching the same member via @
     (essential for old and new databases alike).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import presets
from app.main import create_app
from app.orchestrator import find_mentions
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def _by_name(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_agents_endpoint_serves_english_by_default(client) -> None:
    names = {a["name"] for a in client.get("/api/agents").json()}
    assert {"Aide", "Copywriter", "Storyboard", "Proofreader"} <= names
    assert not any("小助" in n for n in names)


def test_agents_endpoint_serves_chinese_with_lang_zh(client) -> None:
    rows = client.get("/api/agents", params={"lang": "zh"}).json()
    names = {a["name"] for a in rows}
    assert {"小助", "文案", "分镜", "校对"} <= names
    assert not any(n == "Aide" for n in names)
    # the role switches too
    assert _by_name(rows, "小助")["role"] == "协调员"
    # the same member carries the English role name in the English view
    en = _by_name(client.get("/api/agents").json(), "Aide")
    assert en["role"] == "Coordinator"


def test_language_tag_header_is_honoured(client) -> None:
    rows = client.get("/api/agents", headers={"Accept-Language": "zh-CN,zh;q=0.9"}).json()
    assert {a["name"] for a in rows} >= {"小助", "文案"}


def test_user_edits_survive_both_languages() -> None:
    edited = {"name": "小助", "role": "我自己的角色", "prompt": "我自己写的提示词"}
    for lang in ("en", "zh"):
        out = presets.localize_agent(edited, lang)
        assert out["role"] == "我自己的角色" and out["prompt"] == "我自己写的提示词"
    # the name itself is built-in so it follows the language; edited fields do not
    assert presets.localize_agent(edited, "en")["name"] == "Aide"

    renamed = {"name": "我的助手", "role": "协调员"}
    assert presets.localize_agent(renamed, "en")["name"] == "我的助手"
    assert presets.localize_agent(renamed, "zh")["name"] == "我的助手"


def test_at_mentions_accept_either_spelling() -> None:
    english_db = [{"id": "1", "name": "Aide"}, {"id": "2", "name": "Copywriter"}]
    assert [m["id"] for m in find_mentions("@Aide hi", english_db)] == ["1"]
    assert [m["id"] for m in find_mentions("@小助 看一下", english_db)] == ["1"]
    assert [m["id"] for m in find_mentions("@文案 写一下", english_db)] == ["2"]

    chinese_db = [{"id": "1", "name": "小助"}, {"id": "2", "name": "文案"}]
    assert [m["id"] for m in find_mentions("@小助 看一下", chinese_db)] == ["1"]
    assert [m["id"] for m in find_mentions("@Aide take a look", chinese_db)] == ["1"]

    # exact names win over aliases: when both exist each resolves to its own member
    both = [{"id": "1", "name": "小助"}, {"id": "3", "name": "Aide"}]
    assert [m["id"] for m in find_mentions("@小助 hi", both)] == ["1"]
    assert [m["id"] for m in find_mentions("@Aide hi", both)] == ["3"]


def test_agent_presets_mark_existing_members_in_either_language(client) -> None:
    """Presets carry an "already exists" flag: recognised whether the old database
    uses the Chinese name or the new one the English name.
"""
    rows = client.get("/api/agent-presets").json()
    assert all(isinstance(p["exists"], bool) for p in rows)
    # role presets are not created by the seed data
    assert _by_name(rows, "Researcher")["exists"] is False
    # names follow the request language
    assert _by_name(rows, "Researcher")["role"] == "Research and method"
    zh = client.get("/api/agent-presets", params={"lang": "zh"}).json()
    assert _by_name(zh, "研究员")["role"] == "研究与方法"

    # how an old database looks: the member is stored under its Chinese name. The same
    # preset must also be flagged as existing in the English view (matched via the alias).
    assert client.post("/api/agents", json={"name": "研究员"}).status_code == 200
    rows = client.get("/api/agent-presets").json()
    assert _by_name(rows, "Researcher")["exists"] is True
    zh = client.get("/api/agent-presets", params={"lang": "zh"}).json()
    assert _by_name(zh, "研究员")["exists"] is True


def test_builtin_aliases_cover_every_preset() -> None:
    for entry in presets.BUILTIN_AGENTS:
        assert entry.get("name_zh"), f"{entry['name']} 缺少中文名"
        assert presets.builtin_for(entry["name"]) is entry
        assert presets.builtin_for(entry["name_zh"]) is entry
        assert presets.twin_name(entry["name"]) == entry["name_zh"]
        assert presets.twin_name(entry["name_zh"]) == entry["name"]
