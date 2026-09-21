"""模板中心(设置 → 模板中心,原「示例库」)的行为回归。

这一版把「clone 第三方仓库 + 填路径刷新」整套机制删掉了,改成程序自带的一手模板目录。
这些用例钉住三件事:
  1. 目录是自洽的 —— 每个团队模板引用的成员和技能都真的存在,不会点了没反应;
  2. 一键应用是幂等的、安全的 —— 重复点不重复写,建群不重名覆盖,MCP 一律停用;
  3. 团队自定义模板的扩展口是受校验的 —— 非法条目被丢弃并给出原因,且不接受命令类(mcp)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import gallery
from app.main import create_app
from app.presets import AGENT_PRESETS, SEED_AGENTS
from app.tools import EXAMPLE_SKILLS, delete_skill, skill_for
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    fake = FakeLLM(default="好")
    data = tmp_path / "data"
    app = create_app(data, completion_fn=fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data_dir = data
        yield c


def _write_custom(store, name: str, payload: dict) -> None:
    d = Path(store.data_dir) / gallery.CUSTOM_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------- 目录自洽性
def test_catalog_items_are_wellformed_and_unique(store) -> None:
    ov = gallery.overview(store)
    ids = [i["id"] for i in ov["items"]]
    assert len(ids) == len(set(ids)), "模板 id 必须唯一"
    assert set(ov["counts"]) == set(gallery.KINDS)
    assert sum(ov["counts"].values()) == ov["total"] == len(ov["items"])
    for it in ov["items"]:
        assert it["id"].startswith(f"{it['kind']}:")
        assert it["kind"] in gallery.KINDS
        assert it["name"] and it["summary"] is not None and it["icon"]
        assert it["source"] == "builtin" or it["source"].startswith("custom:")
        assert isinstance(it["installed"], bool)
        # 列表里不该带完整正文(正文只在详情接口里给)
        assert "def" not in it


def test_team_templates_reference_existing_members_and_skills() -> None:
    """团队模板里的成员名、技能名都必须能在本程序里解析到 —— 否则用户点了会缺人。"""
    known_agents = {a["name"] for a in SEED_AGENTS} | {p["name"] for p in AGENT_PRESETS}
    for item in gallery._team_rows():
        d = item["def"]
        assert d["members"], f"{item['name']} 没有成员"
        assert all(n in known_agents for n in d["members"]), f"{item['name']} 引用了未知成员"
        assert d["host"] in d["members"], f"{item['name']} 的群主不在成员里"
        assert all(skill_for(s) for s in d["skills"]), f"{item['name']} 引用了未知技能"


def test_catalog_ships_no_third_party_source_metadata(store) -> None:
    """模板中心的条目只可能是自带的或用户自备的,不再有「第三方仓库 URL + 许可证」这类字段。"""
    ov = gallery.overview(store)
    blob = json.dumps(ov, ensure_ascii=False)
    assert "awesome-llm-apps" not in blob
    assert "http://" not in blob and "https://" not in blob
    assert "shipped" not in ov and "commit" not in ov


# --------------------------------------------------------------- 一键应用
def test_team_template_builds_group_with_members_and_skills(client) -> None:
    r = client.post("/api/gallery/team:office/apply", json={}).json()
    g = r["group"]
    assert g["name"] == "Office documents" and len(g["member_ids"]) == 4
    assert r["agents"] == ["Aide", "Librarian", "Copywriter", "Proofreader"]
    assert g["ext"]["skills"] == ["Office writing conventions"]    # 依赖的技能挂成群规则
    host = next(a for a in client.get("/api/agents").json() if a["id"] == g["host_agent_id"])
    assert host["name"] == "Aide"
    # 技能本身也要真的在技能库里(内置示例技能启动时已写入 → 这次算「已存在」)
    assert "Office writing conventions" in {s["name"] for s in client.get("/api/skills").json()}
    assert r["summary"].startswith('Group "Office documents" is ready')
    # 技能本来就有时,措辞不能吹成「装好了」
    assert r["skipped"] == ["Skill: Office writing conventions"] and "already present" in r["summary"]
    assert "and installed" not in r["summary"]
    # 中文界面下同一套模板与技能以中文名呈现
    zh = client.post("/api/gallery/team:office/apply", json={"name": "办公文档"}, headers={"Accept-Language": "zh-CN"}).json()
    assert zh["group"]["ext"]["skills"] == ["公文写作规范"]
    assert zh["skipped"] == ["技能:公文写作规范"] and "本来就有" in zh["summary"]
    assert client.get("/api/gallery/team:office", headers={"Accept-Language": "zh-CN"}).json()["name"] == "办公文档"


def test_team_template_reports_newly_installed_skills(client) -> None:
    delete_skill(Path(client.data_dir) / "skills", "Office writing conventions")
    r = client.post("/api/gallery/team:office/apply", json={}).json()
    assert r["added"] == ["Skill: Office writing conventions"]
    assert "and installed 1 skill" in r["summary"]


def test_team_template_never_overwrites_an_existing_group(client) -> None:
    first = client.post("/api/gallery/team:office/apply", json={}).json()["group"]
    second = client.post("/api/gallery/team:office/apply", json={}).json()["group"]
    third = client.post("/api/gallery/team:office/apply", json={"name": "我的文档组"}).json()["group"]
    names = {g["name"] for g in client.get("/api/groups").json()}
    assert first["id"] != second["id"] and first["name"] == "Office documents"
    assert second["name"] == "Office documents 2"
    assert third["name"] == "我的文档组"
    assert {"Office documents", "Office documents 2", "我的文档组"} <= names


def test_team_template_is_idempotent_for_members(client) -> None:
    before = len(client.get("/api/agents").json())
    client.post("/api/gallery/team:office/apply", json={})
    mid = len(client.get("/api/agents").json())
    client.post("/api/gallery/team:office/apply", json={})
    assert mid > before                              # 第一次会补建缺失的岗位成员
    assert len(client.get("/api/agents").json()) == mid   # 第二次不会重复建


def test_agent_template_creates_member_and_can_join_group(client) -> None:
    r = client.post("/api/gallery/agent:researcher/apply", json={}).json()
    assert r["agents"] == ["Researcher"] and r["added"] == ["Member: Researcher"]
    gid = client.get("/api/groups").json()[0]["id"]
    r2 = client.post("/api/gallery/agent:researcher/apply", json={"group_id": gid}).json()
    assert r2["added"] == ["Joined: Product launch group"]
    g = next(g for g in client.get("/api/groups").json() if g["id"] == gid)
    aid = next(a["id"] for a in client.get("/api/agents").json() if a["name"] == "Researcher")
    assert aid in g["member_ids"]
    # 再点一次:已经在群里了,应该只是提示,不再加
    r3 = client.post("/api/gallery/agent:researcher/apply", json={"group_id": gid}).json()
    assert any("is already in this group" in n for n in r3["notes"])


def test_agent_template_requires_an_existing_group(client) -> None:
    assert client.post("/api/gallery/agent:editor/apply", json={"group_id": "nope"}).status_code == 400


def test_skill_apply_is_idempotent_and_overwritable(client) -> None:
    # 内置示例技能在启动时已经写进技能库 → 直接应用算「已存在」
    already = client.post("/api/gallery/skill:code-review/apply", json={}).json()
    assert already["added"] == [] and already["skipped"] == ["Skill: Code review checklist"]
    # 删掉之后再应用:应该重新写进去
    delete_skill(Path(client.data_dir) / "skills", "Code review checklist")
    first = client.post("/api/gallery/skill:code-review/apply", json={}).json()
    assert first["added"] == ["Skill: Code review checklist"]
    again = client.post("/api/gallery/skill:code-review/apply", json={}).json()
    assert again["skipped"] == ["Skill: Code review checklist"] and again["added"] == []
    forced = client.post("/api/gallery/skill:code-review/apply", json={"overwrite": True}).json()
    assert forced["added"] == ["Skill: Code review checklist"]


def test_skill_directory_matches_the_catalog_body(client) -> None:
    """导入的技能正文必须和目录里的一致(不能只写了个标题)。"""
    client.post("/api/gallery/skill:risk-check/apply", json={})
    body = (Path(client.data_dir) / "skills" / "Risk self-check" / "SKILL.md").read_text(encoding="utf-8")
    assert EXAMPLE_SKILLS["risk-check"]["body"].strip()[:20] in body
    # 从中文名导入也写进同一个（规范名）目录,不会多出一份
    assert not (Path(client.data_dir) / "skills" / "风险自查清单").exists()


def test_prompt_apply_skips_then_overwrites(client) -> None:
    def titles() -> list[str]:
        return [p["title"] for p in client.get("/api/prompts").json()["prompts"]]

    # 种子提示词默认已经写进库里
    skip = client.post("/api/gallery/prompt:leading-conclusion/apply", json={}).json()
    assert skip["skipped"] == ["Prompt: Lead with the conclusion"] and skip["added"] == []
    forced = client.post("/api/gallery/prompt:leading-conclusion/apply", json={"overwrite": True}).json()
    assert forced["added"] == ["Prompt: Lead with the conclusion"]
    assert titles().count("Lead with the conclusion") == 1
    # 老库里的中文标题也算同一个提示词:不会写成第二份
    zh = client.post("/api/gallery/prompt:leading-conclusion/apply", json={},
                     headers={"Accept-Language": "zh-CN"}).json()
    assert zh["skipped"] == ["提示词:先给结论"]
    assert client.get("/api/prompts", headers={"Accept-Language": "zh-CN"}).json()["prompts"][0]["title"] == "先给结论"


def test_mcp_template_is_added_but_disabled(client) -> None:
    r = client.post("/api/gallery/mcp:filesystem/apply", json={}).json()
    assert r["added"] == ["MCP: Filesystem"]
    m = next(m for m in client.get("/api/mcp").json() if m["name"] == "Filesystem")
    assert m["enabled"] is False                       # 一律停用,必须用户自己核对后启用
    assert m["command"] == "npx" and m["args"][-1] == gallery.PLACEHOLDER_DIR
    assert m["env"] == {}
    assert "disabled" in r["summary"]
    assert any("will not enable it" in n for n in r["notes"])   # 明确告诉用户:启用由人来做
    assert client.post("/api/gallery/mcp:filesystem/apply", json={}).json()["skipped"] == ["MCP: Filesystem"]
    # 中文界面下同一个服务器显示中文名
    assert client.get("/api/mcp", headers={"Accept-Language": "zh-CN"}).json()[0]["name"] == "文件系统"


def test_mcp_page_templates_share_one_source(client) -> None:
    """「MCP」页的内置模板和模板中心用的是同一份清单(避免两处漂移)。"""
    listing = client.get("/api/mcp/templates").json()
    assert [m["name"] for m in listing] == [m["name"] for m in gallery.MCP_TEMPLATES]


def test_unknown_template_is_404_and_bad_group_is_400(client) -> None:
    assert client.get("/api/gallery/team:不存在").status_code == 404
    assert client.post("/api/gallery/team:不存在/apply", json={}).status_code == 400


def test_detail_endpoint_returns_full_body(client) -> None:
    d = client.get("/api/gallery/skill:risk-check").json()
    assert d["def"]["body"] == EXAMPLE_SKILLS["risk-check"]["body"]
    assert d["kind"] == "skill" and d["installed"] is True
    assert client.get("/api/gallery/team:report").json()["def"]["members"]
    # 模板详情按请求语言返回(名称、正文、分类提示都是)
    zh = client.get("/api/gallery/skill:risk-check", headers={"Accept-Language": "zh-CN"}).json()
    assert zh["name"] == "风险自查清单" and zh["preview"]["body"].startswith("对外发布")
    cats = client.get("/api/gallery", headers={"Accept-Language": "zh-CN"}).json()["categories"]
    assert [c["label"] for c in cats][:2] == ["团队", "角色"]
    assert "一键建成群聊" in cats[0]["hint"]


# --------------------------------------------------------------- 团队自定义模板
def test_custom_templates_are_loaded_and_can_be_applied(store) -> None:
    _write_custom(store, "team-internal.json", {
        "schema_version": 1, "catalog_version": "1.0.0", "author": "科室",
        "items": [
            {"id": "weekly", "kind": "team", "name": "科室周会", "summary": "自带模板:每周例会",
             "icon": "🗓️", "members": ["Facilitator", "Scribe", "Reviewer"], "host": "Facilitator",
             "skills": ["头脑风暴规则"], "prompt": "本群每周复盘一次。"},
            {"id": "polite", "kind": "skill", "name": "对外措辞规范",
             "summary": "对外沟通的语气要求", "body": "对外一律用正式语气,不承诺时间表。"},
        ],
    })
    ov = gallery.overview(store)
    assert ov["custom"]["loaded"] == 2 and ov["custom"]["errors"] == []
    assert ov["custom"]["files"][0] == {"name": "team-internal.json", "items": 2,
                                        "version": "1.0.0", "author": "科室"}
    ids = {i["id"] for i in ov["items"]}
    assert {"team:weekly", "skill:polite"} <= ids
    assert next(i for i in ov["items"] if i["id"] == "skill:polite")["source"] == "custom:team-internal.json"

    r = gallery.apply(store, "team:weekly")
    assert r["group"]["name"] == "科室周会" and r["group"]["prompt"] == "本群每周复盘一次。"
    # 自定义模板引用的内置技能统一存成规范名(中英两种写法都指向同一个技能)
    assert r["group"]["ext"]["skills"] == ["Brainstorming rules"]
    r2 = gallery.apply(store, "skill:polite")
    assert r2["added"] == ["Skill: 对外措辞规范"]
    assert (Path(store.data_dir) / "skills" / "对外措辞规范" / "SKILL.md").exists()


def test_bad_custom_items_are_dropped_with_reasons(store) -> None:
    _write_custom(store, "mixed.json", {
        "schema_version": 1,
        "items": [
            {"id": "ok", "kind": "prompt", "name": "好条目", "content": "正文"},
            {"id": "bad id!", "kind": "prompt", "name": "坏 id", "content": "x"},
            {"id": "noKind", "name": "缺 kind", "content": "x"},
            {"id": "mcp-try", "kind": "mcp", "name": "想加命令", "command": "rm -rf /"},
            {"id": "emptyBody", "kind": "skill", "name": "空技能", "body": "  "},
            {"id": "tooNew", "kind": "prompt", "name": "要新版本", "content": "x", "requires": "99.0.0"},
        ],
    })
    ov = gallery.overview(store)
    assert ov["custom"]["loaded"] == 1
    assert {i["id"] for i in ov["items"] if i["source"].startswith("custom:")} == {"prompt:ok"}
    reasons = {e["id"]: e["reason"] for e in ov["custom"]["errors"]}
    assert set(reasons) == {"bad id!", "noKind", "mcp-try", "emptyBody", "tooNew"}
    assert "不支持 kind=mcp" in reasons["mcp-try"]      # 命令类必须由人自己加
    assert "name" in reasons["noKind"] or "kind" in reasons["noKind"]


def test_custom_template_id_cannot_collide_with_builtin(store) -> None:
    _write_custom(store, "dup.json", {
        "schema_version": 1,
        "items": [{"id": "office", "kind": "team", "name": "冒名", "members": ["Aide"]}],
    })
    ov = gallery.overview(store)
    assert ov["custom"]["loaded"] == 0
    assert "不唯一" in ov["custom"]["errors"][0]["reason"]


def test_custom_file_schema_and_size_are_guarded(store) -> None:
    _write_custom(store, "future.json", {"schema_version": 99, "items": []})
    _write_custom(store, "notjson.json", {"schema_version": 1})            # 缺 items
    (Path(store.data_dir) / gallery.CUSTOM_DIRNAME / "broken.json").write_text("{不是 json", encoding="utf-8")
    (Path(store.data_dir) / gallery.CUSTOM_DIRNAME / "huge.json").write_text(
        "x" * (gallery.CUSTOM_MAX_BYTES + 10), encoding="utf-8")
    errors = {e["file"]: e["reason"] for e in gallery.overview(store)["custom"]["errors"]}
    assert set(errors) == {"future.json", "notjson.json", "broken.json", "huge.json"}
    assert "schema_version=99" in errors["future.json"]
    assert "items" in errors["notjson.json"]
    assert "解析失败" in errors["broken.json"]
    assert "已忽略" in errors["huge.json"]


def test_custom_templates_reload_after_the_file_changes(store) -> None:
    _write_custom(store, "one.json", {"schema_version": 1, "items": [
        {"id": "a", "kind": "prompt", "name": "甲", "content": "1"}]})
    assert gallery.overview(store)["custom"]["loaded"] == 1
    _write_custom(store, "one.json", {"schema_version": 1, "items": [
        {"id": "a", "kind": "prompt", "name": "甲", "content": "1"},
        {"id": "b", "kind": "prompt", "name": "乙", "content": "2"}]})
    assert gallery.overview(store)["custom"]["loaded"] == 2   # 改完不用重启


def test_version_compare_is_numeric_not_string() -> None:
    assert gallery._ver("1.10.0") > gallery._ver("1.9.0")     # 字符串比较会判反
    assert gallery._ver("2.0.0") > gallery._ver("1.99.99")
    assert gallery._ver("") == (0,)
    assert gallery._ver("v1.2.3") == (1, 2, 3)
