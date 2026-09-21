"""记忆 ⇄ Obsidian 双向同步:导出、读回、导入、删除、冲突、安全护栏。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.obsidian import ObsidianError, ObsidianSync, parse_note
from tests.conftest import FakeLLM


@pytest.fixture
def env(tmp_path):
    from app.store import Store

    st = Store(tmp_path / "data")
    vault = tmp_path / "vault" / "Team Agent 记忆"
    vault.mkdir(parents=True)
    (vault.parent / ".obsidian").mkdir()
    st.update_settings({"obsidian_dir": str(vault)})
    return st, ObsidianSync(st), vault


def md(vault: Path):
    return sorted(p for p in vault.rglob("*.md") if not p.parent.name.startswith("_"))


def test_export_layout_and_skips_action_kind(env):
    st, ob, vault = env
    g = st.list_groups()[0]
    a = st.list_agents()[0]
    st.add_memory("发布类内容统一写 Team Agent", "global", "", "preference", "manual", True)
    st.add_memory("本群的截止日期是 10 月 18 日", "group", g["id"], "fact")
    st.add_memory("Aide 喜欢先列提纲", "agent", a["id"], "lesson")
    st.add_memory("任务→谁做了什么(流水)", "global", "", "action", "auto")
    r = ob.sync()
    assert r["ok"] and r["written"] == 3
    files = md(vault)
    assert len(files) == 3
    rels = {str(f.relative_to(vault)).split("/")[0] for f in files}
    assert rels == {"Global", "Groups", "Members"}
    assert (vault / "Groups" / g["name"]).is_dir() and (vault / "Members" / a["name"]).is_dir()
    text = next(f for f in files if "Global" in str(f)).read_text(encoding="utf-8")
    ours, other, body = parse_note(text)
    assert ours["scope"] == "global" and ours["kind"] == "preference" and ours["pinned"] is True and body == "发布类内容统一写 Team Agent"
    r2 = ob.sync()                                       # 幂等
    assert (r2["written"], r2["pulled"], r2["imported"], r2["conflicts"]) == (0, 0, 0, 0)


def test_edit_in_obsidian_is_read_back_including_scope_and_pin(env):
    st, ob, vault = env
    g = st.list_groups()[0]
    m = st.add_memory("旧内容", "global", "", "fact")
    ob.sync()
    f = md(vault)[0]
    f.write_text(f.read_text(encoding="utf-8").replace("旧内容", "新内容,在 Obsidian 里改的")
                 .replace("kind: fact", "kind: decision").replace("pinned: false", "pinned: true")
                 .replace("scope: global", "scope: group").replace('scope_id: ""', "scope_id: \"\"").replace('scope_name: "Global"', f'scope_name: "{g["name"]}"'),
                 encoding="utf-8")
    r = ob.sync()
    assert r["pulled"] == 1
    got = st.get_memory(m["id"])
    assert (got["content"], got["kind"], got["pinned"], got["scope"], got["scope_id"]) == ("新内容,在 Obsidian 里改的", "decision", True, "group", g["id"])
    assert ob.sync()["pulled"] == 0


def test_edit_in_app_is_written_back(env):
    st, ob, vault = env
    m = st.add_memory("原文", "global", "", "fact")
    ob.sync()
    st.update_memory(m["id"], {"content": "程序里改过的", "pinned": True})
    r = ob.sync()
    assert r["written"] == 1
    ours, _, body = parse_note(md(vault)[0].read_text(encoding="utf-8"))
    assert body == "程序里改过的" and ours["pinned"] is True


def test_new_note_is_imported_and_gets_frontmatter_keeping_other_keys(env):
    st, ob, vault = env
    (vault / "我的想法.md").write_text("---\ntags:\n  - 想法\naliases: [x]\n---\n以后所有报告都用简体中文\n", encoding="utf-8")
    (vault / "空.md").write_text("   \n", encoding="utf-8")
    r = ob.sync()
    assert r["imported"] == 1
    mems = [m for m in st.list_memories() if m["source"] == "obsidian"]
    assert len(mems) == 1 and mems[0]["content"] == "以后所有报告都用简体中文" and mems[0]["scope"] == "global"
    ours, other, body = parse_note((vault / "我的想法.md").read_text(encoding="utf-8"))
    assert ours["ta_id"] == mems[0]["id"] and "tags:" in "\n".join(other) and "  - 想法" in other and body == "以后所有报告都用简体中文"
    assert ob.sync()["imported"] == 0                    # 不会重复导入


def test_duplicate_of_existing_memory_is_not_double_imported(env):
    st, ob, vault = env
    st.add_memory("同一句话", "global", "", "fact")
    ob.sync()
    (vault / "另一个.md").write_text("同一句话", encoding="utf-8")
    r = ob.sync()
    assert r["imported"] == 0 and any("same content" in w for w in r["warnings"])
    assert len(st.list_memories()) == 1


def test_rename_and_move_in_obsidian_keeps_identity(env):
    st, ob, vault = env
    m = st.add_memory("会被移动的记忆", "global", "", "fact")
    ob.sync()
    f = md(vault)[0]
    (vault / "整理").mkdir()
    moved = vault / "整理" / "改了名字.md"
    f.rename(moved)
    r = ob.sync()
    assert r["deleted_memories"] == 0 and r["imported"] == 0 and st.get_memory(m["id"])
    assert st.obsidian_map()[m["id"]]["rel_path"] == "整理/改了名字.md"


def test_delete_in_app_moves_file_to_trash_folder(env):
    st, ob, vault = env
    m = st.add_memory("要删掉的", "global", "", "fact")
    ob.sync()
    st.delete_memory(m["id"])
    r = ob.sync()
    assert r["removed_files"] == 1 and md(vault) == []
    assert len(list((vault / "_deleted").glob("*.md"))) == 1          # 没有真删,可以找回


def test_delete_in_obsidian_deletes_memory_but_mass_delete_is_refused(env):
    st, ob, vault = env
    ids = [st.add_memory(f"记忆 {i}", "global", "", "fact")["id"] for i in range(10)]
    ob.sync()
    md(vault)[0].unlink()
    r = ob.sync()
    assert r["deleted_memories"] == 1 and len(st.list_memories()) == 9
    for f in md(vault)[:7]:                                         # 一次少了一大半:多半是文件夹被移走
        f.unlink()
    r = ob.sync()
    assert r["deleted_memories"] == 0 and len(st.list_memories()) == 9
    # `mass_missing` is the flag the UI branches on; the warning text is localized.
    assert r["mass_missing"] is True and any("Nothing was deleted" in w for w in r["warnings"])
    r = ob.sync(force=True)
    assert r["deleted_memories"] == 7 and len(st.list_memories()) == 2
    assert len(ids) == 10


def test_missing_folder_changes_nothing(env):
    st, ob, vault = env
    st.add_memory("重要", "global", "", "fact")
    ob.sync()
    import shutil

    shutil.rmtree(vault)                                             # 文件夹整个没了(比如外接盘没挂载)
    r = ob.sync()
    assert not r["ok"] and "does not exist" in r["error"] and len(st.list_memories()) == 1


def test_unreadable_file_does_not_delete_its_memory(env):
    st, ob, vault = env
    m = st.add_memory("有一个文件后来坏了", "global", "", "fact")
    ob.sync()
    md(vault)[0].write_bytes(b"\xff\xfe\x00bad")
    r = ob.sync()
    assert st.get_memory(m["id"]) and r["deleted_memories"] == 0 and any("Could not read it" in w for w in r["warnings"])


def test_conflict_newer_side_wins_and_loser_is_backed_up(env):
    st, ob, vault = env
    m = st.add_memory("起点", "global", "", "fact")
    ob.sync()
    f = md(vault)[0]
    f.write_text(f.read_text(encoding="utf-8").replace("起点", "Obsidian 版本"), encoding="utf-8")
    st.update_memory(m["id"], {"content": "程序版本"})
    os.utime(f, (time.time() + 100, time.time() + 100))              # 文件更新
    r = ob.sync()
    assert r["conflicts"] == 1 and st.get_memory(m["id"])["content"] == "Obsidian 版本"
    backup = list((vault / "_conflict-backup").glob("*.md"))
    assert len(backup) == 1 and "程序版本" in backup[0].read_text(encoding="utf-8")
    # 反过来:程序更新
    st.update_memory(m["id"], {"content": "程序又改了"})
    f = md(vault)[0]
    f.write_text(f.read_text(encoding="utf-8").replace("Obsidian 版本", "Obsidian 又改了"), encoding="utf-8")
    os.utime(f, (time.time() - 1000, time.time() - 1000))            # 文件较旧
    r = ob.sync()
    assert r["conflicts"] == 1 and st.get_memory(m["id"])["content"] == "程序又改了"
    assert "程序又改了" in md(vault)[0].read_text(encoding="utf-8")


def test_unknown_scope_falls_back_to_global_with_warning(env):
    st, ob, vault = env
    (vault / "x.md").write_text('---\nscope: group\nscope_name: "不存在的群"\n---\n某条记忆', encoding="utf-8")
    r = ob.sync()
    assert r["imported"] == 1 and any("was not found" in w for w in r["warnings"])
    assert st.list_memories()[0]["scope"] == "global"


def test_symlinks_hidden_and_underscore_dirs_are_ignored(env, tmp_path):
    st, ob, vault = env
    outside = tmp_path / "secret.md"
    outside.write_text("外面的文件,不该被读", encoding="utf-8")
    (vault / "link.md").symlink_to(outside)
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "a.md").write_text("配置里的笔记", encoding="utf-8")
    (vault / "_忽略").mkdir()
    (vault / "_忽略" / "b.md").write_text("下划线目录", encoding="utf-8")
    r = ob.sync()
    assert r["imported"] == 0 and st.list_memories() == []


def test_validate_dir_rules(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for bad in ("relative/path", "/", str(Path.home()), str(data), str(data / "sub"), str(tmp_path)):
        with pytest.raises(ObsidianError):
            ObsidianSync.validate_dir(bad, data)
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ObsidianError):
        ObsidianSync.validate_dir(str(f), data)
    assert ObsidianSync.validate_dir(str(tmp_path / "vault" / "记忆"), data).name == "记忆"


def test_api_flow(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    c = TestClient(app, base_url="http://127.0.0.1")
    vault = tmp_path / "v" / "记忆"
    st = c.get("/api/obsidian").json()
    assert st["dir"] == "" and st["exists"] is False and st["auto"] is False
    assert c.post("/api/obsidian/sync").json()["ok"] is False
    assert c.put("/api/obsidian", json={"dir": "not/absolute"}).status_code == 400
    assert c.put("/api/settings", json={"obsidian_dir": "/tmp/evil"}).json()["obsidian_dir"] == ""   # 只能走 /api/obsidian
    vault.mkdir(parents=True)
    st = c.put("/api/obsidian", json={"dir": str(vault), "auto": True}).json()
    assert st["dir"] == str(vault.resolve()) and st["exists"] and st["auto"] is True
    c.post("/api/memories", json={"content": "接口里加的记忆", "kind": "preference"})
    r = c.post("/api/obsidian/sync").json()
    assert r["ok"] and r["written"] == 1
    assert c.get("/api/obsidian").json()["last"]["written"] == 1 and c.get("/api/obsidian").json()["mapped"] == 1
    other = tmp_path / "v" / "另一个"
    other.mkdir()
    c.put("/api/obsidian", json={"dir": str(other)})
    assert c.get("/api/obsidian").json()["mapped"] == 0 and c.get("/api/obsidian").json()["last"] is None
