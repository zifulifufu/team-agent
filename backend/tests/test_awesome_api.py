"""示例库(awesome-llm-apps 融合):快照浏览、团队→群聊、角色→成员/提示词、技能、MCP(一律停用)、从本地克隆刷新。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import awesome_apps as aa
from app.main import create_app
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = tmp_path / "data"
        yield c


def test_shipped_snapshot_is_valid_and_has_chinese_overlay():
    data, origin = aa.load(Path("/nonexistent"))
    assert origin == "shipped" and aa.validate(data) is None
    assert data["source"]["license"] == "Apache-2.0" and data["source"]["url"].startswith("https://github.com/")
    assert len(data["teams"]) >= 20 and len(data["skills"]) >= 5 and len(data["mcp"]) >= 5
    zh = json.loads((Path(aa.__file__).parent / "data" / "awesome_zh.json").read_text(encoding="utf-8"))
    for a in data["teams"] + data["agents"]:
        assert a["path"] in zh["apps"], a["path"]
    assert {s["name"] for s in data["skills"]} == set(zh["skills"]) and {m["name"] for m in data["mcp"]} == set(zh["mcp"])


def test_snapshot_never_contains_secret_values():
    raw = (Path(aa.__file__).parent / "data" / "awesome_apps.json").read_text(encoding="utf-8")
    import re
    assert not re.search(r"sk-[A-Za-z0-9]{16}|AIza[0-9A-Za-z_-]{20}|ghp_[A-Za-z0-9]{20}", raw)
    for m in json.loads(raw)["mcp"]:
        assert isinstance(m.get("env_keys", []), list) and "env" not in m      # 只有变量名,没有值


def test_overview_lists_everything_with_chinese_titles(client):
    d = client.get("/api/awesome").json()
    assert d["origin"] == "shipped" and d["source"]["license"] == "Apache-2.0" and d["commit"]
    t = next(x for x in d["teams"] if x["path"].endswith("ai_legal_agent_team") and "local" not in x["path"])
    assert t["title"] == "法律分析团队" and {m["name"] for m in t["members"]} == {"Legal Researcher", "Contract Analyst", "Legal Strategist"}
    assert all(s["title"] for s in d["skills"]) and any(s["needs_runtime"] for s in d["skills"]) and any(not s["needs_runtime"] for s in d["skills"])


def test_create_group_from_team_with_lead_becomes_host(client):
    d = client.get("/api/awesome").json()
    t = next(x for x in d["teams"] if x["path"].endswith("agent_teams/ai_legal_agent_team"))
    r = client.post(f"/api/awesome/apps/{t['id']}/create-group", json={}).json()
    assert r["host"].startswith("Legal-Team-Lead") and len(r["members"]) == 4
    g = client.get("/api/groups").json()
    grp = next(x for x in g if x["id"] == r["group"]["id"])
    assert grp["name"] == "法律分析团队" and "awesome-llm-apps" in grp["prompt"] and "Apache-2.0" in grp["prompt"]
    host = next(a for a in client.get("/api/agents").json() if a["id"] == grp["host_agent_id"])
    assert "Coordinate analysis" in host["prompt"]
    # 再建一次:名字自动避让,不会顶掉已有成员
    r2 = client.post(f"/api/awesome/apps/{t['id']}/create-group", json={"name": "法律 2"}).json()
    assert set(r["members"]).isdisjoint(r2["members"])


def test_create_group_without_lead_uses_existing_coordinator_and_sequential_hint(client):
    d = client.get("/api/awesome").json()
    t = next(x for x in d["teams"] if x["sequential"] and not x["lead"])
    r = client.post(f"/api/awesome/apps/{t['id']}/create-group", json={}).json()
    assert r["host"] == "小助"
    grp = next(x for x in client.get("/api/groups").json() if x["id"] == r["group"]["id"])
    assert "流水线" in grp["prompt"]


def test_add_members_and_prompts_dedupe(client):
    d = client.get("/api/awesome").json()
    a = next(x for x in d["agents"] if x["path"].endswith("ai_investment_agent"))
    gid = client.get("/api/groups").json()[0]["id"]
    r = client.post(f"/api/awesome/apps/{a['id']}/members", json={"group_id": gid}).json()
    assert len(r["members"]) == 1
    g = next(x for x in client.get("/api/groups").json() if x["id"] == gid)
    assert r["members"][0]["id"] in g["member_ids"]
    p1 = client.post(f"/api/awesome/apps/{a['id']}/prompts", json={}).json()
    p2 = client.post(f"/api/awesome/apps/{a['id']}/prompts", json={}).json()
    assert len(p1["added"]) == 1 and p1["added"][0].startswith("[示例]") and p2["added"] == [] and len(p2["skipped"]) == 1
    assert client.post("/api/awesome/apps/nope/members", json={}).status_code == 404


def test_skill_install_marks_runtime_dependency_and_refuses_duplicates(client):
    d = client.get("/api/awesome").json()
    sk = next(s for s in d["skills"] if s["needs_runtime"])
    assert client.post(f"/api/awesome/skills/{sk['id']}/install").status_code == 200
    body = client.get(f"/api/skills/{sk['name']}").json()["body"]
    assert "没有那些程序" in body
    assert client.post(f"/api/awesome/skills/{sk['id']}/install").status_code == 409
    pure = next(s for s in d["skills"] if not s["needs_runtime"])
    client.post(f"/api/awesome/skills/{pure['id']}/install")
    assert "没有那些程序" not in client.get(f"/api/skills/{pure['name']}").json()["body"]
    assert next(s for s in client.get("/api/awesome").json()["skills"] if s["id"] == sk["id"])["installed"] is True


def test_mcp_added_disabled_without_secrets_and_tmp_dir_replaced(client):
    d = client.get("/api/awesome").json()
    for m in d["mcp"]:
        assert client.post(f"/api/awesome/mcp/{m['id']}/add").status_code == 200
    rows = {m["name"]: m for m in client.get("/api/mcp").json()}
    assert all(not r["enabled"] for r in rows.values())                       # 一律停用:要你自己核对后再启用
    assert "/tmp" not in rows["filesystem"]["args"] and "/path/to/allowed/dir" in rows["filesystem"]["args"]
    assert client.post(f"/api/awesome/mcp/{d['mcp'][0]['id']}/add").status_code == 409
    assert all(not v for r in rows.values() for v in (r.get("env") or {}).values() if not str(v).startswith("•"))   # 没有替你填任何值


def _fake_clone(root: Path):
    (root / "advanced_ai_agents" / "multi_agent_apps" / "agent_teams" / "demo_team").mkdir(parents=True)
    (root / "README.md").write_text("# awesome\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache License\nVersion 2.0, January 2004\n", encoding="utf-8")
    app = root / "advanced_ai_agents" / "multi_agent_apps" / "agent_teams" / "demo_team"
    (app / "README.md").write_text("# Demo Team\n\nA tiny demo team.\n", encoding="utf-8")
    (app / "app.py").write_text(
        "from agno.agent import Agent\n"
        "a = Agent(name='Alpha', role='first', instructions='Do alpha things')\n"
        "b = Agent(name='Beta', role='second', instructions='Do beta things')\n"
        "import os\nos.system('touch /tmp/should_never_run_from_extract')\n", encoding="utf-8")


def test_refresh_from_local_clone_is_static_only(client, tmp_path):
    root = tmp_path / "clone"
    _fake_clone(root)
    marker = Path("/tmp/should_never_run_from_extract")
    marker.unlink(missing_ok=True)
    r = client.post("/api/awesome/refresh", json={"path": str(root)})
    assert r.status_code == 200, r.text
    assert not marker.exists()                                                # 只读源码、绝不执行
    d = client.get("/api/awesome").json()
    assert d["origin"] == "local" and any(t["title"] == "Demo Team" for t in d["teams"] + d["agents"])
    assert client.post("/api/awesome/reset").json() == {"ok": True}
    assert client.get("/api/awesome").json()["origin"] == "shipped"


def test_refresh_rejects_bad_paths_and_keeps_snapshot(client, tmp_path):
    assert client.post("/api/awesome/refresh", json={"path": "relative/dir"}).status_code == 400
    assert client.post("/api/awesome/refresh", json={"path": str(tmp_path / "nope")}).status_code == 400
    empty = tmp_path / "empty"
    empty.mkdir()
    assert client.post("/api/awesome/refresh", json={"path": str(empty)}).status_code == 400
    fake_lic = tmp_path / "gpl"
    _fake_clone(fake_lic)
    (fake_lic / "LICENSE").write_text("GNU GENERAL PUBLIC LICENSE\n", encoding="utf-8")
    assert client.post("/api/awesome/refresh", json={"path": str(fake_lic)}).status_code == 400
    assert client.get("/api/awesome").json()["origin"] == "shipped"
