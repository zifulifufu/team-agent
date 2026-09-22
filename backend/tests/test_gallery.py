"""Behaviour regressions for the gallery (Settings -> Gallery, once called the
"examples library").

This release removes the whole "clone a third-party repo and refresh its path"
mechanism in favour of a first-party catalog shipped with the app.
These tests pin down three things:
  1. the catalog is self-consistent - every member and skill a team template refers
     to really exists, so nothing is a dead end;
  2. one-click apply is idempotent and safe - repeated clicks write nothing twice,
     creating a group never overwrites one with the same name, MCP always lands
     disabled;
  3. the extension point for team-authored templates is validated - invalid entries
     are dropped with a reason, and command-style entries (mcp) are rejected.
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


# -------------------------------------------------------- catalog self-consistency
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
        # the list carries no full body (that lives in the detail endpoint)
        assert "def" not in it


def test_team_templates_reference_existing_members_and_skills() -> None:
    """Member and skill names in a team template must all resolve inside this app - if
    they do not, clicking it leaves the group short-handed.
"""
    known_agents = {a["name"] for a in SEED_AGENTS} | {p["name"] for p in AGENT_PRESETS}
    for item in gallery._team_rows():
        d = item["def"]
        assert d["members"], f"{item['name']} 没有成员"
        assert all(n in known_agents for n in d["members"]), f"{item['name']} 引用了未知成员"
        assert d["host"] in d["members"], f"{item['name']} 的群主不在成员里"
        assert all(skill_for(s) for s in d["skills"]), f"{item['name']} 引用了未知技能"


def test_catalog_ships_no_third_party_source_metadata(store) -> None:
    """Gallery entries can only be built-in or user supplied: no more fields of the
    "third-party repo URL + licence" kind.
"""
    ov = gallery.overview(store)
    blob = json.dumps(ov, ensure_ascii=False)
    assert "awesome-llm-apps" not in blob
    assert "http://" not in blob and "https://" not in blob
    assert "shipped" not in ov and "commit" not in ov


# ------------------------------------------------------------- one-click apply
def test_team_template_builds_group_with_members_and_skills(client) -> None:
    r = client.post("/api/gallery/team:office/apply", json={}).json()
    g = r["group"]
    assert g["name"] == "Office documents" and len(g["member_ids"]) == 4
    assert r["agents"] == ["Aide", "Librarian", "Copywriter", "Proofreader"]
    assert g["ext"]["skills"] == ["Office writing conventions"]    # the skills it needs are attached as group rules
    host = next(a for a in client.get("/api/agents").json() if a["id"] == g["host_agent_id"])
    assert host["name"] == "Aide"
    # the skill itself must really be in the skill library (built-in example skills
    # are written at startup, so this counts as already present)
    assert "Office writing conventions" in {s["name"] for s in client.get("/api/skills").json()}
    assert r["summary"].startswith('Group "Office documents" is ready')
    # when the skill was already there, the wording must not claim it was installed
    assert r["skipped"] == ["Skill: Office writing conventions"] and "already present" in r["summary"]
    assert "and installed" not in r["summary"]
    # a Chinese interface shows the same template and skills under their Chinese names
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
    assert mid > before                              # the first run creates missing role members
    assert len(client.get("/api/agents").json()) == mid   # the second creates nothing new


def test_agent_template_creates_member_and_can_join_group(client) -> None:
    r = client.post("/api/gallery/agent:researcher/apply", json={}).json()
    assert r["agents"] == ["Researcher"] and r["added"] == ["Member: Researcher"]
    gid = client.get("/api/groups").json()[0]["id"]
    r2 = client.post("/api/gallery/agent:researcher/apply", json={"group_id": gid}).json()
    assert r2["added"] == ["Joined: Product launch group"]
    g = next(g for g in client.get("/api/groups").json() if g["id"] == gid)
    aid = next(a["id"] for a in client.get("/api/agents").json() if a["name"] == "Researcher")
    assert aid in g["member_ids"]
    # clicking again: it is already in the group, so just a note and no second add
    r3 = client.post("/api/gallery/agent:researcher/apply", json={"group_id": gid}).json()
    assert any("is already in this group" in n for n in r3["notes"])


def test_agent_template_requires_an_existing_group(client) -> None:
    assert client.post("/api/gallery/agent:editor/apply", json={"group_id": "nope"}).status_code == 400


def test_skill_apply_is_idempotent_and_overwritable(client) -> None:
    # built-in example skills are written to the skill library at startup, so
    # applying counts as already present
    already = client.post("/api/gallery/skill:code-review/apply", json={}).json()
    assert already["added"] == [] and already["skipped"] == ["Skill: Code review checklist"]
    # after deleting it, applying must write it back
    delete_skill(Path(client.data_dir) / "skills", "Code review checklist")
    first = client.post("/api/gallery/skill:code-review/apply", json={}).json()
    assert first["added"] == ["Skill: Code review checklist"]
    again = client.post("/api/gallery/skill:code-review/apply", json={}).json()
    assert again["skipped"] == ["Skill: Code review checklist"] and again["added"] == []
    forced = client.post("/api/gallery/skill:code-review/apply", json={"overwrite": True}).json()
    assert forced["added"] == ["Skill: Code review checklist"]


def test_skill_directory_matches_the_catalog_body(client) -> None:
    """The imported skill body must match the catalog entry (not just a lone title)."""
    client.post("/api/gallery/skill:risk-check/apply", json={})
    body = (Path(client.data_dir) / "skills" / "Risk self-check" / "SKILL.md").read_text(encoding="utf-8")
    assert EXAMPLE_SKILLS["risk-check"]["body"].strip()[:20] in body
    # importing it under its Chinese name writes the same (canonical) directory, not a second copy
    assert not (Path(client.data_dir) / "skills" / "风险自查清单").exists()


def test_prompt_apply_skips_then_overwrites(client) -> None:
    def titles() -> list[str]:
        return [p["title"] for p in client.get("/api/prompts").json()["prompts"]]

    # seed prompts are already written to the database by default
    skip = client.post("/api/gallery/prompt:leading-conclusion/apply", json={}).json()
    assert skip["skipped"] == ["Prompt: Lead with the conclusion"] and skip["added"] == []
    forced = client.post("/api/gallery/prompt:leading-conclusion/apply", json={"overwrite": True}).json()
    assert forced["added"] == ["Prompt: Lead with the conclusion"]
    assert titles().count("Lead with the conclusion") == 1
    # a Chinese title from an old database counts as the same prompt: no duplicate
    zh = client.post("/api/gallery/prompt:leading-conclusion/apply", json={},
                     headers={"Accept-Language": "zh-CN"}).json()
    assert zh["skipped"] == ["提示词:先给结论"]
    assert client.get("/api/prompts", headers={"Accept-Language": "zh-CN"}).json()["prompts"][0]["title"] == "先给结论"


def test_mcp_template_is_added_but_disabled(client) -> None:
    r = client.post("/api/gallery/mcp:filesystem/apply", json={}).json()
    assert r["added"] == ["MCP: Filesystem"]
    m = next(m for m in client.get("/api/mcp").json() if m["name"] == "Filesystem")
    assert m["enabled"] is False                       # always disabled; the user has to review and enable it
    assert m["command"] == "npx" and m["args"][-1] == gallery.PLACEHOLDER_DIR
    assert m["env"] == {}
    assert "disabled" in r["summary"]
    assert any("will not enable it" in n for n in r["notes"])   # says out loud that enabling is the user call
    assert client.post("/api/gallery/mcp:filesystem/apply", json={}).json()["skipped"] == ["MCP: Filesystem"]
    # a Chinese interface shows the same server under its Chinese name
    assert client.get("/api/mcp", headers={"Accept-Language": "zh-CN"}).json()[0]["name"] == "文件系统"


def test_mcp_page_templates_share_one_source(client) -> None:
    """The built-in templates on the MCP page and the gallery read the same list\nbeneath, so the two cannot drift apart."""
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
    # details follow the request language (names, bodies, category hints, all of it)
    zh = client.get("/api/gallery/skill:risk-check", headers={"Accept-Language": "zh-CN"}).json()
    assert zh["name"] == "风险自查清单" and zh["preview"]["body"].startswith("对外发布")
    cats = client.get("/api/gallery", headers={"Accept-Language": "zh-CN"}).json()["categories"]
    assert [c["label"] for c in cats][:2] == ["团队", "角色"]
    assert "一键建成群聊" in cats[0]["hint"]


# ---------------------------------------------------- team-authored templates
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
    # built-in skills referenced by a custom template are stored under their
    # canonical name (both spellings point at the same skill)
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
    assert "do not support kind=mcp" in reasons["mcp-try"]   # command entries must be added by a human
    assert "name" in reasons["noKind"] or "kind" in reasons["noKind"]


def test_custom_template_id_cannot_collide_with_builtin(store) -> None:
    _write_custom(store, "dup.json", {
        "schema_version": 1,
        "items": [{"id": "office", "kind": "team", "name": "冒名", "members": ["Aide"]}],
    })
    ov = gallery.overview(store)
    assert ov["custom"]["loaded"] == 0
    assert "is not unique" in ov["custom"]["errors"][0]["reason"]


def test_custom_file_schema_and_size_are_guarded(store) -> None:
    _write_custom(store, "future.json", {"schema_version": 99, "items": []})
    _write_custom(store, "notjson.json", {"schema_version": 1})            # missing items
    (Path(store.data_dir) / gallery.CUSTOM_DIRNAME / "broken.json").write_text("{不是 json", encoding="utf-8")
    (Path(store.data_dir) / gallery.CUSTOM_DIRNAME / "huge.json").write_text(
        "x" * (gallery.CUSTOM_MAX_BYTES + 10), encoding="utf-8")
    errors = {e["file"]: e["reason"] for e in gallery.overview(store)["custom"]["errors"]}
    assert set(errors) == {"future.json", "notjson.json", "broken.json", "huge.json"}
    assert "schema_version=99" in errors["future.json"]
    assert "items" in errors["notjson.json"]
    assert "could not be read or parsed" in errors["broken.json"]
    assert "ignored" in errors["huge.json"]


def test_custom_templates_reload_after_the_file_changes(store) -> None:
    _write_custom(store, "one.json", {"schema_version": 1, "items": [
        {"id": "a", "kind": "prompt", "name": "甲", "content": "1"}]})
    assert gallery.overview(store)["custom"]["loaded"] == 1
    _write_custom(store, "one.json", {"schema_version": 1, "items": [
        {"id": "a", "kind": "prompt", "name": "甲", "content": "1"},
        {"id": "b", "kind": "prompt", "name": "乙", "content": "2"}]})
    assert gallery.overview(store)["custom"]["loaded"] == 2   # picked up without a restart


def test_version_compare_is_numeric_not_string() -> None:
    assert gallery._ver("1.10.0") > gallery._ver("1.9.0")     # a plain string compare gets this backwards
    assert gallery._ver("2.0.0") > gallery._ver("1.99.99")
    assert gallery._ver("") == (0,)
    assert gallery._ver("v1.2.3") == (1, 2, 3)


def test_the_gallery_still_answers_when_a_member_is_not_a_built_in_one(tmp_path):
    """`builtin_for` answers `None` for a member the user made or imported, and the three callers
    in `gallery.py` are written as `builtin_names(builtin_for(x)) or [x]` — so the crash happened
    one call *before* the fallback that was meant to handle it. With any such member in the
    database the whole template gallery answered 500."""
    from app import gallery
    from app.store import Store

    store = Store(tmp_path / "data")
    store.create_agent("My Own Helper", "🧭", "", "p", None, [], [])       # not a built-in name
    overview = gallery.overview(store)                                     # must not raise

    kinds = {item["kind"] for item in overview["items"]}
    assert "team" in kinds and "agent" in kinds
    # The non-built-in member is simply not one of the entries the gallery can install.
    assert all(item["name"] != "My Own Helper" for item in overview["items"])
