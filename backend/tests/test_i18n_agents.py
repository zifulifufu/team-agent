"""内置成员名的双语行为。

内置成员用英文名做规范值(库里存的就是它),中文写在 `<字段>_zh`;`presets.localize_agent()`
在显示时按请求语言换值。这里覆盖三件事:
  1. 默认(英文)与 `?lang=zh` 两种视图;
  2. 用户改过的名字/角色/提示词在两种语言下都原样保留;
  3. 中文名与英文名都能 @ 到同一个人(老库是新库都能用的关键)。
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
    # 角色也跟着换
    assert _by_name(rows, "小助")["role"] == "协调员"
    # 英文视图下同一个成员是英文角色名
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
    # 名字本身是内置的,所以会按语言换;改过的字段不换
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

    # 精确名字优先于别名:两个都存在时各归各的
    both = [{"id": "1", "name": "小助"}, {"id": "3", "name": "Aide"}]
    assert [m["id"] for m in find_mentions("@小助 hi", both)] == ["1"]
    assert [m["id"] for m in find_mentions("@Aide hi", both)] == ["3"]


def test_agent_presets_mark_existing_members_in_either_language(client) -> None:
    """预设有「已存在」标记:老库用中文名、新库用英文名,都要认出来。"""
    rows = client.get("/api/agent-presets").json()
    assert all(isinstance(p["exists"], bool) for p in rows)
    # 岗位预设不会随种子自动创建
    assert _by_name(rows, "Researcher")["exists"] is False
    # 名字按语言返回
    assert _by_name(rows, "Researcher")["role"] == "Research and method"
    zh = client.get("/api/agent-presets", params={"lang": "zh"}).json()
    assert _by_name(zh, "研究员")["role"] == "研究与方法"

    # 老库的写法:成员叫「研究员」。英文视图下同一条预设也应标记为已存在(靠别名匹配)。
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
