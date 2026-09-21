"""示例库(接入 awesome-llm-apps):本地快照浏览、团队→群聊、角色→成员/提示词、技能、MCP(一律停用)、从本地克隆刷新。

注意:程序**不内置**上游内容,所以这里的用例都用自造样本:
  * `fixtures/awesome_sample.json` —— 模拟「用户自己 clone 上游后刷新」得到的本地快照(合成数据,非上游内容);
  * `_fake_clone()` —— 模拟本地 clone 目录,用来验证 refresh 只做静态提取。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import awesome_apps as aa
from app.main import create_app
from tests.conftest import FakeLLM

SAMPLE = Path(__file__).parent / "fixtures" / "awesome_sample.json"


@pytest.fixture
def client(tmp_path):
    """数据目录里先放一份本地快照 —— 等价于用户点过「从本地克隆刷新」。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, data / "awesome_apps.json")
    app = create_app(data, completion_fn=FakeLLM(default="好的"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = data
        yield c


@pytest.fixture
def bare_client(tmp_path):
    """没有本地快照的干净安装。"""
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = tmp_path / "data"
        yield c


# --------------------------------------------------------------- 不再内置第三方内容
def test_without_local_snapshot_the_page_is_empty_and_does_not_crash(bare_client):
    """程序不附带上游内容:没有本地快照时应显示为空,而不是报错或回落到内置数据。"""
    data, origin = aa.load(Path("/nonexistent"))
    assert origin == "empty" and aa.validate(data) is None

    d = bare_client.get("/api/awesome").json()
    assert d["origin"] == "empty"
    assert d["teams"] == [] and d["agents"] == [] and d["skills"] == [] and d["mcp"] == []
    assert d["source"]["license"] == "Apache-2.0"          # 出处照旧标注,便于用户自己去找上游
    assert not aa.SHIPPED.exists()                          # 仓库里确实不带内置快照


def test_overview_lists_local_snapshot(client):
    d = client.get("/api/awesome").json()
    assert d["origin"] == "local" and d["commit"] == "test-fixture"
    assert {t["title"] for t in d["teams"]} == {"Legal Analysis Team", "News Pipeline"}
    t = next(x for x in d["teams"] if x["id"] == "gen-legal-team")
    assert {m["name"] for m in t["members"]} == {"Legal Researcher", "Contract Analyst", "Legal Strategist"}
    assert t["lead"] == "Legal-Team-Lead"
    assert any(s["needs_runtime"] for s in d["skills"]) and any(not s["needs_runtime"] for s in d["skills"])


def test_chinese_overlay_is_read_from_data_dir(client):
    """中文覆盖层由使用者自己放在数据目录里(程序不再内置 awesome_zh.json)。"""
    (client.data / "awesome_zh.json").write_text(json.dumps({
        "apps": {"advanced_ai_agents/multi_agent_apps/agent_teams/legal_team": {"title": "法律分析团队", "desc": "中文简介"}},
        "skills": {"Demo Pure Skill": {"title": "纯文字技能", "desc": "中文说明"}},
        "mcp": {"filesystem": "中文提示"},
    }, ensure_ascii=False), encoding="utf-8")
    d = client.get("/api/awesome").json()
    t = next(x for x in d["teams"] if x["id"] == "gen-legal-team")
    assert t["title"] == "法律分析团队" and t["desc"] == "中文简介"
    assert t["title_en"] == "Legal Analysis Team"
    sk = next(s for s in d["skills"] if s["name"] == "Demo Pure Skill")
    assert sk["title"] == "纯文字技能"
    m = next(x for x in d["mcp"] if x["name"] == "filesystem")
    assert m["note"] == "中文提示"


# --------------------------------------------------------------- 导入
def test_create_group_from_team_with_lead_becomes_host(client):
    t = next(x for x in client.get("/api/awesome").json()["teams"] if x["id"] == "gen-legal-team")
    r = client.post(f"/api/awesome/apps/{t['id']}/create-group", json={}).json()
    assert r["host"].startswith("Legal-Team-Lead") and len(r["members"]) == 4
    grp = next(x for x in client.get("/api/groups").json() if x["id"] == r["group"]["id"])
    assert grp["name"] == "Legal Analysis Team"
    assert "awesome-llm-apps" in grp["prompt"] and "Apache-2.0" in grp["prompt"]
    host = next(a for a in client.get("/api/agents").json() if a["id"] == grp["host_agent_id"])
    assert "Coordinate analysis" in host["prompt"]
    # 再建一次:名字自动避让,不会顶掉已有成员
    r2 = client.post(f"/api/awesome/apps/{t['id']}/create-group", json={"name": "法律 2"}).json()
    assert set(r["members"]).isdisjoint(r2["members"])


def test_create_group_without_lead_uses_existing_coordinator_and_sequential_hint(client):
    t = next(x for x in client.get("/api/awesome").json()["teams"] if x["sequential"] and not x["lead"])
    r = client.post(f"/api/awesome/apps/{t['id']}/create-group", json={}).json()
    assert r["host"] == "小助"
    grp = next(x for x in client.get("/api/groups").json() if x["id"] == r["group"]["id"])
    assert "流水线" in grp["prompt"]


def test_add_members_and_prompts_dedupe(client):
    a = next(x for x in client.get("/api/awesome").json()["agents"] if x["id"] == "gen-invest-agent")
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
    pure_body = client.get(f"/api/skills/{pure['name']}").json()["body"]
    assert "没有那些程序" not in pure_body
    assert "被截短" in pure_body                                    # clipped 的技能会加提示
    assert next(s for s in client.get("/api/awesome").json()["skills"] if s["id"] == sk["id"])["installed"] is True


def test_mcp_added_disabled_without_secrets_and_tmp_dir_replaced(client):
    d = client.get("/api/awesome").json()
    for m in d["mcp"]:
        assert client.post(f"/api/awesome/mcp/{m['id']}/add").status_code == 200
    rows = {m["name"]: m for m in client.get("/api/mcp").json()}
    assert all(not r["enabled"] for r in rows.values())                        # 一律停用:要你自己核对后再启用
    assert "/tmp" not in rows["filesystem"]["args"] and "/path/to/allowed/dir" in rows["filesystem"]["args"]
    assert client.post(f"/api/awesome/mcp/{d['mcp'][0]['id']}/add").status_code == 409
    assert all(not v for r in rows.values() for v in (r.get("env") or {}).values() if not str(v).startswith("•"))   # 没有替你填任何值


# --------------------------------------------------------------- 从本地克隆刷新
def _fake_clone(root: Path):
    """一个自造的「上游 clone」目录,用来验证提取只读源码、绝不执行代码。"""
    app = root / "advanced_ai_agents" / "multi_agent_apps" / "agent_teams" / "demo_team"
    app.mkdir(parents=True)
    (root / "README.md").write_text("# awesome\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache License\nVersion 2.0, January 2004\n", encoding="utf-8")
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
    # 写出来的快照里不能有任何密钥值:只有变量名
    raw = (client.data / "awesome_apps.json").read_text(encoding="utf-8")
    assert not re.search(r"sk-[A-Za-z0-9]{16}|AIza[0-9A-Za-z_-]{20}|ghp_[A-Za-z0-9]{20}", raw)
    assert client.post("/api/awesome/reset").json() == {"ok": True}
    assert client.get("/api/awesome").json()["origin"] == "empty"             # 重置后回到「无数据」,不回落到内置


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
    assert client.get("/api/awesome").json()["origin"] == "local"             # 失败的刷新不能破坏已有快照
