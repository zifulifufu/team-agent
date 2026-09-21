"""SQLite 持久化(扩展部分):提示词库、记忆、资料库、模型清单缓存、更新记录。

作为 Store 的混入类使用(见 store.py),共用它的连接与锁。
"""

from __future__ import annotations

import json
import time
from typing import Any

SCHEMA_EXT = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'general',      -- general | group
    use_globally INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'global',      -- global | group | agent
    scope_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'fact',         -- preference | fact | decision | lesson | action
    content TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',     -- manual | auto
    hits INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, scope_id);
CREATE TABLE IF NOT EXISTS library_docs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'note',         -- note | txt | md | pdf | docx | csv | html ...
    size INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS library_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES library_docs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON library_chunks(doc_id, idx);
CREATE TABLE IF NOT EXISTS model_seen (provider_id TEXT PRIMARY KEY, ids TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS model_live (provider_id TEXT PRIMARY KEY, ids TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS obsidian_map (
    memory_id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,                    -- 相对于所选文件夹
    hash TEXT NOT NULL                         -- 上次同步完成时这条记忆的内容指纹,用来判断哪一边改过
);
CREATE TABLE IF NOT EXISTS model_health (
    model_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,                      -- ok | limited | bad
    detail TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'test',       -- test(手动检测) | chat(聊天时顺带记录) | probe(本地服务探测)
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                        -- app | skill | plugin | model | catalog
    ref TEXT NOT NULL DEFAULT '',              -- 去重用:同一 kind+ref 只保留一条未处理的提醒
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'new',        -- new | dismissed | done
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    kind TEXT NOT NULL,                        -- skill | plugin
    name TEXT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    sha TEXT NOT NULL DEFAULT '',              -- 安装时 GitHub 文件的 blob sha,用来判断是否有新版本
    installed_at REAL NOT NULL,
    PRIMARY KEY (kind, name)
);
"""

MEMORY_KINDS = ("preference", "fact", "decision", "lesson", "action")


class ExtStore:
    """依赖宿主类提供: _q / _one / _x / _lock / _db / new_id()"""

    # ------------------------------------------------------------------ prompts
    def list_prompts(self) -> list[dict]:
        rows = self._q("SELECT * FROM prompts ORDER BY created_at, rowid")  # type: ignore[attr-defined]
        for r in rows:
            r["use_globally"] = bool(r["use_globally"])
        return rows

    def get_prompt(self, pid: str) -> dict | None:
        r = self._one("SELECT * FROM prompts WHERE id=?", (pid,))  # type: ignore[attr-defined]
        if r:
            r["use_globally"] = bool(r["use_globally"])
        return r

    def add_prompt(self, title: str, content: str, kind: str = "general", use_globally: bool = False) -> dict:
        pid = self.new_id()  # type: ignore[attr-defined]
        kind = kind if kind in ("general", "group") else "general"
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO prompts(id,title,content,kind,use_globally,created_at) VALUES(?,?,?,?,?,?)",
            (pid, title, content, kind, int(use_globally), time.time()),
        )
        return self.get_prompt(pid)  # type: ignore[return-value]

    def update_prompt(self, pid: str, patch: dict) -> dict | None:
        for k in ("title", "content"):
            if patch.get(k) is not None:
                self._x(f"UPDATE prompts SET {k}=? WHERE id=?", (patch[k], pid))  # type: ignore[attr-defined]
        if patch.get("kind") in ("general", "group"):
            self._x("UPDATE prompts SET kind=? WHERE id=?", (patch["kind"], pid))  # type: ignore[attr-defined]
        if patch.get("use_globally") is not None:
            self._x("UPDATE prompts SET use_globally=? WHERE id=?", (int(bool(patch["use_globally"])), pid))  # type: ignore[attr-defined]
        return self.get_prompt(pid)

    def delete_prompt(self, pid: str) -> None:
        self._x("DELETE FROM prompts WHERE id=?", (pid,))  # type: ignore[attr-defined]

    # ----------------------------------------------------------------- memories
    def list_memories(self, scope: str | None = None, scope_id: str | None = None,
                      kind: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM memories WHERE 1=1", []
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if scope_id is not None:
            sql += " AND scope_id=?"
            args.append(scope_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        rows = self._q(sql, (*args, limit))  # type: ignore[attr-defined]
        for r in rows:
            r["pinned"] = bool(r["pinned"])
        return rows

    def memories_for(self, group_id: str, agent_id: str) -> list[dict]:
        """一次发言可用的记忆:全局 + 本群 + 本成员。"""
        rows = self._q(  # type: ignore[attr-defined]
            "SELECT * FROM memories WHERE scope='global' OR (scope='group' AND scope_id=?) "
            "OR (scope='agent' AND scope_id=?)",
            (group_id, agent_id),
        )
        for r in rows:
            r["pinned"] = bool(r["pinned"])
        return rows

    def get_memory(self, mid: str) -> dict | None:
        r = self._one("SELECT * FROM memories WHERE id=?", (mid,))  # type: ignore[attr-defined]
        if r:
            r["pinned"] = bool(r["pinned"])
        return r

    def add_memory(self, content: str, scope: str = "global", scope_id: str = "", kind: str = "fact",
                   source: str = "manual", pinned: bool = False) -> dict:
        content = content.strip()
        scope = scope if scope in ("global", "group", "agent") else "global"
        kind = kind if kind in MEMORY_KINDS else "fact"
        if scope == "global":
            scope_id = ""
        dup = self._one(  # type: ignore[attr-defined]
            "SELECT id FROM memories WHERE scope=? AND scope_id=? AND content=?", (scope, scope_id, content)
        )
        now = time.time()
        if dup:  # 完全相同的记忆只刷新时间,不重复存
            self._x("UPDATE memories SET updated_at=? WHERE id=?", (now, dup["id"]))  # type: ignore[attr-defined]
            return self.get_memory(dup["id"])  # type: ignore[return-value]
        mid = self.new_id()  # type: ignore[attr-defined]
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO memories(id,scope,scope_id,kind,content,pinned,source,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, scope, scope_id, kind, content, int(pinned), source, now, now),
        )
        return self.get_memory(mid)  # type: ignore[return-value]

    def update_memory(self, mid: str, patch: dict) -> dict | None:
        if patch.get("content") is not None and patch["content"].strip():
            self._x("UPDATE memories SET content=?, updated_at=? WHERE id=?",  # type: ignore[attr-defined]
                    (patch["content"].strip(), time.time(), mid))
        if patch.get("kind") in MEMORY_KINDS:
            self._x("UPDATE memories SET kind=? WHERE id=?", (patch["kind"], mid))  # type: ignore[attr-defined]
        if patch.get("pinned") is not None:
            self._x("UPDATE memories SET pinned=? WHERE id=?", (int(bool(patch["pinned"])), mid))  # type: ignore[attr-defined]
        if patch.get("scope") in ("global", "group", "agent"):
            sid = "" if patch["scope"] == "global" else str(patch.get("scope_id") or "")
            self._x("UPDATE memories SET scope=?, scope_id=? WHERE id=?", (patch["scope"], sid, mid))  # type: ignore[attr-defined]
        return self.get_memory(mid)

    def delete_memory(self, mid: str) -> None:
        self._x("DELETE FROM memories WHERE id=?", (mid,))  # type: ignore[attr-defined]

    def clear_memories(self, scope: str | None = None, scope_id: str | None = None, source: str | None = None) -> int:
        sql, args = "DELETE FROM memories WHERE 1=1", []
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if scope_id is not None:
            sql += " AND scope_id=?"
            args.append(scope_id)
        if source:
            sql += " AND source=?"
            args.append(source)
        with self._lock:  # type: ignore[attr-defined]
            n = self._db.execute(sql, args).rowcount  # type: ignore[attr-defined]
            self._db.commit()  # type: ignore[attr-defined]
        return n

    def obsidian_map(self) -> dict[str, dict]:
        return {r["memory_id"]: r for r in self._q("SELECT * FROM obsidian_map")}  # type: ignore[attr-defined]

    def set_obsidian_map(self, mid: str, rel_path: str, h: str) -> None:
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO obsidian_map(memory_id,rel_path,hash) VALUES(?,?,?) "
            "ON CONFLICT(memory_id) DO UPDATE SET rel_path=excluded.rel_path, hash=excluded.hash", (mid, rel_path, h))

    def del_obsidian_map(self, mid: str) -> None:
        self._x("DELETE FROM obsidian_map WHERE memory_id=?", (mid,))  # type: ignore[attr-defined]

    def clear_obsidian_map(self) -> None:
        self._x("DELETE FROM obsidian_map")  # type: ignore[attr-defined]

    def touch_memories(self, ids: list[str]) -> None:
        now = time.time()
        with self._lock:  # type: ignore[attr-defined]
            for i in ids:
                self._db.execute("UPDATE memories SET hits=hits+1, last_used=? WHERE id=?", (now, i))  # type: ignore[attr-defined]
            self._db.commit()  # type: ignore[attr-defined]

    def trim_memories(self, scope: str, scope_id: str, keep: int, kind: str = "action") -> None:
        """行为流水只留最近 keep 条(置顶的不删)。"""
        self._x(  # type: ignore[attr-defined]
            "DELETE FROM memories WHERE scope=? AND scope_id=? AND kind=? AND pinned=0 AND id NOT IN ("
            "SELECT id FROM memories WHERE scope=? AND scope_id=? AND kind=? ORDER BY created_at DESC LIMIT ?)",
            (scope, scope_id, kind, scope, scope_id, kind, keep),
        )

    # ------------------------------------------------------------------ library
    def list_docs(self) -> list[dict]:
        rows = self._q("SELECT * FROM library_docs ORDER BY created_at DESC, rowid DESC")  # type: ignore[attr-defined]
        for r in rows:
            r["enabled"] = bool(r["enabled"])
        return rows

    def get_doc(self, did: str) -> dict | None:
        r = self._one("SELECT * FROM library_docs WHERE id=?", (did,))  # type: ignore[attr-defined]
        if r:
            r["enabled"] = bool(r["enabled"])
        return r

    def add_doc(self, title: str, filename: str, kind: str, size: int, chunks: list[str], did: str | None = None) -> dict:
        did = did or self.new_id()  # type: ignore[attr-defined]
        with self._lock:  # type: ignore[attr-defined]
            self._db.execute(  # type: ignore[attr-defined]
                "INSERT INTO library_docs(id,title,filename,kind,size,chars,chunks,enabled,created_at) "
                "VALUES(?,?,?,?,?,?,?,1,?)",
                (did, title, filename, kind, size, sum(len(c) for c in chunks), len(chunks), time.time()),
            )
            self._db.executemany(  # type: ignore[attr-defined]
                "INSERT INTO library_chunks(doc_id,idx,text) VALUES(?,?,?)",
                [(did, i, c) for i, c in enumerate(chunks)],
            )
            self._db.commit()  # type: ignore[attr-defined]
        return self.get_doc(did)  # type: ignore[return-value]

    def update_doc(self, did: str, patch: dict) -> dict | None:
        if patch.get("title"):
            self._x("UPDATE library_docs SET title=? WHERE id=?", (patch["title"], did))  # type: ignore[attr-defined]
        if patch.get("enabled") is not None:
            self._x("UPDATE library_docs SET enabled=? WHERE id=?", (int(bool(patch["enabled"])), did))  # type: ignore[attr-defined]
        return self.get_doc(did)

    def delete_doc(self, did: str) -> None:
        self._x("DELETE FROM library_docs WHERE id=?", (did,))  # type: ignore[attr-defined]

    def doc_chunks(self, did: str) -> list[dict]:
        return self._q("SELECT idx, text FROM library_chunks WHERE doc_id=? ORDER BY idx", (did,))  # type: ignore[attr-defined]

    def all_chunks(self, doc_ids: list[str] | None = None) -> list[dict]:
        """检索用:所有已启用文档(或指定文档)的分块。"""
        sql = ("SELECT c.id, c.doc_id, c.idx, c.text, d.title FROM library_chunks c "
               "JOIN library_docs d ON d.id=c.doc_id WHERE d.enabled=1")
        args: tuple = ()
        if doc_ids is not None:
            if not doc_ids:
                return []
            sql += " AND c.doc_id IN (%s)" % ",".join("?" * len(doc_ids))
            args = tuple(doc_ids)
        return self._q(sql + " ORDER BY c.doc_id, c.idx", args)  # type: ignore[attr-defined]

    # ------------------------------------------------- model lists (new/retired)
    def set_health(self, model_id: str, status: str, detail: str = "", latency_ms: int = 0, source: str = "test") -> None:
        self._x(
            "INSERT INTO model_health(model_id,status,detail,latency_ms,source,checked_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(model_id) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
            "latency_ms=excluded.latency_ms, source=excluded.source, checked_at=excluded.checked_at",
            (model_id, status, detail[:300], int(latency_ms), source, time.time()),
        )

    def all_health(self) -> dict[str, dict]:
        return {r["model_id"]: r for r in self._q("SELECT * FROM model_health")}

    def clear_health(self, model_id: str | None = None, provider_id: str | None = None, everything: bool = False) -> None:
        """服务商的密钥/地址改了,或模型没了(或整库恢复过):旧的检测结果不再可信,清掉。"""
        if everything:
            self._x("DELETE FROM model_health")  # type: ignore[attr-defined]
        if model_id:
            self._x("DELETE FROM model_health WHERE model_id=?", (model_id,))
        if provider_id:
            self._x("DELETE FROM model_health WHERE model_id LIKE ?", (f"{provider_id}/%",))

    def get_model_seen(self, pid: str) -> list[str] | None:
        r = self._one("SELECT ids FROM model_seen WHERE provider_id=?", (pid,))  # type: ignore[attr-defined]
        return json.loads(r["ids"]) if r else None

    def set_model_seen(self, pid: str, ids: list[str]) -> None:
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO model_seen(provider_id,ids,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(provider_id) DO UPDATE SET ids=excluded.ids, updated_at=excluded.updated_at",
            (pid, json.dumps(sorted(set(ids))), time.time()),
        )

    def get_model_live(self, pid: str) -> dict | None:
        r = self._one("SELECT ids, fetched_at FROM model_live WHERE provider_id=?", (pid,))  # type: ignore[attr-defined]
        return {"ids": json.loads(r["ids"]), "fetched_at": r["fetched_at"]} if r else None

    def set_model_live(self, pid: str, ids: list[str]) -> None:
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO model_live(provider_id,ids,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(provider_id) DO UPDATE SET ids=excluded.ids, fetched_at=excluded.fetched_at",
            (pid, json.dumps(ids), time.time()),
        )

    # ------------------------------------------------------------------ updates
    def list_updates(self, status: str | None = "new") -> list[dict]:
        rows = self._q(  # type: ignore[attr-defined]
            "SELECT * FROM updates" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC",
            (status,) if status else (),
        )
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    def upsert_update(self, kind: str, ref: str, title: str, detail: dict[str, Any]) -> dict:
        """同一 kind+ref 已有未处理提醒就更新它,否则新建;已被忽略/完成的相同内容不再重复提醒。"""
        old = self._one("SELECT * FROM updates WHERE kind=? AND ref=? ORDER BY created_at DESC", (kind, ref))  # type: ignore[attr-defined]
        det = json.dumps(detail, ensure_ascii=False)
        if old:
            same = old["detail"] == det
            if old["status"] == "new" or same:
                self._x("UPDATE updates SET title=?, detail=? WHERE id=?", (title, det, old["id"]))  # type: ignore[attr-defined]
                return self.list_update(old["id"])  # type: ignore[return-value]
        uid = self.new_id()  # type: ignore[attr-defined]
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO updates(id,kind,ref,title,detail,status,created_at) VALUES(?,?,?,?,?, 'new', ?)",
            (uid, kind, ref, title, det, time.time()),
        )
        return self.list_update(uid)  # type: ignore[return-value]

    def list_update(self, uid: str) -> dict | None:
        r = self._one("SELECT * FROM updates WHERE id=?", (uid,))  # type: ignore[attr-defined]
        if r:
            r["detail"] = json.loads(r["detail"])
        return r

    def set_update_status(self, uid: str, status: str) -> None:
        self._x("UPDATE updates SET status=? WHERE id=?", (status, uid))  # type: ignore[attr-defined]

    def resolve_updates(self, kind: str, ref: str) -> None:
        self._x("UPDATE updates SET status='done' WHERE kind=? AND ref=? AND status='new'", (kind, ref))  # type: ignore[attr-defined]

    # --------------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str = "") -> str:
        r = self._one("SELECT value FROM meta WHERE key=?", (key,))  # type: ignore[attr-defined]
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )

    # ------------------------------------------------------------------ sources
    def list_sources(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self._q("SELECT * FROM sources WHERE kind=? ORDER BY name", (kind,))  # type: ignore[attr-defined]
        return self._q("SELECT * FROM sources ORDER BY kind, name")  # type: ignore[attr-defined]

    def get_source(self, kind: str, name: str) -> dict | None:
        return self._one("SELECT * FROM sources WHERE kind=? AND name=?", (kind, name))  # type: ignore[attr-defined]

    def set_source(self, kind: str, name: str, repo: str, path: str, ref: str, sha: str) -> None:
        self._x(  # type: ignore[attr-defined]
            "INSERT INTO sources(kind,name,repo,path,ref,sha,installed_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,name) DO UPDATE SET repo=excluded.repo, path=excluded.path, ref=excluded.ref, "
            "sha=excluded.sha, installed_at=excluded.installed_at",
            (kind, name, repo, path, ref, sha, time.time()),
        )

    def delete_source(self, kind: str, name: str) -> None:
        self._x("DELETE FROM sources WHERE kind=? AND name=?", (kind, name))  # type: ignore[attr-defined]
