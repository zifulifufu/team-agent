"""Library (folder / link sources), backup and restore, chat log export."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.library import Library, LibraryError
from app.main import create_app
from tests.conftest import FakeLLM


def make(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="好"))
    return TestClient(app, base_url="http://127.0.0.1"), app


# --------------------------------------------------------------------- folders
def test_add_dir_filters_dedupes_and_replaces(store, tmp_path):
    lib = Library(store)
    d = tmp_path / "docs"
    (d / "sub").mkdir(parents=True)
    (d / ".hidden").mkdir()
    (d / "a.md").write_text("发布会时间是 10 月 18 日", encoding="utf-8")
    (d / "sub" / "b.txt").write_text("出差住宿标准每晚 600 元", encoding="utf-8")
    (d / "c.png").write_bytes(b"\x89PNG")
    (d / ".hidden" / "x.md").write_text("不该导入", encoding="utf-8")
    (d / "link.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("外面的", encoding="utf-8")
    kb = lib.shared_kb()["id"]
    r = lib.add_dir(str(d), True, kb)
    assert sorted(x["title"] for x in r["added"]) == ["a", "b"] and r["skipped"] == []
    assert lib.search("住宿标准")[0]["title"] == "b"
    r = lib.add_dir(str(d), True, kb)                                  # importing again skips what did not change
    assert r["added"] == [] and {s["reason"] for s in r["skipped"]} == {"Already up to date"}
    (d / "a.md").write_text("发布会改到 11 月 2 日,地点不变", encoding="utf-8")
    r = lib.add_dir(str(d), True, kb)
    assert [x["title"] for x in r["added"]] == ["a"] and len(store.list_docs()) == 2      # replaced, not duplicated
    assert "11 月" in lib.search("发布会")[0]["text"]
    with pytest.raises(LibraryError):
        lib.add_dir("relative/dir", True, kb)
    with pytest.raises(LibraryError):
        lib.add_dir(str(d / "a.md"), True, kb)


# ------------------------------------------------------------------------ links
def patch_http(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr("app.library.httpx.Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_add_url_html_text_pdf_errors_and_limits(store, monkeypatch):
    lib = Library(store)

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/page":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  content="<html><head><title>产品手册</title><script>bad()</script></head><body><h1>概述</h1><p>本产品支持按强项分工。</p></body></html>".encode())
        if p == "/notes.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content="纯文本内容".encode())
        if p == "/img":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
        if p == "/big":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * (5 * 1024 * 1024 + 10))
        return httpx.Response(404)

    patch_http(monkeypatch, handler)
    kb = lib.shared_kb()["id"]
    d = lib.add_url("https://x.example/page", kb)
    assert d["title"] == "产品手册" and d["kind"] == "link" and d["filename"] == "https://x.example/page"
    assert "bad()" not in lib.read(d["id"])["text"] and "按强项分工" in lib.search("强项分工")[0]["text"]
    assert lib.add_url("https://x.example/notes.txt", kb)["title"] == "notes.txt"
    for url, msg in (("ftp://x/y", "http"), ("https://x.example/img", "not supported"),
                     ("https://x.example/big", "too large"), ("https://x.example/none", "404")):
        with pytest.raises(LibraryError, match=msg):
            lib.add_url(url, kb)


def test_url_api_respects_external_switch(tmp_path, monkeypatch):
    c, app = make(tmp_path)
    patch_http(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello"))
    assert c.post("/api/library/url", json={"url": "https://x.example/a.txt"}).status_code == 200
    c.put("/api/settings", json={"external_calls_enabled": False})
    assert c.post("/api/library/url", json={"url": "https://x.example/b.txt"}).status_code == 403
    assert c.post("/api/library/dir", json={"path": "nope"}).status_code == 400


# ------------------------------------------------------------- backup / restore
def test_restore_replaces_data_keeps_local_keys_and_saves_safety_copy(tmp_path):
    a, app_a = make(tmp_path, "a")
    a.post("/api/library/note", json={"title": "A 库的文档", "content": "只有 A 有的资料:独角兽"})
    a.post("/api/memories", json={"content": "A 的记忆", "kind": "fact"})
    g = a.post("/api/groups", json={"name": "A 的群"}).json()
    backup = a.get("/api/data/export").content                          # keys excluded by default

    b, app_b = make(tmp_path, "b")
    app_b.state.store.update_provider("deepseek", {"api_key": "sk-b-local-key-123"})
    b.post("/api/library/note", json={"title": "B 库的文档", "content": "只有 B 有的资料:企鹅"})
    assert b.get("/api/library/search", params={"q": "企鹅"}).json()
    res = b.post("/api/data/restore", content=backup, headers={"Content-Type": "application/octet-stream"}).json()
    assert res["groups"] >= 2 and res["docs"] == 1 and res["memories"] >= 1
    assert any(x["name"] == "A 的群" for x in b.get("/api/groups").json()) and g["id"]
    assert b.get("/api/library/search", params={"q": "独角兽"}).json()      # search index refreshed
    assert b.get("/api/library/search", params={"q": "企鹅"}).json() == []
    assert app_b.state.store.get_provider("deepseek")["api_key"] == "sk-b-local-key-123"   # no keys in the backup: local ones are kept
    import sqlite3

    safety = list((tmp_path / "b" / "backups").glob("pre-restore-*.db"))
    assert len(safety) == 1
    con = sqlite3.connect(safety[0])
    assert con.execute("SELECT title FROM library_docs").fetchall() == [("B 库的文档",)]    # a copy of the pre-restore data is kept
    con.close()
    assert b.post("/api/data/restore", content=b"not a database", headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert b.post("/api/data/restore", content=b"", headers={"Content-Type": "application/octet-stream"}).status_code == 400
    empty = sqlite3.connect(tmp_path / "other.db")
    empty.execute("CREATE TABLE x(a)")
    empty.commit()
    empty.close()
    assert b.post("/api/data/restore", content=(tmp_path / "other.db").read_bytes(), headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert b.get("/api/health").json()["ok"]                            # a failed restore leaves existing data intact


# --------------------------------------------------------------- chat log export
def test_export_chat_markdown_and_obsidian(tmp_path):
    c, app = make(tmp_path)
    store = app.state.store
    g = store.list_groups()[0]
    a = store.list_agents()[0]
    store.add_message(g["id"], "user", None, "我", "帮我写发布会开场")
    store.add_message(g["id"], "agent", a["id"], a["name"], "好的,这是开场白。", meta={"tools": [{"name": "library_search", "status": "ok"}]})
    r = c.get(f"/api/groups/{g['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown") and "filename*=UTF-8''" in r.headers["content-disposition"]
    text = r.text
    assert text.startswith(f"# {g['name']}") and "**我**" in text and "帮我写发布会开场" in text and "Tool call `library_search`: ok" in text
    assert c.get("/api/groups/nope/export").status_code == 404
    assert c.post(f"/api/groups/{g['id']}/export-obsidian").status_code == 400          # Obsidian is not configured yet
    vault = tmp_path / "v" / "记忆"
    vault.mkdir(parents=True)
    c.put("/api/obsidian", json={"dir": str(vault)})
    p = c.post(f"/api/groups/{g['id']}/export-obsidian").json()["path"]
    assert "_chat-log" in p and open(p, encoding="utf-8").read().startswith("# ")
    r = c.post("/api/obsidian/sync").json()
    assert r["imported"] == 0 and store.list_memories() == []                          # exported chats are not re-imported as memories


def test_export_every_task_list_as_one_table(tmp_path):
    """The boards also travel inside the markdown export, as part of the conversation. This is the
    same information as a table, for the question "what is still open" rather than "what happened":
    one row per task, across every board the group has run."""
    import csv
    import io

    c, app = make(tmp_path)
    store = app.state.store
    g = store.list_groups()[0]
    store.add_message(g["id"], "user", None, "我", "做个发布会方案")
    store.add_message(g["id"], "plan", None, "任务板", "分工", meta={
        "kind": "plan", "goal": "发布会方案", "status": "done",
        "tasks": [
            {"id": "t1", "owner": "文案", "title": "写开场白", "status": "done", "deliverable": "一段开场白"},
            {"id": "t2", "owner": "校对", "title": "检查错别字", "status": "failed", "error": "模型调用失败"},
        ]})

    r = c.get(f"/api/groups/{g['id']}/export-tasks")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "filename*=UTF-8''" in r.headers["content-disposition"] and "tasks" in r.headers["content-disposition"]
    body = r.content.decode("utf-8")
    assert body.startswith("\ufeff"), "without a BOM, Excel opens a UTF-8 CSV as mojibake"
    rows = list(csv.reader(io.StringIO(body.lstrip("\ufeff"))))
    assert rows[0][:6] == ["Board time", "Board", "Goal", "Task", "Owner", "What"]
    assert len(rows) == 3                                   # a header and one row per task
    assert rows[1][3:7] == ["t1", "文案", "写开场白", "Done"]
    assert rows[2][6] == "Failed" and rows[2][8] == "模型调用失败"

    # Task states are machine values in the database; the table says them in the reader's language.
    zh = c.get(f"/api/groups/{g['id']}/export-tasks?lang=zh").content.decode("utf-8")
    zh_rows = list(csv.reader(io.StringIO(zh.lstrip("\ufeff"))))
    assert zh_rows[0][0] == "任务板时间" and zh_rows[1][6] == "已完成"

    # The markdown export says them the same way, and shows why a task failed.
    md = c.get(f"/api/groups/{g['id']}/export").text
    assert "[Done] 文案:写开场白" in md and "[Failed] 校对:检查错别字 — 模型调用失败" in md
    assert c.get("/api/groups/nope/export-tasks").status_code == 404


# ------------------------------------------- a group's own material, kept where it is searched
def test_a_new_group_has_its_own_knowledge_base(store):
    """Every group is a workspace, so every group has its own knowledge base from the moment it
    exists — not from the first time somebody happens to add a document. Otherwise the relationship
    is invisible until it is used, and "the group's own material" has nowhere to be."""
    g = store.create_group("视频制作", None, [])

    kbs = [k for k in store.list_kbs() if k["group_id"] == g["id"]]

    assert len(kbs) == 1 and kbs[0]["name"] == "视频制作"
    assert store.list_docs(kbs[0]["id"]) == []


def test_the_groups_own_material_lands_in_its_own_knowledge_base(store, tmp_path):
    """The join that was missing: attachments whose content has already been read out, and the
    documents in the workspace, become documents of *that* group's knowledge base — so the next
    search, in the next turn, finds what the group has now."""
    from app.library import Library

    lib = Library(store)
    g = store.create_group("视频制作", None, [])
    other = store.create_group("别的群", None, [])
    workspace = tmp_path / "ws"
    (workspace / "草稿").mkdir(parents=True)
    (workspace / "草稿" / "脚本.md").write_text("颅内动脉瘤介入治疗:弹簧圈填塞瘤腔。", encoding="utf-8")
    (workspace / "分镜.txt").write_text("B05:载瘤动脉保持通畅。", encoding="utf-8")
    (workspace / "成片.mp4").write_bytes(b"\x00" * 32)              # not a document: not indexed
    (workspace / ".frames").mkdir()
    (workspace / ".frames" / "f1.jpg").write_bytes(b"\xff\xd8")     # internal: never walked
    store.add_attachment(g["id"], "att-1", "纪要.txt", "text/plain", 20,
                         kind="document", text="会上确认:无血喷、无碎片。")

    out = lib.sync_group_material(g["id"], workspace)

    assert out["added"] == 3, out          # the mp4 and the frames folder are not material at all
    assert {d["title"] for d in lib.scope_docs({"mode": "all"}, g["id"])} == {"分镜", "脚本", "纪要.txt"}
    # …and a member's search now finds them
    assert lib.search("弹簧圈填塞瘤腔", 3, lib.scope_ids({"mode": "all"}, g["id"]))
    assert lib.search("无血喷", 3, lib.scope_ids({"mode": "all"}, g["id"]))
    # Another group's search does not: a group's own material stays its own
    assert lib.search("弹簧圈填塞瘤腔", 3, lib.scope_ids({"mode": "all"}, other["id"])) == []


def test_the_material_is_replaced_when_it_changes_and_left_alone_when_it_does_not(store, tmp_path):
    """A document that changed must not keep its earlier passages (a search would still find what it
    used to say), and one that did not change must not be rewritten on every turn."""
    from app.library import Library

    lib = Library(store)
    g = store.create_group("群", None, [])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    note = workspace / "标准.md"
    note.write_text("住宿标准:每人每晚不超过 500 元。", encoding="utf-8")

    first = lib.sync_group_material(g["id"], workspace)
    again = lib.sync_group_material(g["id"], workspace)
    note.write_text("住宿标准:每人每晚不超过 800 元,按城市分档。", encoding="utf-8")
    third = lib.sync_group_material(g["id"], workspace)

    assert (first["added"], first["updated"]) == (1, 0)
    assert (again["added"], again["updated"]) == (0, 0) and again["skipped"] == 1
    assert (third["added"], third["updated"]) == (0, 1)
    doc = [d for d in store.list_docs() if d["title"] == "标准"][0]
    assert len(store.doc_chunks(doc["id"])) == 1                      # the old version is gone, not stacked
    assert store._q("select count(*) n from library_chunks")[0]["n"] == 1
    assert lib.search("800 元", 3, lib.scope_ids({"mode": "all"}, g["id"]))
    # The passage that answers with the old figure is gone: this is what "the knowledge base was
    # updated" has to mean for the next search.
    assert all("500" not in h["text"] for h in lib.search("500 元 住宿标准", 5, lib.scope_ids({"mode": "all"}, g["id"])))


async def test_a_search_that_finds_nothing_says_what_the_group_can_search(store, make_router, tmp_path):
    """A dead end is the most common outcome on the knowledge bases this app is pointed at (English
    atlases, papers, manuals) — BM25 matches words, and a question asked in Chinese shares none with
    an English document. Naming what is in there turns "nothing found" into "try the English name",
    which is the difference between using the knowledge base and answering from memory."""
    from app.library import Library
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    lib = Library(store)
    kb = lib.shared_kb()
    lib.add_file("Internal Carotid Artery Segments.md", "Coiling an aneurysm keeps it open.".encode(),
                 kb_id=kb["id"])
    orch, g = setup(store, make_router, FakeLLM(default="x"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)

    miss = await orch.toolhub.call(ctx, "library_search", {"query": "颅内动脉瘤怎么处理"}, None)

    assert miss.ok and "Internal Carotid Artery Segments" in miss.text
    assert "English" in miss.text and "library_read" in miss.text, miss.text   # and what to do about it
    # A hit is still reported as a hit, with its passage.
    hit = await orch.toolhub.call(ctx, "library_search", {"query": "coiling aneurysm"}, None)
    assert "Coiling an aneurysm keeps it open." in hit.text


async def test_the_turn_brings_the_groups_own_material_up_to_date_first(store, make_router, tmp_path):
    """The mechanism under test is *when* it runs: before anybody reads, so a script written a minute
    ago and a document just dropped in are both searchable in this very turn."""
    from app.library import Library
    from tests.conftest import FakeLLM
    from tests.test_collab import Collector, setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    workspace = store.workspace_dir(g["id"])
    (workspace / "脚本.md").write_text("第 5 段:弹簧圈填塞瘤腔,不出现血喷。", encoding="utf-8")

    await orch.handle_user_message(g["id"], "我们按脚本继续", Collector())

    lib = Library(store)
    assert lib.search("弹簧圈填塞瘤腔", 3, lib.scope_ids({"mode": "all"}, g["id"])), \
        "what the group wrote before this turn is searchable in it"


def test_two_groups_with_the_same_name_get_distinguishable_knowledge_bases(store):
    """Group names are the user's, and they repeat (the app's own list has three called the same
    thing). Two knowledge bases called the same thing cannot be told apart in the picker, so the
    later one is numbered — the same way member names already are."""
    a = store.create_group("视频制作", None, [])
    b = store.create_group("视频制作", None, [])

    names = {k["group_id"]: k["name"] for k in store.list_kbs() if k["group_id"] in (a["id"], b["id"])}

    assert names[a["id"]] == "视频制作" and names[b["id"]] == "视频制作 2"


def test_groups_that_existed_before_get_their_knowledge_base_on_the_next_start(store):
    """The backfill, so the relationship shows up for groups that were already there rather than
    waiting for somebody to talk in each one."""
    g = store.create_group("旧群", None, [])
    for kb in store.list_kbs(g["id"]):
        store.delete_kb(kb["id"])                 # pretend this group predates the rule

    assert store.ensure_group_kbs() == 1
    assert [k["name"] for k in store.list_kbs(g["id"]) if k["group_id"] == g["id"]] == ["旧群"]
    assert store.ensure_group_kbs() == 0           # nothing left to do on the next start


def test_the_switch_decides_whether_the_workspace_half_is_watched(store):
    """The default follows where the workspace is — a folder this app manages is watched, one the
    user picked is not — and an explicit choice wins over both."""
    from app.library import watch_workspace

    picked = store.create_group("自选目录", None, [], workspace=str(store.data_dir))
    managed = store.create_group("程序管理", None, [])
    assert watch_workspace(store.get_group(picked["id"])) is False
    assert watch_workspace(store.get_group(managed["id"])) is True

    store.update_group(picked["id"], {"ext": {"library": {"mode": "all", "kb_ids": [],
                                                         "collection_ids": [], "watch_workspace": True}}})
    store.update_group(managed["id"], {"ext": {"library": {"mode": "all", "kb_ids": [],
                                                          "collection_ids": [], "watch_workspace": False}}})
    assert watch_workspace(store.get_group(picked["id"])) is True
    assert watch_workspace(store.get_group(managed["id"])) is False


def test_the_button_looks_at_the_pictures_nobody_has_looked_at(tmp_path):
    """The on-demand half of "a group's own material": pictures with no description yet cost a
    vision call each, so it happens when the user presses for it — and what comes back is cached,
    indexed, and paid for once."""
    from app import attachments as attachments_lib, coderun

    client, app = make(tmp_path)
    store = app.state.store
    g = client.get("/api/groups").json()[0]
    workspace = coderun.workspace_dir(store.data_dir, store.get_settings(), g["id"])
    ids = []
    for i in range(3):
        aid = store.new_id()
        data = b"\x89PNG\r\n\x1a\n" + bytes(64 + i)
        rel = attachments_lib.save(workspace, aid, f"图{i}.png", ".png", data)
        store.add_attachment(g["id"], aid, f"图{i}.png", "image/png", len(data), kind="image", rel_path=rel)
        ids.append(aid)
    # Nobody can look yet: the panel's count is there, and pressing says why nothing happened.
    assert client.get(f"/api/groups/{g['id']}/capabilities").json()["pictures_pending"] == 3
    off = client.post(f"/api/groups/{g['id']}/library/describe", params={"limit": 2}).json()
    assert off["described"] == 0 and off["pending"] == 3 and off["reason"]

    # Turn cloud vision on with a model that carries `multimodal`, and the batch describes one
    # picture per call, caching it on the row and indexing it into this group's own knowledge base.
    store.update_settings({"vision_cloud": True})
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.update_model("deepseek/deepseek-flash", {"strengths": ["multimodal"]})
    first = client.post(f"/api/groups/{g['id']}/library/describe", params={"limit": 2}).json()
    assert (first["described"], first["pending"]) == (2, 1), first
    assert first["documents"] == 2
    described = [a for a in ids if store.get_attachment(a)["vision_text"]]
    assert len(described) == 2
    # Pressing again finishes the rest, and re-pressing costs nothing.
    again = client.post(f"/api/groups/{g['id']}/library/describe").json()
    assert (again["described"], again["pending"]) == (1, 0), again
    assert all(store.get_attachment(a)["vision_text"] for a in ids)
    assert client.post(f"/api/groups/{g['id']}/library/describe").json()["described"] == 0
