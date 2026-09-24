"""The process engineer: a member that watches every group and appears in none of them.

The role is only worth having if the invisibility actually holds, and that is what is asserted here
rather than the wording of its prompt:

  * it is kept in every group (and in a group made later), by the setting, idempotently;
  * it is **nowhere a member can be chosen**: not in the member list, not in a group's member ids,
    not an `@`-candidate, never the host — because a watcher that can be addressed or that can end up
    in the chair changes how the group works, which is the opposite of watching it;
  * it is **not offered** in the picker or the template gallery: it is already in the group, so
    "add it" would be a promise the app cannot keep;
  * and the one place it *is* visible — the process panel — reports where it is and what it found.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import presets, proclog, templates, tools
from app.main import create_app
from tests.conftest import FakeLLM

PRESET_KEY = "process"
SKILL_KEY = "process-audit"
SKILL_NAME = "Workflow audit"


def client(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="OK"))
    return TestClient(app, base_url="http://127.0.0.1"), app


def watcher(store):
    return templates.process_engineer(store)


# ------------------------------------------------------------------ the definition itself
def test_the_role_exists_in_both_languages_and_is_marked_system():
    entry = presets.AGENT_PRESET_BY_KEY[PRESET_KEY]
    for field in ("name", "name_zh", "avatar", "role", "role_zh", "tags"):
        assert entry.get(field), field
    assert entry["name"] == "Process engineer" and entry["name_zh"] == "流程工程师"
    # The marker is what keeps it out of the picker, and it is also the flag the app reads to decide
    # what to keep in every group.
    assert entry.get("system") is True
    assert PRESET_KEY in presets.SYSTEM_PRESET_KEYS

    for field in ("prompt", "prompt_zh"):
        text = str(entry[field])
        assert len(text) >= 200, f"{field} is too thin to steer anything"
        # This prompt is not decoration: it is the system prompt of the review pass.
        assert "verified" in text
    for marker in ("process_log", "scan", "report"):
        assert marker in str(entry["prompt"]), marker


def test_it_is_not_offered_as_a_member_to_add(tmp_path):
    assert PRESET_KEY not in {p["key"] for p in presets.offered_presets()}
    assert PRESET_KEY in presets.AGENT_PRESET_BY_KEY          # …but it is still a known definition

    cl, _ = client(tmp_path)
    assert PRESET_KEY not in {p["key"] for p in cl.get("/api/agent-presets").json()}
    # The gallery installs members into groups, so it must not list it either.
    assert PRESET_KEY not in {e["id"] for e in cl.get("/api/gallery").json().get("entries", [])
                              if e["kind"] == "agent"}


def test_the_skill_is_complete_and_filed_under_a_real_category():
    ex = tools.EXAMPLE_SKILLS[SKILL_KEY]
    for field in ("name", "name_zh", "description", "description_zh", "body", "body_zh", "scope"):
        assert ex.get(field), field
    assert ex["scope"] == "member"
    assert tools.BUILTIN_SKILL_CATEGORY[SKILL_KEY] in tools.SKILL_CATEGORY_ORDER
    for marker in ("action=scan", "action=report", "verified", "ask_advisor"):
        assert marker in ex["body"], marker
        assert marker in ex["body_zh"], marker
    # The role carries it, so the review pass reads the same discipline a person would.
    assert presets.AGENT_PRESET_BY_KEY[PRESET_KEY]["skills"] == [SKILL_NAME]


# ------------------------------------------------------------------ kept in every group
def test_it_joins_every_group_at_startup_and_stays_out_of_sight(tmp_path):
    cl, app = client(tmp_path)
    store = app.state.store
    agent = watcher(store)
    assert agent is not None and agent["hidden"] is True

    gid = store.list_groups()[0]["id"]
    raw = store.member_ids(gid, include_hidden=True)
    assert agent["id"] in raw                                   # it is in the group…
    assert agent["id"] not in store.member_ids(gid)             # …and in none of the visible lists
    assert agent["id"] not in cl.get("/api/groups").json()[0]["member_ids"]
    assert all(m["id"] != agent["id"] for m in store.group_members(gid))
    assert agent["name"] not in {a["name"] for a in cl.get("/api/agents").json()}
    assert agent["name"] not in {g["name"] for g in store.list_agents()}


def test_a_group_made_later_gets_it_too(tmp_path):
    cl, app = client(tmp_path)
    store = app.state.store
    agent = watcher(store)
    me = store.list_agents()[0]["id"]

    created = cl.post("/api/groups", json={"name": "新的群", "host_agent_id": me, "member_ids": [me]}).json()
    assert agent["id"] in store.member_ids(created["id"], include_hidden=True)
    assert store.member_ids(created["id"]) == [me]               # the user's own list is untouched
    # And the chair is the member the user picked, never the watcher.
    assert created["host_agent_id"] == me


def test_joining_twice_changes_nothing(tmp_path):
    cl, app = client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    before = len(store.member_ids(gid, include_hidden=True))

    assert templates.keep_process_engineer(store) == []          # already in every group
    templates.keep_process_engineer(store, gid)
    assert len(store.member_ids(gid, include_hidden=True)) == before


def test_a_row_that_was_created_visible_is_hidden_by_the_auto_path(tmp_path):
    """The installs that have been around longest are the ones that need this.

    A row created by a version that kept the watcher as an ordinary member would stay visible for
    ever, and "in every group, in none of them" would be quietly false — so the auto path puts it
    back rather than trusting how it was created.
    """
    cl, app = client(tmp_path, name="data_visible")
    store = app.state.store
    agent = watcher(store)
    store.set_agent_hidden(agent["id"], False)
    assert watcher(store)["hidden"] is False

    templates.keep_process_engineer(store)
    assert watcher(store)["hidden"] is True
    assert watcher(store)["name"] not in {a["name"] for a in cl.get("/api/agents").json()}


def test_the_switch_off_means_off(tmp_path):
    cl, app = client(tmp_path, name="data_off")
    store = app.state.store
    store.update_settings({"process_autojoin": False})
    gid = store.list_groups()[0]["id"]
    store.remove_member(gid, watcher(store)["id"])

    assert templates.keep_process_engineer(store) == []
    assert watcher(store)["id"] not in store.member_ids(gid, include_hidden=True)
    # Off is a real off: a member the user removed does not come back on the next startup.
    assert templates.keep_process_engineer(store) == []


# ------------------------------------------------------------------ never a member of the round
def test_it_can_never_be_addressed_or_take_a_turn(tmp_path, make_router):
    from app import orchestrator

    cl, app = client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    agent = watcher(store)
    members = store.group_members(gid)

    for spelling in ("@Process engineer", "@流程工程师", "@所有人"):
        found = orchestrator.find_mentions(f"{spelling} 看一下", members)
        assert all(m["id"] != agent["id"] for m in found), spelling
    # …and a group whose host slot somehow points at it falls back to a real member.
    store.update_group(gid, {"host_agent_id": agent["id"]})
    group = store.get_group(gid)
    picked = orchestrator.Orchestrator._pick_host(group, members)
    assert picked["id"] != agent["id"]


def test_it_never_says_anything_in_a_round(tmp_path, store, make_router):
    from tests.test_collab import Collector, setup

    _, g = setup(store, make_router, FakeLLM(default="好的"))
    name = watcher(store)["name"]
    orch = setup  # noqa: F841 — the setup below is the same orchestrator, spelled out for clarity
    orch, g = setup(store, make_router, FakeLLM(default="好的"))
    c = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "随便说点什么", c))
    asyncio.run(orch.drain())
    assert name not in {m.get("sender_name") for m in store.list_messages(g["id"])}


# ------------------------------------------------------------------ the one place it shows
def test_the_process_panel_reports_where_it_is_and_what_it_found(tmp_path):
    cl, app = client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    workspace = store.workspace_dir(gid)
    entries: list = []
    proclog.report(entries, title="交付物没落盘", symptom="说了话但文件不在", severity="blocker",
                   stage="delivery", key="missing-file:报告.docx", by="auto")
    proclog.write(proclog.unit(workspace), entries, "群")

    st = cl.get("/api/process").json()
    assert st["name"] == "Process engineer" and st["hidden"] is True
    assert st["groups"] == 1 and st["in_groups"] == 1 and st["not_in"] == []
    assert st["entries"]["open"] == 1 and st["recent"][0]["title"] == "交付物没落盘"
    assert st["autojoin"] and st["autolog"] and st["review"]
    assert st["recent"][0]["by"] == "auto"
