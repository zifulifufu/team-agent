"""Stage four endpoints: settings sanitising, group extras, the capability table,
library uploads, prompts, templates, MCP, plugins, skills, backups.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.presets import PRESET_BY_ID
from tests.conftest import FakeLLM

ECHO = str(Path(__file__).parent / "mcp_echo_server.py")


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = tmp_path / "data"
        yield c


def gid(c):
    return c.get("/api/groups").json()[0]["id"]


# ------------------------------------------------------------------- settings
def test_settings_validation_and_token_is_write_only(client):
    assert client.put("/api/settings", json={"plan_mode": "bogus"}).status_code == 400
    assert client.put("/api/settings", json={"tool_rounds": 99}).status_code == 400
    assert client.put("/api/settings", json={"tool_rounds": True}).status_code == 400
    assert client.put("/api/settings", json={"app_repo": "not a repo"}).status_code == 400
    s = client.put("/api/settings", json={"github_token": "  ghp_x  ", "app_repo": " me/app ", "unknown": 1}).json()
    assert s["github_token"] == "" and s["github_token_set"] is True and s["app_repo"] == "me/app" and "unknown" not in s
    s = client.put("/api/settings", json={"plan_mode": "off"}).json()          # saving without the token must not clear it
    assert s["github_token_set"] is True and s["plan_mode"] == "off"
    assert client.get("/api/settings").json()["github_token"] == ""
    s = client.put("/api/settings", json={"github_token": ""}).json()          # an empty string clears it on purpose
    assert s["github_token_set"] is False
    d = client.get("/api/settings").json()
    # auto_check_updates ships off: enabled, the backend would reach the internet 20s
    # after startup, and in corporate or classified environments that is an
    # unauthorised outbound call
    assert d["auto_update_skills"] is False and d["auto_check_updates"] is False and "{{agent_name}}" in d["system_prompt"]


# ------------------------------------------------------------- group extras
def test_group_ext_merge_and_create_with_ext(client):
    g = gid(client)
    r = client.patch(f"/api/groups/{g}", json={"ext": {"skills": ["头脑风暴规则"]}, "prompt": "本群写文案"}).json()
    # built-in skill names follow the request language: posted with the Chinese
    # spelling, read back under the language currently in effect
    assert r["ext"]["skills"] == ["Brainstorming rules"] and r["prompt"] == "本群写文案" and r["ext"]["plan"] == "inherit"
    r = client.patch(f"/api/groups/{g}", json={"ext": {"library": {"mode": "selected", "kb_ids": ["kb1"], "collection_ids": ["col1"]}, "plan": "bogus"}}).json()
    # Selection is by knowledge base now, not by document
    assert r["ext"]["skills"] == ["Brainstorming rules"] and r["ext"]["library"] == {"mode": "selected", "kb_ids": ["kb1"], "collection_ids": ["col1"]}
    assert r["ext"]["plan"] == "inherit"                                        # an invalid value is ignored
    new = client.post("/api/groups", json={"name": "新群", "ext": {"plugins": ["p"]}, "prompt": "hi"}).json()
    assert new["ext"]["plugins"] == ["p"] and new["prompt"] == "hi"


def test_capabilities_roster_and_tools(client, tmp_path):
    g = gid(client)
    # posting the Chinese tag names (the old convention) still has to be accepted
    # and normalised to ASCII ids
    client.patch(f"/api/agents/{client.get('/api/agents').json()[1]['id']}", json={"tags": ["代码"]})
    cap = client.get(f"/api/groups/{g}/capabilities").json()
    assert len(cap["members"]) == 4 and sum(m["is_host"] for m in cap["members"]) == 1
    assert any("coding" in m["strengths"] for m in cap["members"])
    assert all(m["model"] for m in cap["members"])
    names = {t["name"] for t in cap["tools"]}
    assert {"current_time", "memory_search"} <= names and "library_search" not in names   # an empty library offers no search tool
    client.post("/api/library/note", json={"title": "备忘", "content": "周五开会"})
    names = {t["name"] for t in client.get(f"/api/groups/{g}/capabilities").json()["tools"]}
    assert {"library_search", "library_read"} <= names
    assert client.get("/api/groups/nope/capabilities").status_code == 404


def test_apply_prompt_and_preview(client):
    g = gid(client)
    p = client.post("/api/prompts", json={"title": "群规", "content": "本群 {{group_name}} 简短回答", "use_globally": False}).json()
    assert client.post("/api/prompts", json={"title": "", "content": "x"}).status_code == 400
    r = client.post(f"/api/groups/{g}/apply-prompt", json={"prompt_id": p["id"]}).json()
    assert r["prompt"] == "本群 {{group_name}} 简短回答"
    r = client.post(f"/api/groups/{g}/apply-prompt", json={"prompt_id": p["id"], "mode": "append"}).json()
    assert r["prompt"].count("简短回答") == 2
    prev = client.post("/api/prompts/preview", json={"content": "你在「{{group_name}}」,日期 {{date}}", "group_id": g}).json()
    assert "{{" not in prev["text"] and prev["tokens"] > 0 and prev["raw_tokens"] > 0
    sp = client.get(f"/api/groups/{g}/system-prompt-preview").json()
    assert "[Group prompt]" in sp["text"] and "[Members and their parts]" in sp["text"] and "{{" not in sp["text"]
    lst = client.get("/api/prompts").json()
    assert lst["variables"] and lst["default_system_prompt"] and len(lst["prompts"]) >= 3


def test_global_prompt_reset_and_delete(client):
    client.put("/api/settings", json={"system_prompt": "自定义"})
    assert client.post("/api/prompts/reset-system").json()["system_prompt"].startswith("You are")
    p = client.post("/api/prompts", json={"title": "t", "content": "c"}).json()
    assert client.patch(f"/api/prompts/{p['id']}", json={"use_globally": True}).json()["use_globally"] is True
    assert client.delete(f"/api/prompts/{p['id']}").status_code == 200
    assert client.delete(f"/api/prompts/{p['id']}").status_code == 404


# ----------------------------------------------- member presets / group templates
def test_agent_presets_add_member_anytime_and_templates(client):
    g = gid(client)
    presets = client.get("/api/agent-presets").json()
    assert {p["key"] for p in presets} >= {"host", "reviewer", "scribe", "librarian", "coder"}
    key = next(p["key"] for p in presets if not p["exists"])
    before = len(client.get("/api/groups").json()[0]["member_ids"])
    r = client.post(f"/api/groups/{g}/members/from-preset", json={"key": key}).json()
    assert len(r["member_ids"]) == before + 1
    r2 = client.post(f"/api/groups/{g}/members/from-preset", json={"key": key}).json()
    assert len(r2["member_ids"]) == before + 1                                  # adding twice creates no extra or duplicate member
    assert client.post(f"/api/groups/{g}/members/from-preset", json={"key": "zzz"}).status_code == 404
    tpls = client.get("/api/templates").json()
    assert {t["id"] for t in tpls} >= {"office", "video", "writing", "brainstorm", "review"}
    grp = client.post("/api/templates/brainstorm/create-group", json={}).json()
    assert grp["ext"]["skills"] and len(grp["member_ids"]) >= 3 and grp["host_agent_id"] in grp["member_ids"]
    assert client.post("/api/templates/nope/create-group", json={}).status_code == 404


# ------------------------------------------------------- model picking / strengths
def test_model_options_recommend_and_strengths(client):
    opts = client.get("/api/providers/deepseek/model-options").json()
    ids = [m["id"] for m in opts["models"]]
    assert "deepseek-flash" in ids and opts["new_count"] == 0                  # first open: everything already counts as seen
    m = opts["models"][0]
    assert isinstance(m["strengths"], list) and m["summary"] is not None
    assert client.get("/api/providers/nope/model-options").status_code == 404
    assert client.get("/api/models/recommend", params={"tags": "代码"}).json()["models"] == []   # Chinese tag names are still accepted (see strengths.ALIASES)
    client.patch("/api/providers/deepseek", json={"api_key": "sk-test-1234"})
    rec = client.get("/api/models/recommend", params={"tags": "代码,推理"}).json()
    assert rec["tags"] == ["coding", "reasoning"] and rec["models"]      # canonical ASCII ids come back
    # editing strengths by hand sticks; passing null restores the automatic value
    mid = "deepseek/deepseek-flash"
    r = client.patch(f"/api/models/{mid}", json={"strengths": ["写作"]}).json()
    assert r["strengths"] == ["writing"] and r["strengths_custom"] is True   # normalised to ASCII ids on write
    r = client.patch(f"/api/models/{mid}", json={"strengths": None}).json()
    assert r["strengths_custom"] is False and r["strengths"]
    tags = client.get("/api/strengths").json()["tags"]
    assert "writing" in [t["id"] for t in tags]
    assert {t["label"] for t in tags} >= {"Writing"} and tags[0]["desc"]        # label and description in the English UI
    zh = client.get("/api/strengths?lang=zh").json()["tags"]
    assert "写作" in [t["label"] for t in zh]                                  # the same ids read as Chinese in the Chinese UI


def test_refresh_model_options_respects_offline_switch(client):
    client.put("/api/settings", json={"external_calls_enabled": False})
    assert client.post("/api/providers/deepseek/model-options/refresh").status_code == 403
    assert client.post("/api/providers/deepseek/model-options/seen").status_code == 200


def test_aggregator_presets_stay_remote(client):
    """MetaChat and Cherry Studio can join a group, but neither may claim to be local.

    `is_local` is what lets a provider run while outbound calls are off
    (router.build_chain). Cherry Studio's gateway listens on loopback yet forwards to the
    providers configured inside it, so calling it local would silently defeat the switch.
    """
    presets = {p["preset"]: p for p in client.get("/api/presets").json()}
    assert {"metachat", "cherry-studio"} <= set(presets)
    for pid in ("metachat", "cherry-studio"):
        p = presets[pid]
        assert p["kind"] == "openai_compatible" and p["is_local"] is False, pid
        assert p["base_url"].startswith("http")
        # Both languages, like every other preset. The API localizes `*_zh` away, so read the
        # source table to check the pair exists.
        assert PRESET_BY_ID[pid]["hint"] and PRESET_BY_ID[pid]["hint_zh"]
    # Neither ships a model list: the roster is whatever the account or the gateway reports
    assert presets["metachat"]["models"] == [] and presets["cherry-studio"]["models"] == []
    # Adding one really does create a provider, and a model on it still obeys the offline switch
    prov = client.post("/api/providers", json={"preset": "cherry-studio"}).json()
    assert prov["id"] == "cherry-studio" and prov["is_local"] is False
    client.post("/api/providers/cherry-studio/models", json={"model_name": "whatever"})
    client.put("/api/settings", json={"external_calls_enabled": False})
    r = client.post("/api/test-model", json={"model_id": "cherry-studio/whatever"}).json()
    assert r["ok"] is False and "Outbound calls are disabled" in r["error"]


# ------------------------------------------------------- knowledge bases and collections
def test_documents_live_in_knowledge_bases(client):
    g = gid(client)
    other = client.post("/api/groups", json={"name": "Another project"}).json()["id"]
    OCTET = {"Content-Type": "application/octet-stream"}
    up = lambda gid_, name, body: client.post(  # noqa: E731
        "/api/library/upload", params={"filename": name, "group_id": gid_}, content=body, headers=OCTET).json()

    mine = up(g, "本群.txt", "本群的验收标准以现场演示为准".encode())
    theirs = up(other, "别群.txt", "别人的预算口径按含税价算".encode())
    shared = up("", "共享.txt", "单笔超过 500 元必须附发票".encode())

    # Each group's upload landed in its own knowledge base (the shared one for no group)
    kbs = {k["id"]: k for k in client.get("/api/knowledge-bases").json()}
    assert kbs[mine["kb_id"]]["group_id"] == g and kbs[theirs["kb_id"]]["group_id"] == other
    assert kbs[shared["kb_id"]]["group_id"] == "" and kbs[shared["kb_id"]]["docs"] == 1
    assert kbs[mine["kb_id"]]["name"] == client.get("/api/groups").json()[0]["name"]

    # The group's list is its own knowledge base plus the shared one, never another group's
    titles = {d["title"] for d in client.get("/api/library", params={"group_id": g}).json()["docs"]}
    assert titles == {"本群", "共享"}
    assert {d["title"] for d in client.get("/api/library", params={"kb_id": mine["kb_id"]}).json()["docs"]} == {"本群"}
    # No filter = the whole library, which is what the overview shows
    assert len(client.get("/api/library").json()["docs"]) == 3

    # Searching inside a group cannot reach another group's knowledge base
    def hits(q, **params):
        return {h["title"] for h in client.get("/api/library/search", params={"q": q, **params}).json()}

    assert hits("发票", group_id=g) == {"共享"}
    assert hits("含税价", group_id=g) == set()
    assert hits("含税价", group_id=other) == {"别群"}
    # Scoped to one knowledge base, a shared document is not visible through the group's own
    assert hits("发票", kb_id=mine["kb_id"]) == set()

    # An unknown knowledge base or group is refused rather than treated as shared
    assert client.get("/api/library", params={"kb_id": "nope"}).status_code == 404
    assert client.get("/api/library", params={"group_id": "nope"}).status_code == 404
    assert client.post("/api/library/note", json={"title": "x", "content": "y", "kb_id": "nope"}).status_code == 404
    assert client.post("/api/library/upload", params={"filename": "x.txt", "kb_id": "nope"},
                       content=b"hi", headers=OCTET).status_code == 404


def test_a_shared_collection_cannot_hand_over_a_private_knowledge_base(client):
    """A collection is a convenience for the user, not a way around the workspace boundary."""
    g = gid(client)
    other = client.post("/api/groups", json={"name": "Another project"}).json()["id"]
    secret = client.post("/api/knowledge-bases", json={"name": "别群的机密库", "group_id": other}).json()
    public = client.post("/api/knowledge-bases", json={"name": "共用规范"}).json()
    col = client.post("/api/collections", json={"name": "合集", "kb_ids": [public["id"], secret["id"]]}).json()

    client.post("/api/library/note", json={"title": "机密", "content": "机密内容在此", "kb_id": secret["id"]})
    client.post("/api/library/note", json={"title": "规范", "content": "共用规范内容", "kb_id": public["id"]})

    # Attaching the collection gives the group the shared member, and only that one
    client.patch(f"/api/groups/{g}", json={"ext": {"library": {"mode": "selected", "collection_ids": [col["id"]]}}})
    titles = {d["title"] for d in client.get("/api/library", params={"group_id": g}).json()["docs"]}
    assert titles == {"规范"}
    assert {h["title"] for h in client.get("/api/library/search", params={"q": "机密", "group_id": g}).json()} == set()
    # The collection itself still lists both, because it is a library of knowledge bases, not a
    # view of what any particular group may use
    assert len(client.get("/api/collections").json()[0]["kb_ids"]) == 2


def test_a_group_that_picked_documents_is_moved_to_knowledge_bases(tmp_path):
    """The old `ext.library.ids` listed documents. Opening a database that has one must end up
    with the knowledge bases holding those documents, and never with a wider scope than the
    group could already reach."""
    import json as _json

    from app.store import Store

    st = Store(tmp_path / "d")
    g = st.list_groups()[0]
    kb = st.add_kb("资料库", "", g["id"])
    st.add_doc("一份", "a.txt", "txt", 3, ["内容"], kb_id=kb["id"])
    st.add_doc("两份", "b.txt", "txt", 3, ["内容"], kb_id=kb["id"])
    st._x("UPDATE groups SET ext=? WHERE id=?",
          (_json.dumps({"library": {"mode": "selected", "ids": [st.list_docs(kb["id"])[0]["id"]]}}), g["id"]))

    again = Store(tmp_path / "d")
    lib = again.get_group(g["id"])["ext"]["library"]
    assert lib["mode"] == "selected" and lib["kb_ids"] == [kb["id"]]
    assert "ids" not in lib


def test_knowledge_base_and_collection_crud(client):
    g = gid(client)
    kb = client.post("/api/knowledge-bases", json={"name": "  手册  ", "description": " d ", "group_id": g}).json()
    assert kb["name"] == "手册" and kb["description"] == "d" and kb["group_id"] == g
    assert client.post("/api/knowledge-bases", json={"name": "   "}).status_code == 400

    note = client.post("/api/library/note", json={"title": "一条", "content": "内容", "kb_id": kb["id"]}).json()
    assert note["kb_id"] == kb["id"]
    # A document can be moved between knowledge bases
    shared = client.post("/api/knowledge-bases", json={"name": "共享"}).json()
    assert client.patch(f"/api/library/{note['id']}", json={"kb_id": shared["id"]}).json()["kb_id"] == shared["id"]

    col = client.post("/api/collections", json={"name": "合集", "kb_ids": [kb["id"], "ghost"]}).json()
    # An id that does not exist is dropped rather than stored as a dangling reference
    assert col["id"] and client.get("/api/collections").json()[0]["kb_ids"] == [kb["id"]]
    client.patch(f"/api/collections/{col['id']}", json={"kb_ids": [shared["id"]]})
    assert client.get("/api/collections").json()[0]["kb_ids"] == [shared["id"]]

    # Deleting a knowledge base takes its documents with it, and unlinks it from every group
    client.patch(f"/api/groups/{g}", json={"ext": {"library": {"mode": "selected", "kb_ids": [kb["id"]]}}})
    assert client.delete(f"/api/knowledge-bases/{kb['id']}").json()["deleted_docs"] == 0
    left = client.get("/api/groups").json()[0]["ext"]["library"]["kb_ids"]
    assert kb["id"] not in left
    assert client.delete(f"/api/collections/{col['id']}").status_code == 200

    assert client.get("/api/knowledge-bases", params={"group_id": "nope"}).status_code == 404
    assert client.post("/api/collections", json={"name": " "}).status_code == 400


# -------------------------------------------------------------------- library
def test_library_upload_search_read_scope_cleanup(client):
    g = gid(client)
    raw = "报销制度:单笔超过 500 元必须附发票。".encode()
    d = client.post("/api/library/upload", params={"filename": "报销.txt", "group_id": g}, content=raw, headers={"Content-Type": "application/octet-stream"}).json()
    assert d["title"] == "报销" and d["chars"] > 5
    assert client.post("/api/library/upload", params={"filename": "x.exe", "group_id": g}, content=b"MZ", headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert client.post("/api/library/upload", params={"filename": "x.txt", "group_id": g}, content=b"", headers={"Content-Type": "application/octet-stream"}).status_code == 400
    n = client.post("/api/library/note", json={"title": "备忘", "content": "周五下午开会", "group_id": g}).json()
    hits = client.get("/api/library/search", params={"q": "发票"}).json()
    assert hits and hits[0]["doc_id"] == d["id"]
    assert "发票" in client.get(f"/api/library/{d['id']}").json()["text"]
    assert d["kb_id"] and d["kb_id"] == n["kb_id"], "both landed in the group's own knowledge base"
    client.patch(f"/api/groups/{g}", json={"ext": {"library": {"mode": "selected", "kb_ids": [d["kb_id"]]}}})
    assert client.patch(f"/api/library/{n['id']}", json={"enabled": False}).json()["enabled"] in (False, 0)
    assert client.delete(f"/api/library/{d['id']}").status_code == 200
    ids = client.get("/api/groups").json()[0]["ext"]["library"]["kb_ids"]
    assert ids == [d["kb_id"]], "the group keeps pointing at the knowledge base, not at documents"
    assert client.get(f"/api/library/{d['id']}").status_code == 404
    assert client.get("/api/library").json()["count"] == 1


# --------------------------------------------------------------------- memory
def test_memories_crud_and_guardrails(client):
    m = client.post("/api/memories", json={"content": "报告统一用 A4 竖版", "kind": "preference", "pinned": True}).json()
    assert client.post("/api/memories", json={"content": ""}).status_code == 400
    assert client.post("/api/memories", json={"content": "x" * 501}).status_code == 400
    assert client.post("/api/memories", json={"content": "x", "scope": "group"}).status_code == 400
    assert client.get("/api/memories", params={"q": "A4"}).json()["count"] == 1
    assert client.patch(f"/api/memories/{m['id']}", json={"pinned": False}).status_code == 200
    assert client.delete("/api/memories").status_code == 400                    # wiping everything needs a filter
    assert client.delete("/api/memories", params={"scope": "global"}).json()["deleted"] >= 1
    assert client.delete(f"/api/memories/{m['id']}").status_code == 404


# ----------------------------------------------------------------------- MCP
def test_mcp_secrets_masked_preserved_and_validated(client):
    assert client.post("/api/mcp", json={"name": "x"}).status_code == 400
    assert client.post("/api/mcp", json={"name": "x", "url": "ftp://a"}).status_code == 400
    assert client.post("/api/mcp", json={"name": "x", "command": "a", "transport": "pigeon"}).status_code == 400
    m = client.post("/api/mcp", json={"name": "远程", "url": "https://x.example/mcp", "headers": {"Authorization": "Bearer S3CRET"},
                                      "env": {"K": "v"}}).json()
    assert m["headers"] == {"Authorization": "••••••"} and "S3CRET" not in str(client.get("/api/mcp").json())
    assert m["transport_effective"] == "http"
    # the frontend posts the mask back unchanged, so the original value is kept
    client.patch(f"/api/mcp/{m['id']}", json={"headers": {"Authorization": "••••••"}, "description": "d"})
    from app.store import Store
    assert Store(client.data).get_mcp(m["id"])["headers"] == {"Authorization": "Bearer S3CRET"}
    assert len(client.get("/api/mcp/templates").json()) >= 5


def test_mcp_connect_real_server_and_offline_blocks_remote(client):
    m = client.post("/api/mcp", json={"name": "echo", "command": sys.executable, "args": [ECHO]}).json()
    r = client.post(f"/api/mcp/{m['id']}/connect").json()
    assert r["status"] == "ready" and {t["name"] for t in r["tools"]} == {"echo", "add"}
    g = gid(client)
    client.patch(f"/api/groups/{g}", json={"ext": {"mcp": [m["id"]]}})
    cap = client.get(f"/api/groups/{g}/capabilities").json()
    assert any(t["name"] == "mcp__echo__add" for t in cap["tools"])
    assert client.post(f"/api/mcp/{m['id']}/disconnect").json()["status"] in ("idle", "stopped", "disconnected")
    client.delete(f"/api/mcp/{m['id']}")
    assert client.get("/api/groups").json()[0]["ext"]["mcp"] == []
    remote = client.post("/api/mcp", json={"name": "r", "url": "https://x.example/mcp"}).json()
    client.put("/api/settings", json={"external_calls_enabled": False})
    assert client.post(f"/api/mcp/{remote['id']}/connect").status_code == 403


# ------------------------------------------------------------------- plugins
def test_plugins_listing_reload_source_delete_and_group_enablement(client):
    pdir = client.data / "plugins"
    (pdir / "greet.py").write_text(
        'PLUGIN = {"name": "问候", "version": "1.0"}\ndef register(r):\n    r.register("greet", "问候", None, lambda a: "你好")\n',
        encoding="utf-8")
    (pdir / "bad.py").write_text("raise RuntimeError('boom')", encoding="utf-8")
    ps = {p["id"]: p for p in client.post("/api/plugins/reload").json()}
    assert ps["greet"]["name"] == "问候" and ps["greet"]["tools"] == ["greet"] and ps["bad"]["error"]
    assert "def register" in client.get("/api/plugins/greet/source").json()["content"]
    assert client.get("/api/plugins/..%2Fx/source").status_code in (404, 422)
    g = gid(client)
    assert "greet" not in {t["name"] for t in client.get(f"/api/groups/{g}/capabilities").json()["tools"]}
    client.patch(f"/api/groups/{g}", json={"ext": {"plugins": ["greet"]}})
    assert "greet" in {t["name"] for t in client.get(f"/api/groups/{g}/capabilities").json()["tools"]}
    assert client.delete("/api/plugins/greet").status_code == 200 and not (pdir / "greet.py").exists()
    assert client.delete("/api/plugins/greet").status_code == 404
    assert "greet" not in {t["name"] for t in client.get("/api/tools").json()["tools"]}


# -------------------------------------------------------------------- skills
def test_skills_crud_rename_propagates_to_members_and_groups(client):
    g = gid(client)
    aid = client.get("/api/agents").json()[0]["id"]
    assert client.post("/api/skills", json={"name": "", "body": "x"}).status_code == 400
    s = client.post("/api/skills", json={"name": "旧名", "description": "d", "body": "正文", "scope": "group"}).json()
    assert s["scope"] == "group" and s["body"] == "正文"
    assert client.post("/api/skills", json={"name": "旧名", "body": "y"}).status_code == 409
    client.patch(f"/api/agents/{aid}", json={"skills": ["旧名"]})
    client.patch(f"/api/groups/{g}", json={"ext": {"skills": ["旧名"]}})
    r = client.put("/api/skills/旧名", json={"name": "新名", "description": "d2", "body": "正文2", "scope": "group"}).json()
    assert r["name"] == "新名"
    assert [a for a in client.get("/api/agents").json() if a["id"] == aid][0]["skills"] == ["新名"]
    assert client.get("/api/groups").json()[0]["ext"]["skills"] == ["新名"]
    assert client.get("/api/skills/旧名").status_code == 404
    assert client.delete("/api/skills/新名").status_code == 200
    assert client.get("/api/groups").json()[0]["ext"]["skills"] == []
    assert [a for a in client.get("/api/agents").json() if a["id"] == aid][0]["skills"] == []


# ------------------------------------------------------------------- backups
def test_export_strips_secrets_by_default(client, tmp_path):
    client.patch("/api/providers/deepseek", json={"api_key": "sk-topsecret"})
    client.put("/api/settings", json={"github_token": "ghp_topsecret"})
    m = client.post("/api/mcp", json={"name": "r", "url": "https://x.example/mcp", "headers": {"A": "topsecret"}}).json()
    assert m

    def dump(path: Path) -> str:
        db = sqlite3.connect(path)
        try:
            return "\n".join(str(r) for t in ("providers", "mcp_servers", "settings") for r in db.execute(f"SELECT * FROM {t}"))
        finally:
            db.close()

    f = tmp_path / "b.db"
    f.write_bytes(client.get("/api/data/export").content)
    assert "topsecret" not in dump(f)
    f2 = tmp_path / "b2.db"
    f2.write_bytes(client.get("/api/data/export", params={"include_keys": True}).content)
    assert "topsecret" in dump(f2)


def test_upgrade_moves_old_documents_into_knowledge_bases(tmp_path):
    """A database from before knowledge bases kept each document in one bucket: a group's
    library, or the shared one. Opening it must turn each bucket into one knowledge base that
    keeps exactly that visibility, and drop the column it no longer needs.
    """
    import sqlite3

    from app.store import Store

    d = tmp_path / "old"
    d.mkdir()
    db = sqlite3.connect(d / "team-agent.db")
    db.execute("CREATE TABLE library_docs (id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', "
               "kind TEXT NOT NULL DEFAULT 'note', size INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0, "
               "chunks INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
               "group_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
    db.execute("INSERT INTO library_docs(id,title,kind,created_at,group_id) VALUES('shared1','旧共享','txt',1.0,'')")
    db.execute("INSERT INTO library_docs(id,title,kind,created_at,group_id) VALUES('priv1','旧私有','txt',1.0,'grp1')")
    db.commit()
    db.close()

    st = Store(d)
    kbs = {k["group_id"]: k for k in st.list_kbs()}
    assert set(kbs) == {"", "grp1"}
    assert kbs[""].name if hasattr(kbs[""], "name") else kbs[""]["name"]
    assert [d_["title"] for d_ in st.list_docs(kbs[""]["id"])] == ["旧共享"]
    assert [d_["title"] for d_ in st.list_docs(kbs["grp1"]["id"])] == ["旧私有"]
    # The redundant column is gone
    assert "group_id" not in {r["name"] for r in st._q("PRAGMA table_info(library_docs)")}

    # Reopening must not migrate a second time
    again = Store(d)
    assert len(again.list_kbs()) == 2 and len(again.list_docs()) == 2


def test_upgrade_from_the_very_first_library_shape(tmp_path):
    """Before the library was per group there was no `group_id` column at all: everything was
    global, so everything belongs in the shared knowledge base."""
    import sqlite3

    from app.store import Store

    d = tmp_path / "ancient"
    d.mkdir()
    db = sqlite3.connect(d / "team-agent.db")
    db.execute("CREATE TABLE library_docs (id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', "
               "kind TEXT NOT NULL DEFAULT 'note', size INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0, "
               "chunks INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)")
    db.execute("INSERT INTO library_docs(id,title,kind,created_at) VALUES('a','古早资料','txt',1.0)")
    db.commit()
    db.close()

    st = Store(d)
    kbs = st.list_kbs()
    assert len(kbs) == 1 and kbs[0]["group_id"] == ""
    assert [d_["title"] for d_ in st.list_docs(kbs[0]["id"])] == ["古早资料"]


def test_upgrade_from_old_database_backfills_seed_tags_once(tmp_path):
    """A v0.2 database has no role strengths on its built-in members: upgrade fills
    them in once; anything the user clears or edits afterwards stays put.
"""
    from app.store import Store

    s = Store(tmp_path / "d")
    a = next(x for x in s.list_agents() if x["name"] == "Copywriter")
    s.update_agent(a["id"], {"tags": []})
    s._x("DELETE FROM meta WHERE key='backfill_seed_tags'")           # pretend the flag was never applied
    s2 = Store(tmp_path / "d")
    assert next(x for x in s2.list_agents() if x["name"] == "Copywriter")["tags"]
    s2.update_agent(a["id"], {"tags": []})
    s3 = Store(tmp_path / "d")
    assert next(x for x in s3.list_agents() if x["name"] == "Copywriter")["tags"] == []


def test_an_old_document_with_no_owner_at_all_is_still_assigned(tmp_path):
    """`group_id` was never declared NOT NULL, so an old row can carry NULL rather than ''.

    Read as NULL it went to `add_kb` unchanged, and `knowledge_bases.group_id` *is* NOT NULL — so
    opening the database raised `IntegrityError: NOT NULL constraint failed` and the application
    could not start at all. NULL means here exactly what the empty string already means: "no
    group", i.e. the shared knowledge base.
    """
    import sqlite3

    from app.store import Store

    d = tmp_path / "nulldb"
    d.mkdir()
    db = sqlite3.connect(d / "team-agent.db")
    db.execute("CREATE TABLE library_docs (id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', "
               "kind TEXT NOT NULL DEFAULT 'note', size INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0, "
               "chunks INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
               "group_id TEXT DEFAULT '', created_at REAL NOT NULL)")
    db.execute("INSERT INTO library_docs(id,title,kind,created_at,group_id) VALUES('n1','无主资料','txt',1.0,NULL)")
    db.execute("INSERT INTO library_docs(id,title,kind,created_at,group_id) VALUES('s1','共享资料','txt',1.0,'')")
    db.commit()
    db.close()

    st = Store(d)
    kbs = st.list_kbs()
    assert len(kbs) == 1, f"one shared base, not one per startup: {[k['group_id'] for k in kbs]}"
    assert sorted(x["title"] for x in st.list_docs(kbs[0]["id"])) == ["共享资料", "无主资料"]

    again = Store(d)                       # the second open has nothing left to do
    assert len(again.list_kbs()) == 1 and len(again.list_docs()) == 2


def test_a_partial_library_patch_keeps_the_rest_of_the_selection(tmp_path):
    """`library` is the one `ext` key that is an object, and `ext` is merged one level deep.

    A patch carrying only `kb_ids` therefore replaced the whole object and `normalize_ext` filled
    `mode` with its default — which is `all`, i.e. every knowledge base the group can reach. A
    caller that means "tick one more box" would have widened the group's search well past the
    boxes that are ticked, and nothing would have said so.
    """
    from app.store import Store

    st = Store(tmp_path / "d")
    g = st.create_group("G")
    st.update_group(g["id"], {"ext": {"library": {
        "mode": "selected", "kb_ids": ["kb1"], "collection_ids": ["c1"]}}})
    st.update_group(g["id"], {"ext": {"library": {"kb_ids": ["kb1", "kb2"]}}})
    lib = st.get_group(g["id"])["ext"]["library"]
    assert lib["mode"] == "selected", "the mode was reset to the default, which is wider"
    assert lib["collection_ids"] == ["c1"]
    assert lib["kb_ids"] == ["kb1", "kb2"]

    # A sibling key is still replaced wholesale — only `library` is merged deeper.
    st.update_group(g["id"], {"ext": {"plan": "off"}})
    assert st.get_group(g["id"])["ext"]["plan"] == "off"
    assert st.get_group(g["id"])["ext"]["library"]["mode"] == "selected"
