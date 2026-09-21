"""Chinese/English bilingual regression tests for the built-in content (group
templates / skills / prompts / MCP / categories).

Four things are pinned down:
  1. no built-in content shows Chinese in the English UI, and none shows English in
     the Chinese UI;
  2. an old database (whose columns hold the Chinese spellings) still lines up on
     the new version - the "installed" flag, dedup, attaching skills;
  3. content the user edited survives verbatim in both languages;
  4. names written to disk on install/import are language neutral (not duplicated
     once per language).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import presets, tools
from app.main import create_app
from tests.conftest import FakeLLM

HAN = __import__("re").compile(r"[\u4e00-\u9fff]")


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def zh(client, path, **kw):
    return client.get(path, headers={"Accept-Language": "zh-CN"}, **kw).json()


def en(client, path, **kw):
    return client.get(path, **kw).json()


# ------------------------------------------------------------- group templates
def test_group_templates_are_one_language_at_a_time(client) -> None:
    rows = {t["id"]: t for t in en(client, "/api/templates")}
    assert rows["office"]["name"] == "Office documents"
    assert rows["office"]["members"] == ["Aide", "Librarian", "Copywriter", "Proofreader"]
    assert rows["office"]["skills"] == ["Office writing conventions"]
    assert rows["office"]["host"] == "Aide"
    assert not HAN.search(rows["office"]["name"] + rows["office"]["desc"])

    zrows = {t["id"]: t for t in zh(client, "/api/templates")}
    assert zrows["office"]["name"] == "办公文档"
    assert zrows["office"]["members"] == ["小助", "资料员", "文案", "校对"]
    assert zrows["office"]["skills"] == ["公文写作规范"]
    assert zrows["office"]["host"] == "小助"
    # no English names or blurbs may leak into the Chinese view
    assert not any(c.isascii() and c.isalpha() for c in zrows["office"]["desc"].replace("Markdown", ""))


# ---------------------------------------------------------------- the gallery
def test_gallery_items_are_one_language_at_a_time(client) -> None:
    en_ov, zh_ov = en(client, "/api/gallery"), zh(client, "/api/gallery")
    assert [c["label"] for c in en_ov["categories"]] == ["Teams", "Roles", "Skills", "Prompts", "MCP"]
    assert [c["label"] for c in zh_ov["categories"]] == ["团队", "角色", "技能", "提示词", "MCP"]
    assert en_ov["categories"][0]["hint"].startswith("One click builds")
    assert zh_ov["categories"][0]["hint"].startswith("一键建成群聊")

    def pick(ov, ident):
        return next(i for i in ov["items"] if i["id"] == ident)

    # ids are language neutral, so both sides fetch the same entry by the same id
    for ident, en_name, zh_name in [
        ("team:clinical", "Study design discussion", "研究方案讨论"),
        ("skill:risk-check", "Risk self-check", "风险自查清单"),
        ("prompt:handoff", "Hand off to the next member", "交接给下一位"),
        ("mcp:playwright", "Browser (Playwright)", "浏览器(Playwright)"),
    ]:
        assert pick(en_ov, ident)["name"] == en_name
        assert pick(zh_ov, ident)["name"] == zh_name
        assert pick(en_ov, ident)["tags"] and pick(zh_ov, ident)["tags"]

    # the detail body follows the language too, with no bookkeeping fields such as
    # <field>_zh leaking through
    det_en = en(client, "/api/gallery/skill:risk-check")
    det_zh = zh(client, "/api/gallery/skill:risk-check")
    assert det_en["preview"]["body"].startswith("Before publishing")
    assert det_zh["preview"]["body"].startswith("对外发布")
    assert "_zh" not in str(en(client, "/api/gallery/team:clinical"))


# --------------------------------------------------------------------- skills
def test_installed_skills_are_shown_in_the_request_language(client) -> None:
    names_en = {s["name"] for s in en(client, "/api/skills")}
    names_zh = {s["name"] for s in zh(client, "/api/skills")}
    assert "Code review checklist" in names_en and "代码评审清单" in names_zh
    assert not any(HAN.search(n) for n in names_en)
    # one skill, one file on disk, whatever the language
    en_id = next(s["path"] for s in en(client, "/api/skills") if s["name"] == "Code review checklist")
    zh_id = next(s["path"] for s in zh(client, "/api/skills") if s["name"] == "代码评审清单")
    assert en_id == zh_id
    body_zh = zh(client, "/api/skills/代码评审清单")["body"]
    assert body_zh.startswith("评审代码时按这个顺序看")
    assert en(client, "/api/skills/Code review checklist")["body"].startswith("Review code in this order")


def test_user_edited_builtin_content_is_never_translated() -> None:
    entry = tools.EXAMPLE_SKILLS["risk-check"]
    edited = tools.Skill(name="Risk self-check", description="我自己的说明", body="我自己的正文", path="x")
    for lang in ("en", "zh"):
        shown = tools.localize_skill(edited, lang)
        assert shown.description == "我自己的说明" and shown.body == "我自己的正文"
    # only untouched entries get swapped to the other language
    assert tools.localize_skill(tools.Skill(**{**{k: entry[k] for k in ("name", "description", "body")},
                                               "path": "x"}), "zh").name == "风险自查清单"
    assert presets.localize_prompt({"title": "先给结论", "content": "我改过的"}, "en")["content"] == "我改过的"


def test_group_skills_are_stored_language_neutrally(client) -> None:
    """Skills an old database picked by their Chinese name are still recognised, and
    attaching them to a group always stores the canonical name.
"""
    gid = en(client, "/api/groups")[0]["id"]
    client.patch(f"/api/groups/{gid}", json={"ext": {"skills": ["头脑风暴规则"]}})
    assert en(client, "/api/groups")[0]["ext"]["skills"] == ["Brainstorming rules"]
    assert zh(client, "/api/groups")[0]["ext"]["skills"] == ["头脑风暴规则"]


# -------------------------------------------------------------------- prompts
def test_prompts_and_default_system_prompt_follow_the_language(client) -> None:
    en_p = en(client, "/api/prompts")
    zh_p = zh(client, "/api/prompts")
    assert any(p["title"] == "Lead with the conclusion" for p in en_p["prompts"])
    assert any(p["title"] == "先给结论" for p in zh_p["prompts"])
    assert en_p["default_system_prompt"].startswith('You are "{{agent_name}}"')
    assert zh_p["default_system_prompt"].startswith("你是「{{agent_name}}」")
    assert all(not HAN.search(p["title"]) for p in en_p["prompts"])
    # variable descriptions are bilingual too
    assert next(v for v in en_p["variables"] if v["name"] == "group_name")["desc"] == "the name of the group chat"
    assert next(v for v in zh_p["variables"] if v["name"] == "group_name")["desc"] == "群聊名称"


def test_old_chinese_system_prompt_is_still_recognised() -> None:
    """An old database stores the Chinese default prompt: switching to the English UI
    must show the English default rather than treating it as user-authored.
"""
    assert presets.localize_system_prompt(presets.DEFAULT_SYSTEM_PROMPT_ZH, "en") == presets.DEFAULT_SYSTEM_PROMPT
    assert presets.localize_system_prompt(presets.DEFAULT_SYSTEM_PROMPT, "zh") == presets.DEFAULT_SYSTEM_PROMPT_ZH
    assert presets.localize_system_prompt("我自己写的", "en") == "我自己写的"


# ------------------------------------------------------------------------ MCP
def test_mcp_templates_are_bilingual(client) -> None:
    en_m = en(client, "/api/mcp/templates")
    zh_m = zh(client, "/api/mcp/templates")
    assert [m["name"] for m in en_m][0] == "Filesystem"
    assert [m["name"] for m in zh_m][0] == "文件系统"
    assert all(not HAN.search(m["name"] + m["note"]) for m in en_m)
    # the command itself is identical in every language, otherwise importing breaks
    assert [m["args"] for m in en_m] == [m["args"] for m in zh_m]


# --------------------------------------------------------------- stable reason codes

async def test_skipped_models_carry_a_reason_code(store, make_router):
    """A skipped model carries a stable `reason` code, not just a message.

    The bubble shows `detail`, whose wording follows the interface language, so the
    frontend decides whether to show the offline hint from `reason` — matching on the
    text would silently stop working the moment the UI language changes.
    """
    from tests.conftest import FakeLLM

    store.update_settings({"external_calls_enabled": False})
    r = await make_router(FakeLLM()).complete([{"role": "user", "content": "hi"}])
    codes = {a.reason for a in r.attempts}
    assert "offline" in codes
    assert all(isinstance(a.reason, str) for a in r.attempts)
    # And the serialised form the frontend actually reads must carry it too.
    assert any(a.to_dict().get("reason") == "offline" for a in r.attempts)
