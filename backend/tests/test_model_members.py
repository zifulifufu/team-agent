"""Models as members: pull "a model I added" straight into the group."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FakeLLM


def make(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    return TestClient(app, base_url="http://127.0.0.1"), app.state.store


def first_group(c):
    return c.get("/api/groups").json()[0]


def test_from_model_creates_member_and_reuses(tmp_path):
    c, store = make(tmp_path)
    g = first_group(c)
    model = next(m for m in store.list_models() if m["provider_id"] == "deepseek")
    r = c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]})
    assert r.status_code == 200
    ag = [a for a in c.get("/api/agents").json() if a.get("origin") == "model"]
    assert len(ag) == 1 and ag[0]["model_id"] == model["id"]
    assert ag[0]["id"] in r.json()["member_ids"]
    assert " " not in ag[0]["name"] and "@" not in ag[0]["name"]
    # pulling it a second time reuses the same member instead of creating another
    c.delete(f"/api/groups/{g['id']}/members/{ag[0]['id']}")
    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]})
    assert len([a for a in c.get("/api/agents").json() if a.get("origin") == "model"]) == 1


def test_model_member_strengths_and_capabilities(tmp_path):
    c, store = make(tmp_path)
    g = first_group(c)
    model = next(m for m in store.list_models() if m["provider_id"] == "ollama")
    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]})
    caps = c.get(f"/api/groups/{g['id']}/capabilities").json()
    row = next(m for m in caps["members"] if m["origin"] == "model")
    assert row["manual_model"] is True and row["model"]["id"] == model["id"]
    assert row["strengths"] == model["strengths"][:6]  # a model member inherits the model strengths


def test_disabled_or_unknown_model_rejected(tmp_path):
    c, store = make(tmp_path)
    g = first_group(c)
    model = store.list_models()[0]
    assert c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": "nope/x"}).status_code == 404
    c.patch(f"/api/models/{model['id']}", json={"enabled": False})
    assert c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]}).status_code == 400


def test_name_collision_gets_suffix(tmp_path):
    c, store = make(tmp_path)
    g = first_group(c)
    model = store.list_models()[0]
    c.post("/api/agents", json={"name": model["display_name"].replace(" ", "-")})
    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]})
    names = [a["name"] for a in c.get("/api/agents").json()]
    assert len(names) == len(set(names))


def test_model_member_cannot_switch_model_and_dies_with_model(tmp_path):
    c, store = make(tmp_path)
    g = first_group(c)
    models = store.list_models()
    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": models[0]["id"]})
    ag = next(a for a in c.get("/api/agents").json() if a.get("origin") == "model")
    assert c.patch(f"/api/agents/{ag['id']}", json={"model_id": models[-1]["id"]}).status_code == 400
    assert c.patch(f"/api/agents/{ag['id']}", json={"model_id": None}).status_code == 400
    assert c.patch(f"/api/agents/{ag['id']}", json={"role": "改个岗位"}).status_code == 200
    c.delete(f"/api/models/{models[0]['id']}")
    assert not [a for a in c.get("/api/agents").json() if a.get("origin") == "model"]


def test_model_member_speaks_with_its_own_model(tmp_path):
    """@-ing a model member really sends the request to that model (no automatic
    strength-based routing)."""
    fake = FakeLLM(default="收到")
    app = create_app(tmp_path / "data", completion_fn=fake)
    c, store = TestClient(app, base_url="http://127.0.0.1"), app.state.store
    g = c.get("/api/groups").json()[0]
    model = next(m for m in store.list_models() if m["provider_id"] == "ollama")
    ag_group = c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]}).json()
    ag = next(a for a in c.get("/api/agents").json() if a.get("origin") == "model")
    assert ag["id"] in ag_group["member_ids"]
    c.post(f"/api/groups/{g['id']}/messages", json={"text": f"@{ag['name']} 你好"})
    for _ in range(50):
        msgs = c.get(f"/api/groups/{g['id']}/messages").json()
        if any(m["sender_id"] == ag["id"] for m in msgs):
            break
        time.sleep(0.1)
    mine = [m for m in msgs if m["sender_id"] == ag["id"]]
    assert mine and mine[-1]["model_id"] == model["id"]
    assert fake.calls and fake.calls[0][0].startswith("ollama")


def test_unusable_pinned_model_is_reported(tmp_path):
    """Adding DeepSeek to a group before its key is filled in: the member card has to
    make it visible that the pinned model cannot be used right now and another one
    is standing in for it.
"""
    c, store = make(tmp_path)
    g = first_group(c)
    model = next(m for m in store.list_models() if m["provider_id"] == "deepseek")
    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]})
    row = next(m for m in c.get(f"/api/groups/{g['id']}/capabilities").json()["members"] if m["origin"] == "model")
    assert row["model_problem"] and row["model"]["id"] != model["id"]
    c.patch("/api/providers/deepseek", json={"api_key": "sk-abcdef123456"})
    row = next(m for m in c.get(f"/api/groups/{g['id']}/capabilities").json()["members"] if m["origin"] == "model")
    assert row["model_problem"] == "" and row["model"]["id"] == model["id"]
