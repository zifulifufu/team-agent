"""数据测试:老版本数据库升级、备份/恢复往返、较大数据量、Obsidian 往返、资料库导入、指示灯记录。"""

from __future__ import annotations

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.obsidian import ObsidianSync
from app.store import Store
from tests.conftest import FakeLLM

OCT = {"Content-Type": "application/octet-stream"}


def client(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="OK"))
    return TestClient(app, base_url="http://127.0.0.1"), app


# ------------------------------------------------------------ 升级
def make_v031_db(path):
    """造一个 v0.3.1 形状的库:新版才有的列和表都不存在。"""
    st = Store(path.parent / "tmp-build")
    st.add_memory("升级前的记忆", "global", "", "fact")
    g = st.list_groups()[0]
    st.add_message(g["id"], "user", "user", "我", "升级前的消息")
    st.add_mcp("旧 MCP", "npx", ["-y", "x"])
    st.add_doc("旧文档", "a.txt", "txt", 10, ["旧文档内容"])
    src = st._db
    src.commit()
    dst = sqlite3.connect(path)
    src.backup(dst)
    for t in ("model_health", "obsidian_map"):        # v0.4.0 新增的表
        dst.execute(f"DROP TABLE IF EXISTS {t}")
    for table, col in [("agents", "origin"), ("agents", "tags"), ("groups", "ext"), ("groups", "prompt"),
                       ("mcp_servers", "transport"), ("mcp_servers", "headers"), ("mcp_servers", "description"),
                       ("models", "strengths")]:
        dst.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    dst.commit()
    cols = {r[1] for r in dst.execute("PRAGMA table_info(agents)")}
    dst.close()
    assert "origin" not in cols and "tags" not in cols
    return path


def test_opening_an_old_database_upgrades_it_in_place(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    make_v031_db(d / "team-agent.db")
    st = Store(d)                                    # 升级发生在这里
    cols = {r["name"] for r in st._q("PRAGMA table_info(agents)")}
    assert {"origin", "tags"} <= cols
    assert st.list_groups()[0]["ext"]["library"]["mode"] in ("all", "off", "selected")
    assert [m["content"] for m in st.list_memories()] == ["升级前的记忆"]
    assert any(x["content"] == "升级前的消息" for x in st.list_messages(st.list_groups()[0]["id"]))
    assert st.list_mcp()[0]["headers"] == {} and st.list_mcp()[0]["name"] == "旧 MCP"
    assert st.list_docs()[0]["title"] == "旧文档"
    st.set_health("deepseek/deepseek-flash", "ok", "", 100, "test")           # 新表可用
    st.set_obsidian_map("m", "a.md", "h")
    assert st.obsidian_map()["m"]["rel_path"] == "a.md"
    st2 = Store(d)                                   # 再开一次(第二次启动)不出错、不重复种子
    assert len(st2.list_agents()) == len(st.list_agents())


def test_old_database_works_through_the_api_and_can_be_backed_up_and_restored(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    make_v031_db(d / "team-agent.db")
    app = create_app(d, completion_fn=FakeLLM(default="OK"))
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/api/groups").status_code == 200 and c.get("/api/models-health").status_code == 200
    backup = c.get("/api/data/export").content
    c2, _ = client(tmp_path, "new")
    r = c2.post("/api/data/restore", content=backup, headers=OCT)
    assert r.status_code == 200 and r.json()["memories"] == 1 and r.json()["docs"] == 1


# ------------------------------------------------------------ 往返
def test_backup_restore_round_trip_preserves_everything_visible(tmp_path):
    a, app_a = client(tmp_path, "a")
    st = app_a.state.store
    g = st.list_groups()[0]
    st.update_settings({"max_hops": 5, "perm_mode": "ask_all", "perm_allow": ["x"]})
    a.post("/api/memories", json={"content": "偏好 A", "kind": "preference", "pinned": True})
    a.post("/api/library/note", json={"title": "文档 A", "content": "独角兽资料"})
    st.add_message(g["id"], "agent", st.list_agents()[0]["id"], "小助", "一条回复", meta={"tools": [{"name": "t", "status": "ok"}]})
    st.set_health("ollama/qwen2.5:7b", "ok", "", 12, "test")
    before = {"groups": a.get("/api/groups").json(), "agents": a.get("/api/agents").json(),
              "messages": a.get(f"/api/groups/{g['id']}/messages").json(), "settings": a.get("/api/settings").json(),
              "memories": a.get("/api/memories").json()["memories"]}
    backup = a.get("/api/data/export").content
    b, _ = client(tmp_path, "b")
    assert b.post("/api/data/restore", content=backup, headers=OCT).status_code == 200
    after = {"groups": b.get("/api/groups").json(), "agents": b.get("/api/agents").json(),
             "messages": b.get(f"/api/groups/{g['id']}/messages").json(), "settings": b.get("/api/settings").json(),
             "memories": b.get("/api/memories").json()["memories"]}
    assert after["groups"] == before["groups"] and after["agents"] == before["agents"] and after["messages"] == before["messages"]
    assert after["memories"] == before["memories"]
    for k in ("max_hops", "perm_mode", "perm_allow", "route_chain", "history_clip"):
        assert after["settings"][k] == before["settings"][k]
    assert b.get("/api/library/search", params={"q": "独角兽"}).json()


def test_restore_twice_in_a_row_keeps_both_safety_copies(tmp_path):
    a, _ = client(tmp_path, "a")
    backup = a.get("/api/data/export").content
    b, _ = client(tmp_path, "b")
    assert b.post("/api/data/restore", content=backup, headers=OCT).status_code == 200
    assert b.post("/api/data/restore", content=backup, headers=OCT).status_code == 200
    assert len(list((tmp_path / "b" / "backups").glob("pre-restore-*.db"))) == 2


# ------------------------------------------------------------ 数据量
def test_larger_dataset_export_restore_and_obsidian(tmp_path):
    a, app_a = client(tmp_path, "a")
    st = app_a.state.store
    g = st.list_groups()[0]
    ag = st.list_agents()[0]
    t0 = time.time()
    for i in range(3000):
        st.add_message(g["id"], "user" if i % 2 else "agent", None if i % 2 else ag["id"], "我" if i % 2 else ag["name"], f"第 {i} 条消息 " + "内容" * 20)
    for i in range(300):
        st.add_memory(f"第 {i} 条记忆:偏好编号 {i} 需要保持一致", "global" if i % 3 else "group", "" if i % 3 else g["id"], "fact")
    for i in range(60):
        a.post("/api/library/note", json={"title": f"文档{i}", "content": ("这是第 %d 份资料。" % i) * 300})
    assert len(a.get(f"/api/groups/{g['id']}/messages").json()) >= 30          # 有 history 上限,但接口可用
    vault = tmp_path / "vault"
    vault.mkdir()
    st.update_settings({"obsidian_dir": str(vault)})
    ob = ObsidianSync(st)
    r = ob.sync()
    assert r["ok"] and r["written"] == 300
    assert not ob.sync()["written"]                                              # 幂等
    backup = a.get("/api/data/export").content
    b, app_b = client(tmp_path, "b")
    res = b.post("/api/data/restore", content=backup, headers=OCT).json()
    assert res["memories"] == 300 and res["docs"] == 60
    assert app_b.state.store._one("SELECT COUNT(*) AS n FROM messages")["n"] >= 3000
    assert time.time() - t0 < 60


def test_obsidian_edit_round_trip_at_scale(tmp_path):
    st = Store(tmp_path / "d")
    vault = tmp_path / "v"
    vault.mkdir()
    st.update_settings({"obsidian_dir": str(vault)})
    for i in range(120):
        st.add_memory(f"事实 {i}", "global", "", "fact")
    ob = ObsidianSync(st)
    ob.sync()
    files = sorted(vault.rglob("*.md"))
    for f in files[:30]:                                       # Obsidian 里改 30 条
        f.write_text(f.read_text(encoding="utf-8").replace("事实", "已改事实"), encoding="utf-8")
    for f in files[30:40]:                                     # 删 10 条
        f.unlink()
    for i in range(15):                                        # 新建 15 条
        (vault / f"新{i}.md").write_text(f"新笔记 {i}", encoding="utf-8")
    r = ob.sync()
    assert (r["pulled"], r["deleted_memories"], r["imported"]) == (30, 10, 15)
    contents = [m["content"] for m in st.list_memories(limit=1000)]
    assert sum(c.startswith("已改事实") for c in contents) == 30 and len(contents) == 120 - 10 + 15


# ------------------------------------------------------------ 资料库 / 指示灯
def test_library_directory_import_recursion_and_reimport(tmp_path):
    a, _ = client(tmp_path)
    root = tmp_path / "docs"
    (root / "子目录").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "a.md").write_text("# 甲\n内容甲", encoding="utf-8")
    (root / "子目录" / "b.txt").write_text("内容乙", encoding="utf-8")
    (root / ".hidden" / "c.txt").write_text("不该被导入", encoding="utf-8")
    (root / "d.exe").write_bytes(b"MZ")
    (root / "e.md").symlink_to(root / "a.md")
    r = a.post("/api/library/dir", json={"path": str(root)}).json()
    assert sorted(d["title"] for d in r["added"]) == ["a", "b"]
    again = a.post("/api/library/dir", json={"path": str(root)}).json()
    assert again["added"] == [] and all(s["reason"] == "已是最新" for s in again["skipped"])
    assert a.post("/api/library/dir", json={"path": "相对路径"}).status_code == 400
    assert a.post("/api/library/dir", json={"path": str(tmp_path / "不存在")}).status_code == 400


def test_health_records_survive_restart_and_are_cleared_on_key_change(tmp_path):
    st = Store(tmp_path / "d")
    st.set_health("deepseek/deepseek-flash", "ok", "", 88, "chat")
    st2 = Store(tmp_path / "d")
    assert st2.all_health()["deepseek/deepseek-flash"]["status"] == "ok"
    st2.update_provider("deepseek", {"api_key": "sk-new-key-123456"})
    assert "deepseek/deepseek-flash" not in st2.all_health()


def test_settings_json_is_never_left_half_written(tmp_path):
    a, app = client(tmp_path)
    for i in range(30):
        assert a.put("/api/settings", json={"max_hops": 1 + i % 10, "route_chain": ["ollama/qwen2.5:7b"]}).status_code == 200
    s = app.state.store.get_settings()
    assert json.dumps(s) and s["route_chain"] == ["ollama/qwen2.5:7b"]
