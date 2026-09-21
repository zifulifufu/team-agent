"""资料库(文件夹/链接来源)、备份恢复、聊天记录导出。"""

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


# ------------------------------------------------------------------ 文件夹
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
    r = lib.add_dir(str(d))
    assert sorted(x["title"] for x in r["added"]) == ["a", "b"] and r["skipped"] == []
    assert lib.search("住宿标准")[0]["title"] == "b"
    r = lib.add_dir(str(d))                                            # 再导入:没变的跳过
    assert r["added"] == [] and {s["reason"] for s in r["skipped"]} == {"已是最新"}
    (d / "a.md").write_text("发布会改到 11 月 2 日,地点不变", encoding="utf-8")
    r = lib.add_dir(str(d))
    assert [x["title"] for x in r["added"]] == ["a"] and len(store.list_docs()) == 2      # 替换,不重复
    assert "11 月" in lib.search("发布会")[0]["text"]
    with pytest.raises(LibraryError):
        lib.add_dir("relative/dir")
    with pytest.raises(LibraryError):
        lib.add_dir(str(d / "a.md"))


# ------------------------------------------------------------------ 链接
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
    d = lib.add_url("https://x.example/page")
    assert d["title"] == "产品手册" and d["kind"] == "link" and d["filename"] == "https://x.example/page"
    assert "bad()" not in lib.read(d["id"])["text"] and "按强项分工" in lib.search("强项分工")[0]["text"]
    assert lib.add_url("https://x.example/notes.txt")["title"] == "notes.txt"
    for url, msg in (("ftp://x/y", "http"), ("https://x.example/img", "不支持"), ("https://x.example/big", "太大"), ("https://x.example/none", "404")):
        with pytest.raises(LibraryError, match=msg):
            lib.add_url(url)


def test_url_api_respects_external_switch(tmp_path, monkeypatch):
    c, app = make(tmp_path)
    patch_http(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello"))
    assert c.post("/api/library/url", json={"url": "https://x.example/a.txt"}).status_code == 200
    c.put("/api/settings", json={"external_calls_enabled": False})
    assert c.post("/api/library/url", json={"url": "https://x.example/b.txt"}).status_code == 403
    assert c.post("/api/library/dir", json={"path": "nope"}).status_code == 400


# ------------------------------------------------------------------ 备份 / 恢复
def test_restore_replaces_data_keeps_local_keys_and_saves_safety_copy(tmp_path):
    a, app_a = make(tmp_path, "a")
    a.post("/api/library/note", json={"title": "A 库的文档", "content": "只有 A 有的资料:独角兽"})
    a.post("/api/memories", json={"content": "A 的记忆", "kind": "fact"})
    g = a.post("/api/groups", json={"name": "A 的群"}).json()
    backup = a.get("/api/data/export").content                          # 默认不含密钥

    b, app_b = make(tmp_path, "b")
    app_b.state.store.update_provider("deepseek", {"api_key": "sk-b-local-key-123"})
    b.post("/api/library/note", json={"title": "B 库的文档", "content": "只有 B 有的资料:企鹅"})
    assert b.get("/api/library/search", params={"q": "企鹅"}).json()
    res = b.post("/api/data/restore", content=backup, headers={"Content-Type": "application/octet-stream"}).json()
    assert res["groups"] >= 2 and res["docs"] == 1 and res["memories"] >= 1
    assert any(x["name"] == "A 的群" for x in b.get("/api/groups").json()) and g["id"]
    assert b.get("/api/library/search", params={"q": "独角兽"}).json()      # 检索索引已刷新
    assert b.get("/api/library/search", params={"q": "企鹅"}).json() == []
    assert app_b.state.store.get_provider("deepseek")["api_key"] == "sk-b-local-key-123"   # 备份里没有密钥:本机的保留
    import sqlite3

    safety = list((tmp_path / "b" / "backups").glob("pre-restore-*.db"))
    assert len(safety) == 1
    con = sqlite3.connect(safety[0])
    assert con.execute("SELECT title FROM library_docs").fetchall() == [("B 库的文档",)]    # 恢复前的数据留了一份
    con.close()
    assert b.post("/api/data/restore", content=b"not a database", headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert b.post("/api/data/restore", content=b"", headers={"Content-Type": "application/octet-stream"}).status_code == 400
    empty = sqlite3.connect(tmp_path / "other.db")
    empty.execute("CREATE TABLE x(a)")
    empty.commit()
    empty.close()
    assert b.post("/api/data/restore", content=(tmp_path / "other.db").read_bytes(), headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert b.get("/api/health").json()["ok"]                            # 失败的恢复不影响现有数据


# ------------------------------------------------------------------ 聊天记录导出
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
    assert c.post(f"/api/groups/{g['id']}/export-obsidian").status_code == 400          # 还没设置 Obsidian
    vault = tmp_path / "v" / "记忆"
    vault.mkdir(parents=True)
    c.put("/api/obsidian", json={"dir": str(vault)})
    p = c.post(f"/api/groups/{g['id']}/export-obsidian").json()["path"]
    assert "_聊天记录" in p and open(p, encoding="utf-8").read().startswith("# ")
    r = c.post("/api/obsidian/sync").json()
    assert r["imported"] == 0 and store.list_memories() == []                          # 导出的聊天不会被当成记忆导入
