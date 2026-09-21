"""Domain experts (`kind: "expert"`) are ordinary member presets, so they are added to a group the
same way — one call to from-preset — and appear in their own section of the picker.

What these tests protect is the *shape* of the library: every expert complete in both languages,
no duplicate keys or names, listed under its own kind, reachable by key, and reusable when the same
expert is added twice. Not the wording of any single prompt.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import presets
from app.main import create_app
from tests.conftest import FakeLLM


def client(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="OK"))
    return TestClient(app, base_url="http://127.0.0.1"), app


def test_every_expert_is_complete_in_both_languages():
    assert presets.EXPERT_PRESETS, "the expert library should not be empty"
    for e in presets.EXPERT_PRESETS:
        assert e["kind"] == "expert", e["key"]
        for field in ("key", "name", "name_zh", "avatar", "role", "role_zh", "tags"):
            assert e.get(field), f"{e['key']} is missing {field}"
        # The prompt is the whole point of an expert: a method and a stated limit. A one-liner would
        # steer nothing, so a floor is worth asserting (it is a floor, not a writing standard).
        for field in ("prompt", "prompt_zh"):
            assert len(str(e[field])) >= 80, f"{e['key']}.{field} is too thin to steer anything"


def test_the_library_has_no_duplicate_keys_or_names():
    keys = [p["key"] for p in presets.AGENT_PRESETS]
    assert len(keys) == len(set(keys)), "two presets share a key"
    # both spellings are matched by name throughout the app, so both have to be unique
    names = [n for p in presets.AGENT_PRESETS for n in (p["name"], p["name_zh"])]
    assert len(names) == len(set(names)), "two presets share a name"


def test_roles_and_experts_are_listed_under_their_own_kind(tmp_path):
    cl, _ = client(tmp_path)
    rows = cl.get("/api/agent-presets").json()
    assert {r["kind"] for r in rows} == {"role", "expert"}
    assert sum(1 for r in rows if r["kind"] == "expert") == len(presets.EXPERT_PRESETS)
    stroke = next(r for r in rows if r["key"] == "stroke")
    assert stroke["name"] and stroke["role"] and stroke["prompt"]


@pytest.mark.parametrize("key", [p["key"] for p in presets.EXPERT_PRESETS])
def test_an_expert_can_be_pulled_into_a_group(tmp_path, key):
    """Every expert, not just a sample: the picker offers all of them, so all of them must work."""
    cl, app = client(tmp_path, name="data_" + key)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    body = presets.AGENT_PRESET_BY_KEY[key]
    r = cl.post(f"/api/groups/{gid}/members/from-preset", json={"key": key})
    assert r.status_code == 200, r.text
    members = store.group_members(gid)
    mine = [m for m in members if m["name"] in (body["name"], body["name_zh"])]
    assert len(mine) == 1, f"{key} did not join as exactly one member"
    assert mine[0]["prompt"].strip(), "the member was created without its expert prompt"


def test_adding_the_same_expert_twice_reuses_the_member(tmp_path):
    cl, app = client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    body = presets.AGENT_PRESET_BY_KEY["ethics"]
    cl.post(f"/api/groups/{gid}/members/from-preset", json={"key": "ethics"})
    cl.post(f"/api/groups/{gid}/members/from-preset", json={"key": "ethics"})
    names = [m["name"] for m in store.group_members(gid)]
    assert names.count(body["name"]) + names.count(body["name_zh"]) == 1
