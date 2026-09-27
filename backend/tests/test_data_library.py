"""Library (folder / link sources), backup and restore, chat log export."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.library import Library, LibraryError
from app import library as library_mod
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


# ------------------------------------------- a group's own material stays in the group
def test_a_new_group_has_no_knowledge_base_of_its_own(store):
    """A group used to be handed one the moment it existed, so that "the group's own material" had
    somewhere to live from the start. That is how this app's own database reached sixty-four bases,
    sixty of them empty, for ten projects. A base appears only when somebody puts a document in it
    — see `Library.workspace_kb`."""
    g = store.create_group("视频制作", None, [])

    assert [k for k in store.list_kbs() if k["group_id"] == g["id"]] == []


def test_nothing_of_a_groups_own_enters_the_library_by_itself(store):
    """The reversal, as a test. What had been read out of this group's attachments, and the documents
    sitting in its workspace, used to be copied into a knowledge base before every turn. They are not
    any more: a library holds what somebody filed in it. Nothing is lost — the material is still in
    the group, which is where a member reads it."""
    from app.library import Library

    lib = Library(store)
    g = store.create_group("视频制作", None, [])
    workspace = store.workspace_dir(g["id"])
    (workspace / "草稿").mkdir(parents=True, exist_ok=True)
    (workspace / "草稿" / "脚本.md").write_text("颅内动脉瘤介入治疗:弹簧圈填塞瘤腔。", encoding="utf-8")
    store.add_attachment(g["id"], "att-1", "纪要.txt", "text/plain", 20,
                         kind="document", text="会上确认:无血喷、无碎片。")

    assert store.list_docs() == []
    assert lib.scope_docs({"mode": "all"}, g["id"]) == []
    assert lib.search("弹簧圈填塞瘤腔", 3, lib.scope_ids({"mode": "all"}, g["id"])) == []
    # Still readable, one directory over: the file is where the group put it, and the attachment kept
    # the text that was read out of it.
    assert (workspace / "草稿" / "脚本.md").read_text(encoding="utf-8").startswith("颅内动脉瘤")
    assert store.list_attachments(g["id"])[0]["text"].startswith("会上确认")


def test_uploading_into_a_groups_library_creates_its_base_on_first_use(store):
    """The half that was kept, and why the decision was about *automatic* entry rather than about the
    feature: filing something in a group's own library still gives that group a base, made the moment
    it is needed, owned by the group, and filed under "project material" without being asked."""
    from app.library import Library

    lib = Library(store)
    g = store.create_group("视频制作", None, [])
    other = store.create_group("别的群", None, [])

    kb = lib.workspace_kb(g["id"])
    assert kb is not None and kb["group_id"] == g["id"]
    # The heading gets a default from ownership; the origin does not, because it is read off the
    # documents at list time and a stored guess would freeze at whatever the base held that day.
    assert kb["purpose"] == "project" and kb["source"] == ""
    lib.add_file("标准.md", "住宿标准:每人每晚不超过 500 元。".encode(), kb_id=kb["id"])

    assert [d["title"] for d in store.list_docs(kb["id"])] == ["标准"]
    # …and it is this group's: another group's search does not see it
    assert [d for d in lib.scope_docs({"mode": "all"}, other["id"]) if d["title"] == "标准"] == []


def test_re_importing_a_folder_replaces_rather_than_stacks(store, tmp_path):
    """The promise that used to hang off the sync, now hanging off the import that still exists: a
    document whose file changed must not keep its earlier passages (a search would still find what it
    used to say), an unchanged file must not be rewritten, and a changed one keeps its id — so a
    document already selected by a group is not lost by an update."""
    from app.library import Library

    lib = Library(store)
    kb = lib.shared_kb()
    folder = tmp_path / "标准"
    folder.mkdir()
    note = folder / "标准.md"
    note.write_text("住宿标准:每人每晚不超过 500 元。", encoding="utf-8")

    first = lib.add_dir(str(folder), kb_id=kb["id"])
    again = lib.add_dir(str(folder), kb_id=kb["id"])
    note.write_text("住宿标准:每人每晚不超过 800 元,按城市分档。", encoding="utf-8")
    third = lib.add_dir(str(folder), kb_id=kb["id"])

    assert (len(first["added"]), len(first["skipped"])) == (1, 0)
    assert (len(again["added"]), len(again["skipped"])) == (0, 1)
    assert (len(third["added"]), len(third["skipped"])) == (1, 0)
    assert third["added"][0]["id"] == first["added"][0]["id"]
    doc = store.list_docs(kb["id"])[0]
    assert len(store.doc_chunks(doc["id"])) == 1                      # the old version is gone, not stacked
    assert store._q("select count(*) n from library_chunks")[0]["n"] == 1
    assert lib.search("800 元", 3, lib.scope_ids({"mode": "all"}, ""))
    # The passage that answers with the old figure is gone: this is what "the library was updated"
    # has to mean for the next search.
    assert all("500" not in h["text"] for h in lib.search("500 元 住宿标准", 5, lib.scope_ids({"mode": "all"}, "")))


async def test_a_turn_does_not_file_the_groups_own_files(store, make_router):
    """*When* the old sync ran was the point of it — before anybody read, so that a script written a
    minute ago was searchable in this very turn. It does not run at all now, and a turn has to be
    shown to leave the library exactly as it found it."""
    from tests.conftest import FakeLLM
    from tests.test_collab import Collector, setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    workspace = store.workspace_dir(g["id"])
    (workspace / "脚本.md").write_text("第 5 段:弹簧圈填塞瘤腔,不出现血喷。", encoding="utf-8")

    await orch.handle_user_message(g["id"], "我们按脚本继续", Collector())

    assert "脚本" not in {d["title"] for d in store.list_docs()}
    assert (workspace / "脚本.md").is_file()


def test_a_retired_setting_is_dropped_rather_than_kept(store):
    """`library.watch_workspace` went with the copying it controlled. A stored switch that controls
    nothing is worse than no switch, because it reads like it still does something — so the
    normalizer drops it and an older group cannot carry one forward."""
    g = store.create_group("群", None, [])
    store.update_group(g["id"], {"ext": {"library": {"mode": "all", "kb_ids": [], "collection_ids": [],
                                                     "watch_workspace": False}}})

    assert "watch_workspace" not in (store.get_group(g["id"])["ext"]["library"] or {})


def test_a_bases_source_is_read_off_its_documents_not_guessed(store, tmp_path):
    """Where material came from is a fact about each row (`library.origin_of`): a folder the user
    imported is `import`, a bare name came in through a form, a URL is a link, nothing at all was
    typed here. A base whose documents disagree is `mixed`, and the breakdown is what makes that word
    mean something."""
    from app.library import Library, shelf_of

    lib = Library(store)
    kb = lib.shared_kb()
    folder = tmp_path / "我的资料"
    folder.mkdir()
    (folder / "a.md").write_text("本机导入的一篇。", encoding="utf-8")
    lib.add_dir(str(folder), kb_id=kb["id"])
    lib.add_file("上传的一篇.md", "上传的一篇。".encode(), kb_id=kb["id"])
    lib.add_text("网页", "从网页抄下来的。", "https://example.invalid/a", "link", kb_id=kb["id"])
    lib.add_text("手工", "手写的一段。", "", "note", kb_id=kb["id"])

    # Sorted, because `list_docs` answers newest-first (which is what a document list wants).
    rows = sorted((d["title"], d["origin"]) for d in store.list_docs(kb["id"]))
    assert rows == [("a", "import"), ("上传的一篇", "upload"), ("手工", "written"), ("网页", "link")]
    assert shelf_of([o for _t, o in rows]) == "mixed"
    assert shelf_of(["workspace", "workspace"]) == "workspace"     # one way in is one word
    assert shelf_of([]) == ""                                      # nothing to say yet

def test_the_button_looks_at_the_pictures_nobody_has_looked_at(tmp_path):
    """Pictures with no description yet cost a vision call each, so it happens when the user presses
    for it — and what comes back is cached on the row and paid for once. (It used to be indexed into
    the group's own knowledge base as well; nothing enters a library by itself any more, so what this
    delivers is a picture the group can actually be shown.)"""
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
    # picture per call, caching it on the row so the next member shown it reads the same description
    # instead of paying for another look.
    store.update_settings({"vision_cloud": True})
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.update_model("deepseek/deepseek-flash", {"strengths": ["multimodal"]})
    first = client.post(f"/api/groups/{g['id']}/library/describe", params={"limit": 2}).json()
    assert (first["described"], first["pending"]) == (2, 1), first
    # `documents` is how many this group has *filed*, and it has filed none: what a description
    # produces is a readable attachment, not a library entry.
    assert first["documents"] == 0
    described = [a for a in ids if store.get_attachment(a)["vision_text"]]
    assert len(described) == 2
    # Pressing again finishes the rest, and re-pressing costs nothing.
    again = client.post(f"/api/groups/{g['id']}/library/describe").json()
    assert (again["described"], again["pending"]) == (1, 0), again
    assert all(store.get_attachment(a)["vision_text"] for a in ids)
    assert client.post(f"/api/groups/{g['id']}/library/describe").json()["described"] == 0


def test_the_backfill_labels_what_was_never_labelled_and_never_overwrites_an_edit(store):
    """What a start does to a database that predates the labels.

    Only empty values are filled, so this is safe at every start — and the "never overwrites" half is
    the load-bearing one: a purpose is the user's own decision, and a migration that re-derived it
    would silently undo it on the next launch.
    """
    lib = Library(store)
    kb = lib.shared_kb()
    lib.add_text("导入的一篇", "内容。", "/Users/example/vault/a.md", "md", kb_id=kb["id"])
    lib.add_text("程序抓的一篇", "内容。", str(store.data_dir / "ingest" / "b.md"), "note", kb_id=kb["id"])
    g = store.create_group("群", None, [])
    own = lib.workspace_kb(g["id"])

    # As an older database would look: the columns exist and are empty.
    store._x("UPDATE library_docs SET origin=''")
    store._x("UPDATE knowledge_bases SET source='', purpose=''")
    store._label_library_material()

    origins = {d["title"]: d["origin"] for d in store.list_docs(kb["id"])}
    assert origins == {"导入的一篇": "import", "程序抓的一篇": "capture"}
    assert store.get_kb(own["id"])["purpose"] == "project"
    # It fills the heading and nothing else. A stored `source` would go stale the moment a document
    # was added, so the word is derived when the list is read — see the API test below.
    assert store.get_kb(kb["id"])["source"] == ""

    store.update_kb(kb["id"], {"purpose": "我的归类"})
    store._label_library_material()
    assert store.get_kb(kb["id"])["purpose"] == "我的归类"      # a decision is never re-derived
    assert store.get_kb(own["id"])["purpose"] == "project"


def test_the_knowledge_base_list_carries_the_labels_and_the_evidence_behind_them(tmp_path):
    """The page groups by purpose and shows the origin with its breakdown, so both have to arrive on
    the list — and the vocabulary has to arrive from the backend, because the closed origin list has
    exactly one definition (`library.ORIGINS`) and a second copy in the page would drift."""
    client, app = make(tmp_path, "lib-labels")
    store = app.state.store
    lib = Library(store)
    kb = lib.shared_kb()
    lib.add_text("导入的一篇", "内容。", "/Users/example/vault/a.md", "md", kb_id=kb["id"])
    lib.add_text("程序抓的一篇", "内容。", str(store.data_dir / "ingest" / "b.md"), "note", kb_id=kb["id"])
    g = store.create_group("视频制作", None, [])
    lib.workspace_kb(g["id"])

    voc = client.get("/api/library/vocabulary").json()
    assert "import" in voc["origins"] and "capture" in voc["origins"] and voc["mixed"] == "mixed"
    assert "project" in voc["purposes"]

    def rows() -> dict:
        return {r["name"]: r for r in client.get("/api/knowledge-bases").json()}

    shared = rows()["Shared knowledge base"]
    # Two ways in is `mixed`, and the breakdown is what makes that word mean something.
    assert shared["source"] == "mixed" and shared["purpose"] == ""
    assert shared["origins"] == {"import": 1, "capture": 1}
    # A base with nothing in it has no origin to report, and says so instead of guessing one from the
    # fact that it belongs to a project.
    assert rows()["视频制作"]["purpose"] == "project" and rows()["视频制作"]["source"] == ""

    # A heading is the user's to set, and setting one is a plain patch.
    client.patch(f"/api/knowledge-bases/{shared['id']}", json={"purpose": "医学与创意"})
    assert rows()["Shared knowledge base"]["purpose"] == "医学与创意"

    # The origin is derived until somebody disagrees with it, and "automatic" puts it back: an empty
    # patch value is a real answer, which is why it is sent rather than omitted.
    client.patch(f"/api/knowledge-bases/{shared['id']}", json={"source": "import"})
    assert rows()["Shared knowledge base"]["source"] == "import"
    assert rows()["Shared knowledge base"]["source_override"] == "import"
    client.patch(f"/api/knowledge-bases/{shared['id']}", json={"source": ""})
    assert rows()["Shared knowledge base"]["source"] == "mixed"
    assert rows()["Shared knowledge base"]["source_override"] == ""

    # …and the derivation follows the documents, which a stored word never could.
    lib.add_text("又导入一篇", "内容。", "/Users/example/vault/c.md", "md", kb_id=kb["id"])
    assert rows()["Shared knowledge base"]["origins"] == {"import": 2, "capture": 1}


def test_the_vocabulary_lists_come_from_the_one_definition():
    """Two copies of this list would drift, and the day they drift is the day a filter silently stops
    matching. This is the cheap guard: the served list *is* the one the rows are labelled with."""
    from app import library

    assert library.ORIGINS[0] == "import" and "written" in library.ORIGINS
    assert library.MIXED not in library.ORIGINS
    assert "project" in library.PURPOSES


# ------------------------------------------------- what fetched material is *for*
def test_a_fetched_skills_category_is_read_off_its_own_words(store, tmp_path):
    """A hundred-odd skills fetched off one platform are one grey pile. Each one's name says what it
    is for — that is the whole signal, and it was not a field.

    The five scenes this was built from are pinned here, because the *order* of the rules is the
    specification: 电影级长镜头运镜 must not be swallowed by the "电影感/电影级" bucket, and
    微表情导演 must be 表演 rather than 导演.
    """
    from app.library import CATEGORIES, category_of

    assert set(CATEGORIES) >= {"camera", "storyboard", "performance", "director", "commercial"}
    for title, want in (
        ("电影级长镜头", "camera"),
        ("创作分镜", "storyboard"),
        ("AI演员微表情导演", "performance"),
        ("名导十五秒视频风格资产引擎", "director"),
        ("一图成片-电影广告全能导演", "commercial"),
    ):
        assert category_of(title) == want, title

    # The name settles it; a phrase further down the instructions never overrules it.
    assert category_of("惊悚短片运镜", "本技能输出 15 秒分镜表") == "camera"
    # The platform's own tags count too, and both spellings are understood.
    assert category_of("某技能", tags=("commercial_ads",)) == "commercial"
    assert category_of("某技能", tags=("film_short",)) == "story"
    # …and nothing at all is a real answer, not an "other" bucket. A trial protocol titled 试验设计
    # must not be filed under graphic design.
    assert category_of("动脉瘤性蛛网膜下腔出血处理指南") == ""
    assert category_of("随机对照试验设计") == "design"      # the trap this gating exists for
    assert category_of("", "") == ""


def test_only_fetched_material_gets_a_category(store):
    """`add_text` classifies what the app fetched for itself, and nothing else — the user's own
    imports are not guessed at (see `Store._classify_library_material`)."""
    lib = Library(store)
    kb = lib.shared_kb()
    fetched = lib.add_text("电影级长镜头运镜", "内容。", str(store.data_dir / "jimeng-skills" / "skill-1.md"),
                           "note", kb_id=kb["id"])
    mine = lib.add_text("我们的规范", "内容。", "/Users/example/vault/spec.md", "md", kb_id=kb["id"])
    note = lib.add_text("手写的笔记", "内容。", kb_id=kb["id"])
    explicit = lib.add_text("随便一个名字", "内容。", str(store.data_dir / "ingest" / "x.md"), "note",
                            kb_id=kb["id"], category="trailer")

    assert fetched["category"] == "camera" and fetched["origin"] == "capture"
    assert mine["category"] == "" and mine["origin"] == "import"
    assert note["category"] == ""                       # typed here: nothing to read
    assert explicit["category"] == "trailer"            # an ingester that knows better wins


def test_the_backfill_classifies_what_is_already_there_without_touching_anything_else(store):
    """The database that already holds the fetched skills: a start classifies them from what was
    written down (title, the platform's tag line, the beginning of the body), and leaves every other
    document alone."""
    lib = Library(store)
    kb = lib.shared_kb()
    body = "# 法宝特效运镜技能\n\n───\ntags: []\n───\n\n**即梦上是这么介绍它的**：把法宝光效与运镜结合。\n\n正文。"
    old = lib.add_text("法宝特效运镜技能", body, str(store.data_dir / "jimeng-skills" / "skill-9.md"),
                       "note", kb_id=kb["id"])
    lib.add_text("一篇医学指南", "内容。", "/Users/example/vault/guide.md", "md", kb_id=kb["id"])

    # As an older database would look: the column exists and is empty.
    store._x("UPDATE library_docs SET category=''")
    store._classify_library_material()

    got = {d["title"]: d["category"] for d in store.list_docs(kb["id"])}
    assert got["法宝特效运镜技能"] == "camera"
    assert got["一篇医学指南"] == ""
    assert store.get_doc(old["id"])["category"] == "camera"

    # Idempotent, and it never re-derives what is already there (a re-run must not rewrite labels).
    store._x("UPDATE library_docs SET category='我的写法' WHERE id=?", (old["id"],))
    store._classify_library_material()
    assert store.get_doc(old["id"])["category"] == "我的写法"


def test_the_library_list_carries_the_category_facet_and_can_group_by_it(tmp_path):
    """The page's third axis: what the material is for, with counts, as a filter and as a
    classification view."""
    client, app = make(tmp_path, "lib-category")
    store = app.state.store
    lib = Library(store)
    kb = lib.shared_kb()
    for name in ("电影级长镜头运镜", "FPV 穿越运镜", "创作分镜", "AI演员微表情导演", "一篇医学指南"):
        lib.add_text(name, f"{name} 的内容。", str(store.data_dir / "jimeng-skills" / f"{name}.md"),
                     "note", kb_id=kb["id"])

    listed = client.get("/api/library", params={"limit": 50}).json()
    assert listed["categories"]["camera"] == 2 and listed["categories"]["storyboard"] == 1
    assert listed["categories"][""] == 1                 # the guideline: nothing in its words says
    assert sorted(listed["categories"]) == ["", "camera", "performance", "storyboard"]

    only = client.get("/api/library", params={"category": "camera"}).json()
    assert only["total"] == 2 and all(d["category"] == "camera" for d in only["docs"])

    grouped = client.get("/api/library", params={"group_by": "category", "per_group": 1}).json()
    assert grouped["groups"][0]["id"] == "camera" and grouped["groups"][0]["count"] == 2
    assert len(grouped["groups"][0]["docs"]) == 1
    assert client.get("/api/library/vocabulary").json()["categories"] == list(library_mod.CATEGORIES)
