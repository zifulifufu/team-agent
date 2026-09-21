"""SQLite 持久化层:服务商、模型、设置、agent、群聊、消息。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import strengths as strength_lib
from .catalog import Catalog
from .local_models import LocalCatalog
from .presets import DEFAULT_SETTINGS, PRESET_BY_ID, SEED_AGENTS, SEED_PROMPTS
from .store_ext import SCHEMA_EXT, ExtStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    is_local INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,               -- <provider_id>/<model_name>
    provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    avatar TEXT NOT NULL DEFAULT '🤖',
    role TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    model_id TEXT,                     -- NULL = 使用路由层默认链
    skills TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host_agent_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, agent_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    sender_type TEXT NOT NULL,          -- user | agent | system
    sender_id TEXT,
    sender_name TEXT NOT NULL,
    content TEXT NOT NULL,
    model_id TEXT,
    fallback_from TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, created_at);
-- 使用统计按「时间范围 + 只算 agent 消息」取数;没有这个索引时 5 万条消息要扫全表(实测 3.2ms),
-- 有索引走范围查找只要 0.26ms。IF NOT EXISTS 让老库在下次打开时自动补上。
CREATE INDEX IF NOT EXISTS idx_messages_agent_time ON messages(sender_type, created_at);
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    args TEXT NOT NULL DEFAULT '[]',
    env TEXT NOT NULL DEFAULT '{}',
    url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s or new_id()


DEFAULT_EXT: dict = {
    "skills": [],       # 本群启用的技能(全员共用,常放「群聊规则」类技能)
    "plugins": [],      # 本群启用的插件 ID(插件文件名)
    "mcp": [],          # 本群启用的 MCP 服务器 ID
    "library": {"mode": "all", "ids": []},   # all=全部已启用文档 | selected=只用 ids | off=不用
    "plan": "inherit",  # inherit=跟随全局设置 | auto | on | off
    "memory": True,     # 本群是否读写记忆
}


def normalize_ext(ext: Any) -> dict:
    out = json.loads(json.dumps(DEFAULT_EXT))
    if not isinstance(ext, dict):
        return out
    for k in ("skills", "plugins", "mcp"):
        if isinstance(ext.get(k), list):
            out[k] = [str(x) for x in dict.fromkeys(ext[k])]
    lib = ext.get("library")
    if isinstance(lib, dict):
        if lib.get("mode") in ("all", "selected", "off"):
            out["library"]["mode"] = lib["mode"]
        if isinstance(lib.get("ids"), list):
            out["library"]["ids"] = [str(x) for x in lib["ids"]]
    if ext.get("plan") in ("inherit", "auto", "on", "off"):
        out["plan"] = ext["plan"]
    if isinstance(ext.get("memory"), bool):
        out["memory"] = ext["memory"]
    return out


def default_data_dir() -> Path:
    return Path(os.environ.get("TEAM_AGENT_DATA", Path.home() / ".team-agent"))


_SECRET_FLAG = re.compile(r"(key|token|secret|passw|auth|bearer)", re.I)


def _mask_args(args: list) -> list:
    """备份不带密钥时,启动参数里像密钥的值也抹掉(--api-key XXX / --token=XXX)。"""
    out, hide_next = [], False
    for a in args:
        a = str(a)
        if hide_next:
            out.append("***")
            hide_next = False
        elif a.startswith("-") and "=" in a and _SECRET_FLAG.search(a.split("=", 1)[0]):
            out.append(a.split("=", 1)[0] + "=***")
        elif a.startswith("-") and _SECRET_FLAG.search(a):
            out.append(a)
            hide_next = True
        else:
            out.append(a)
    return out


class Store(ExtStore):
    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "skills").mkdir(exist_ok=True)
        (self.data_dir / "plugins").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.data_dir / "team-agent.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        # 一次群里协作要写好几条消息,默认的 delete journal + synchronous=FULL 会让每次提交都 fsync。
        # 换成 WAL + NORMAL 后写入只在检查点落盘:实测 add_message 0.27ms→0.03ms、set_health 0.20ms→0.01ms,
        # 读不受影响;WAL 只保证「不会损坏」,极端断电最多丢最后几条已提交记录(桌面应用的常规取舍)。
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        # 备份/恢复会另开连接读同一个库,留出重试窗口,避免偶发 "database is locked"。
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(SCHEMA)
        self._db.executescript(SCHEMA_EXT)
        self._migrate()
        self.catalog = Catalog(self.data_dir)
        self.local_catalog = LocalCatalog(self.data_dir)
        self._seed()

    new_id = staticmethod(new_id)

    # ------------------------------------------------------------------ helpers
    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _migrate(self) -> None:
        """给老数据库补列(CREATE TABLE IF NOT EXISTS 不会改已有的表)。"""
        adds = [
            ("models", "strengths", "TEXT"),                                 # NULL = 自动推断,否则是用户改过的 JSON 列表
            ("agents", "tags", "TEXT NOT NULL DEFAULT '[]'"),
            ("agents", "origin", "TEXT NOT NULL DEFAULT ''"),                 # 'model' = 由「模型」直接拉进群自动创建的成员
            ("agents", "engine", "TEXT NOT NULL DEFAULT ''"),                 # 非空 = 外部智能体成员(如 workbuddy),不走模型路由
            ("agents", "engine_cfg", "TEXT NOT NULL DEFAULT '{}'"),           # 外部智能体的设置(权限级别、工作目录……)
            ("groups", "ext", "TEXT NOT NULL DEFAULT '{}'"),
            ("groups", "prompt", "TEXT NOT NULL DEFAULT ''"),
            ("mcp_servers", "transport", "TEXT NOT NULL DEFAULT ''"),      # stdio | sse | http,空=自动判断
            ("mcp_servers", "headers", "TEXT NOT NULL DEFAULT '{}'"),
            ("mcp_servers", "description", "TEXT NOT NULL DEFAULT ''"),
        ]
        for table, col, decl in adds:
            cols = {r["name"] for r in self._q(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._x(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _flag(self, key: str) -> bool:
        """一次性标记(比如「已经写入过示例提示词」),避免用户删掉后又被重新种回来。"""
        if self._one("SELECT 1 FROM meta WHERE key=?", (key,)):
            return True
        self._x("INSERT INTO meta(key,value) VALUES(?, '1')", (key,))
        return False

    # --------------------------------------------------------------------- seed
    def _seed(self) -> None:
        if self._one("SELECT 1 FROM providers LIMIT 1") is None:
            self.add_provider_from_preset("deepseek")
            self.add_provider_from_preset("ollama")
        if self._one("SELECT 1 FROM settings LIMIT 1") is None:
            for k, v in DEFAULT_SETTINGS.items():
                self._x("INSERT INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))
        if self._one("SELECT 1 FROM agents LIMIT 1") is None:
            ids = [self.create_agent(**a)["id"] for a in SEED_AGENTS]
            g = self.create_group("产品发布小组", host_agent_id=ids[0], member_ids=ids)
            self.add_message(
                g["id"], "system", None, "系统",
                "欢迎!直接发消息,协调员「小助」会响应;也可以用 @成员名 点名。"
                "先在「模型」页给 DeepSeek 填入 API Key,未填或不可用时会自动回退到本地模型。",
            )

        if not self._flag("backfill_seed_tags"):
            # 从旧版本升级:内置的四个成员原来没有岗位强项,补上一次(只补空的,用户改过的不动)
            by_name = {a["name"]: a for a in SEED_AGENTS}
            for a in self.list_agents():
                seed = by_name.get(a["name"])
                if seed and not a["tags"] and seed.get("tags"):
                    self.update_agent(a["id"], {"tags": seed["tags"]})

        if not self._flag("seed_prompts"):
            for p in SEED_PROMPTS:
                self.add_prompt(p["title"], p["content"], p["kind"], p["use_globally"])

    # ----------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for r in self._q("SELECT key,value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        return out

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            self._x(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )
        return self.get_settings()

    # ---------------------------------------------------------------- providers
    def list_providers(self) -> list[dict]:
        rows = self._q("SELECT * FROM providers ORDER BY sort, rowid")
        for r in rows:
            r["enabled"] = bool(r["enabled"])
            r["is_local"] = bool(r["is_local"])
        return rows

    def get_provider(self, pid: str) -> dict | None:
        r = self._one("SELECT * FROM providers WHERE id=?", (pid,))
        if r:
            r["enabled"] = bool(r["enabled"])
            r["is_local"] = bool(r["is_local"])
        return r

    def add_provider(
        self, name: str, kind: str, base_url: str = "", api_key: str = "",
        is_local: bool = False, enabled: bool = True, pid: str | None = None,
    ) -> dict:
        pid = pid or slugify(name)
        if self.get_provider(pid):
            pid = f"{pid}-{new_id()[:4]}"
        n = self._one("SELECT COALESCE(MAX(sort),0)+1 AS n FROM providers")["n"]
        self._x(
            "INSERT INTO providers(id,name,kind,base_url,api_key,enabled,is_local,sort) VALUES(?,?,?,?,?,?,?,?)",
            (pid, name, kind, base_url, api_key, int(enabled), int(is_local), n),
        )
        return self.get_provider(pid)  # type: ignore[return-value]

    def add_provider_from_preset(self, preset_id: str, api_key: str = "") -> dict:
        p = PRESET_BY_ID[preset_id]
        prov = self.add_provider(
            p["name"], p["kind"], p["base_url"], api_key, p["is_local"], pid=p["preset"]
        )
        for m in (self.catalog.defaults(p["preset"]) if not p["is_local"] else None) or p["models"]:
            self.add_model(prov["id"], m)
        return prov

    def update_provider(self, pid: str, patch: dict) -> dict | None:
        allowed = {"name", "base_url", "api_key", "enabled", "is_local", "kind"}
        sets, args = [], []
        for k, v in patch.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                args.append(int(v) if isinstance(v, bool) else v)
        if sets:
            self._x(f"UPDATE providers SET {', '.join(sets)} WHERE id=?", (*args, pid))
            if patch.keys() & {"api_key", "base_url", "kind"}:
                self.clear_health(provider_id=pid)
        return self.get_provider(pid)

    def delete_provider(self, pid: str) -> None:
        self._x("DELETE FROM model_seen WHERE provider_id=?", (pid,))
        self._x("DELETE FROM model_live WHERE provider_id=?", (pid,))
        self.clear_health(provider_id=pid)
        for a in self._q("SELECT id FROM agents WHERE origin='model' AND model_id LIKE ?", (f"{pid}/%",)):
            self.delete_agent(a["id"])   # 「模型成员」跟着模型走:服务商删了,它们也没意义(和删单个模型一致)
        self._x("UPDATE agents SET model_id=NULL WHERE model_id LIKE ?", (f"{pid}/%",))
        self._x("DELETE FROM providers WHERE id=?", (pid,))

    # ------------------------------------------------------------------- models
    def list_models(self) -> list[dict]:
        rows = self._q(
            "SELECT m.*, p.name AS provider_name, p.kind, p.is_local, p.enabled AS provider_enabled, "
            "p.base_url AS provider_base_url FROM models m JOIN providers p ON p.id=m.provider_id "
            "ORDER BY p.sort, m.rowid"
        )
        for r in rows:
            r["enabled"] = bool(r["enabled"])
            r["is_local"] = bool(r["is_local"])
            r["provider_enabled"] = bool(r["provider_enabled"])
            prov = {"id": r["provider_id"], "base_url": r.pop("provider_base_url"), "is_local": r["is_local"]}
            custom = json.loads(r["strengths"]) if r["strengths"] else None
            auto = self.catalog.strengths_for(prov, r["model_name"])
            r["strengths_auto"] = auto
            r["strengths_custom"] = custom is not None
            r["strengths"] = custom if custom is not None else auto
            r.update(self.catalog.describe(prov, r["model_name"]))
        return rows

    def get_model(self, model_id: str) -> dict | None:
        for m in self.list_models():
            if m["id"] == model_id:
                return m
        return None

    def add_model(self, provider_id: str, model_name: str, display_name: str | None = None) -> dict:
        mid = f"{provider_id}/{model_name}"
        self._x(
            "INSERT OR IGNORE INTO models(id,provider_id,model_name,display_name,enabled) VALUES(?,?,?,?,1)",
            (mid, provider_id, model_name, display_name or model_name),
        )
        return self.get_model(mid)  # type: ignore[return-value]

    def update_model(self, model_id: str, patch: dict) -> dict | None:
        if "enabled" in patch:
            self._x("UPDATE models SET enabled=? WHERE id=?", (int(bool(patch["enabled"])), model_id))
        if patch.get("display_name"):
            self._x("UPDATE models SET display_name=? WHERE id=?", (patch["display_name"], model_id))
        if "strengths" in patch:  # None = 恢复自动推断
            tags = strength_lib.clean_tags(patch["strengths"]) if patch["strengths"] is not None else None
            self._x("UPDATE models SET strengths=? WHERE id=?",
                    (json.dumps(tags, ensure_ascii=False) if tags is not None else None, model_id))
        return self.get_model(model_id)

    def delete_model(self, model_id: str) -> None:
        for a in self._q("SELECT id FROM agents WHERE origin='model' AND model_id=?", (model_id,)):
            self.delete_agent(a["id"])  # 「模型成员」就是这个模型本身,模型没了它也没意义
        self._x("UPDATE agents SET model_id=NULL WHERE model_id=?", (model_id,))
        self.clear_health(model_id=model_id)
        self._x("DELETE FROM models WHERE id=?", (model_id,))

    # ------------------------------------------------------------------- agents
    @staticmethod
    def _agent_row(r: dict | None) -> dict | None:
        if r:
            r["skills"] = json.loads(r["skills"])
            r["tags"] = json.loads(r.get("tags") or "[]")
            try:
                cfg = json.loads(r.get("engine_cfg") or "{}")
            except ValueError:
                cfg = {}
            r["engine_cfg"] = cfg if isinstance(cfg, dict) else {}
        return r

    def list_agents(self) -> list[dict]:
        return [self._agent_row(r) for r in self._q("SELECT * FROM agents ORDER BY rowid")]  # type: ignore[misc]

    def get_agent(self, aid: str) -> dict | None:
        return self._agent_row(self._one("SELECT * FROM agents WHERE id=?", (aid,)))

    def create_agent(
        self, name: str, avatar: str = "🤖", role: str = "", prompt: str = "",
        model_id: str | None = None, skills: list[str] | None = None, tags: list[str] | None = None,
        origin: str = "", engine: str = "", engine_cfg: dict | None = None,
    ) -> dict:
        aid = new_id()
        self._x(
            "INSERT INTO agents(id,name,avatar,role,prompt,model_id,skills,tags,origin,engine,engine_cfg) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (aid, name, avatar, role, prompt, model_id, json.dumps(skills or []),
             json.dumps(strength_lib.clean_tags(tags or []), ensure_ascii=False), origin, engine,
             json.dumps(engine_cfg or {}, ensure_ascii=False)),
        )
        return self.get_agent(aid)  # type: ignore[return-value]

    MODEL_AVATARS = ("🐋", "🌙", "🔮", "🧠", "⚡", "🌟", "🦉", "🐼", "🦊", "🐙", "🌿", "🪐")
    MODEL_PROMPT = (
        "你就是模型「{{model_name}}」本身,以群成员的身份参与协作。发挥你这个模型的强项承担任务,"
        "不擅长的部分交给更合适的成员,不要各说各话。"
    )

    def ensure_model_agent(self, model_id: str) -> dict | None:
        """把「我添加的模型」变成可拉进群的成员:已有就复用,没有就创建(名字、强项都取自模型本身)。"""
        m = self.get_model(model_id)
        if not m:
            return None
        for a in self.list_agents():
            if a.get("origin") == "model" and a["model_id"] == model_id:
                return a
        base = re.sub(r"[\s@]+", "-", (m["display_name"] or m["model_name"]).strip()).strip("-") or "模型"
        taken = {a["name"] for a in self.list_agents()}
        name = base
        for cand in (base, f"{base}-{m.get('provider_name') or m['provider_id']}"):
            name = re.sub(r"[\s@]+", "-", cand)
            if name not in taken:
                break
        else:
            n = 2
            while f"{name}-{n}" in taken:
                n += 1
            name = f"{name}-{n}"
        kind = "本地" if m["is_local"] else (m.get("provider_name") or "云端")
        avatar = self.MODEL_AVATARS[sum(ord(ch) for ch in m["provider_id"]) % len(self.MODEL_AVATARS)]
        return self.create_agent(name, avatar, f"模型成员 · {kind}", self.MODEL_PROMPT, model_id, [], [], origin="model")

    def update_agent(self, aid: str, patch: dict) -> dict | None:
        for k in ("name", "avatar", "role", "prompt", "model_id"):
            if k in patch:
                self._x(f"UPDATE agents SET {k}=? WHERE id=?", (patch[k], aid))
        if "skills" in patch and patch["skills"] is not None:
            self._x("UPDATE agents SET skills=? WHERE id=?", (json.dumps(patch["skills"]), aid))
        if isinstance(patch.get("engine_cfg"), dict):
            self._x("UPDATE agents SET engine_cfg=? WHERE id=?", (json.dumps(patch["engine_cfg"], ensure_ascii=False), aid))
        if "tags" in patch and patch["tags"] is not None:
            self._x("UPDATE agents SET tags=? WHERE id=?",
                    (json.dumps(strength_lib.clean_tags(patch["tags"]), ensure_ascii=False), aid))
        return self.get_agent(aid)

    def delete_agent(self, aid: str) -> None:
        self._x("UPDATE groups SET host_agent_id=NULL WHERE host_agent_id=?", (aid,))
        self._x("DELETE FROM agents WHERE id=?", (aid,))

    # ------------------------------------------------------------------- groups
    @staticmethod
    def _group_row(g: dict | None) -> dict | None:
        if g:
            g["ext"] = normalize_ext(json.loads(g.get("ext") or "{}"))
        return g

    def list_groups(self) -> list[dict]:
        groups = self._q("SELECT * FROM groups ORDER BY created_at")
        for g in groups:
            self._group_row(g)
            g["member_ids"] = self.member_ids(g["id"])
            last = self._one(
                "SELECT content, created_at FROM messages WHERE group_id=? ORDER BY created_at DESC LIMIT 1",
                (g["id"],),
            )
            g["last_message"] = last["content"][:60] if last else ""
            g["last_at"] = last["created_at"] if last else g["created_at"]
        return groups

    def get_group(self, gid: str) -> dict | None:
        g = self._group_row(self._one("SELECT * FROM groups WHERE id=?", (gid,)))
        if g:
            g["member_ids"] = self.member_ids(gid)
        return g

    def member_ids(self, gid: str) -> list[str]:
        return [
            r["agent_id"]
            for r in self._q("SELECT agent_id FROM group_members WHERE group_id=? ORDER BY position", (gid,))
        ]

    def group_members(self, gid: str) -> list[dict]:
        return [a for a in (self.get_agent(i) for i in self.member_ids(gid)) if a]

    def create_group(self, name: str, host_agent_id: str | None = None, member_ids: list[str] | None = None,
                     ext: dict | None = None, prompt: str = "") -> dict:
        gid = new_id()
        self._x(
            "INSERT INTO groups(id,name,host_agent_id,created_at,ext,prompt) VALUES(?,?,?,?,?,?)",
            (gid, name, host_agent_id, time.time(), json.dumps(normalize_ext(ext), ensure_ascii=False), prompt),
        )
        for i, aid in enumerate(member_ids or []):
            self.add_member(gid, aid, i)
        return self.get_group(gid)  # type: ignore[return-value]

    def update_group(self, gid: str, patch: dict) -> dict | None:
        if patch.get("name"):
            self._x("UPDATE groups SET name=? WHERE id=?", (patch["name"], gid))
        if "host_agent_id" in patch:
            self._x("UPDATE groups SET host_agent_id=? WHERE id=?", (patch["host_agent_id"], gid))
        if patch.get("prompt") is not None:
            self._x("UPDATE groups SET prompt=? WHERE id=?", (patch["prompt"], gid))
        if isinstance(patch.get("ext"), dict):
            cur = self.get_group(gid)
            merged = {**(cur["ext"] if cur else {}), **patch["ext"]}  # type: ignore[index]
            self._x("UPDATE groups SET ext=? WHERE id=?", (json.dumps(normalize_ext(merged), ensure_ascii=False), gid))
        return self.get_group(gid)

    def delete_group(self, gid: str) -> None:
        self._x("DELETE FROM groups WHERE id=?", (gid,))

    def add_member(self, gid: str, aid: str, position: int | None = None) -> None:
        if position is None:
            position = self._one(
                "SELECT COALESCE(MAX(position),-1)+1 AS n FROM group_members WHERE group_id=?", (gid,)
            )["n"]
        self._x(
            "INSERT OR IGNORE INTO group_members(group_id,agent_id,position) VALUES(?,?,?)",
            (gid, aid, position),
        )

    def remove_member(self, gid: str, aid: str) -> None:
        self._x("DELETE FROM group_members WHERE group_id=? AND agent_id=?", (gid, aid))
        self._x("UPDATE groups SET host_agent_id=NULL WHERE id=? AND host_agent_id=?", (gid, aid))

    # ----------------------------------------------------------------- messages
    def add_message(
        self, gid: str, sender_type: str, sender_id: str | None, sender_name: str, content: str,
        model_id: str | None = None, fallback_from: str | None = None, meta: dict | None = None,
        mid: str | None = None,
    ) -> dict:
        mid = mid or new_id()
        self._x(
            "INSERT INTO messages(id,group_id,sender_type,sender_id,sender_name,content,model_id,fallback_from,meta,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, gid, sender_type, sender_id, sender_name, content, model_id, fallback_from,
             json.dumps(meta or {}, ensure_ascii=False), time.time()),
        )
        return self.get_message(mid)  # type: ignore[return-value]

    def update_message(self, mid: str, content: str | None = None, meta: dict | None = None) -> dict | None:
        if content is not None:
            self._x("UPDATE messages SET content=? WHERE id=?", (content, mid))
        if meta is not None:
            self._x("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), mid))
        return self.get_message(mid)

    def get_message(self, mid: str) -> dict | None:
        r = self._one("SELECT * FROM messages WHERE id=?", (mid,))
        if r:
            r["meta"] = json.loads(r["meta"])
        return r

    def list_messages(self, gid: str, limit: int = 200) -> list[dict]:
        # 外层不能直接写 ORDER BY rowid:新版 SQLite(≥3.51)里子查询的 rowid 对外层不可见,会报 no such column: rowid。
        # 所以在子查询里把它取成 _rid,外层按 _rid 排,取完再去掉。
        rows = self._q(
            "SELECT * FROM (SELECT *, rowid AS _rid FROM messages WHERE group_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?) "
            "ORDER BY created_at, _rid",
            (gid, limit),
        )
        for r in rows:
            r.pop("_rid", None)
            r["meta"] = json.loads(r["meta"])
        return rows

    def clear_messages(self, gid: str) -> None:
        self._x("DELETE FROM messages WHERE group_id=?", (gid,))

    def clear_all_messages(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self._db.execute("DELETE FROM messages")
            self._db.commit()
        return n

    def agent_message_rows(self, since: float = 0) -> list[dict]:
        """统计用:所有 agent 发言的模型、回退、耗时信息。"""
        rows = self._q(
            "SELECT model_id, fallback_from, meta, created_at FROM messages "
            "WHERE sender_type='agent' AND created_at>=? ORDER BY created_at",
            (since,),
        )
        for r in rows:
            r["meta"] = json.loads(r["meta"])
        return rows

    def backup_to(self, dest: Path | str, include_keys: bool = False) -> None:
        """用 SQLite 在线备份 API 生成一致的快照;默认清除其中的 API Key。"""
        dest = Path(dest)
        with self._lock:
            out = sqlite3.connect(dest)
            try:
                self._db.backup(out)
                if not include_keys:
                    out.execute("UPDATE providers SET api_key=''")
                    out.execute("UPDATE mcp_servers SET env='{}', headers='{}'")  # env/headers 里常放各种密钥
                    for mid, url, args in out.execute("SELECT id, url, args FROM mcp_servers").fetchall():
                        out.execute("UPDATE mcp_servers SET url=?, args=? WHERE id=?",
                                    (url.split("?", 1)[0], json.dumps(_mask_args(json.loads(args or "[]")), ensure_ascii=False), mid))
                    out.execute("UPDATE settings SET value='\"\"' WHERE key='github_token'")
                    out.commit()
                out.execute("VACUUM")
            finally:
                out.close()

    RESTORE_TABLES = {"providers", "models", "agents", "groups", "messages", "settings"}

    def _check_backup(self, work: Path) -> None:
        """在临时副本上把备份「按当前版本的样子补齐并检查一遍」,任何一步不通过都抛 ValueError——
        这样一个坏文件在碰到真实数据库之前就被挡下了。"""
        try:
            con = sqlite3.connect(work)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("文件已损坏")
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not self.RESTORE_TABLES <= tables:
                    raise ValueError("缺少必要的数据表")
                con.executescript(SCHEMA)
                con.executescript(SCHEMA_EXT)
                for _k, v in con.execute("SELECT key, value FROM settings").fetchall():
                    json.loads(v)
                con.commit()
            finally:
                con.close()
        except sqlite3.DatabaseError:
            raise ValueError("这不是有效的备份文件(不是 Team Agent 的 SQLite 数据库,或者版本不兼容)") from None
        except ValueError as e:
            raise ValueError(f"这不是可用的 Team Agent 备份:{e}") from None

    def restore_from(self, src: Path | str) -> dict:
        """用备份文件替换当前全部数据。先自动留一份当前数据的副本(backups/pre-restore-*.db)。
        备份是「不含密钥」的版本时,当前已有的 API Key / MCP 密钥 / GitHub 令牌会保留下来,不会被清空。
        备份里记的 Obsidian 文件夹和同步对应关系不带过来(那台电脑上的路径在这里未必存在,盲目同步会误删记忆)。"""
        import shutil

        bdir = self.data_dir / "backups"
        bdir.mkdir(exist_ok=True)
        work = bdir / f".restore-{uuid.uuid4().hex[:8]}.db"
        try:
            shutil.copyfile(Path(src), work)
            self._check_backup(work)
            safety = bdir / time.strftime("pre-restore-%Y%m%d-%H%M%S.db")
            if safety.exists():
                safety = bdir / f"{safety.stem}-{uuid.uuid4().hex[:4]}.db"
            self.backup_to(safety, include_keys=True)
            old_keys = {p["id"]: p["api_key"] for p in self.list_providers()}
            old_mcp = {m["id"]: (m["env"], m["headers"]) for m in self.list_mcp()}
            old_gh = self.get_settings().get("github_token", "")
            with self._lock:
                con = sqlite3.connect(work)
                try:
                    con.backup(self._db)
                finally:
                    con.close()
                self._db.execute("PRAGMA foreign_keys = ON")
        finally:
            # 主库现在是 WAL;临时副本仍用 delete journal,但把 -wal/-shm 一并清掉以防将来改动漏文件
            for f in (work, *[Path(str(work) + s) for s in ("-journal", "-wal", "-shm")]):
                f.unlink(missing_ok=True)
        self._migrate()
        for p in self.list_providers():
            if not p["api_key"] and old_keys.get(p["id"]):
                self._x("UPDATE providers SET api_key=? WHERE id=?", (old_keys[p["id"]], p["id"]))
        for m in self.list_mcp():
            env, headers = old_mcp.get(m["id"], ({}, {}))
            if not m["env"] and not m["headers"] and (env or headers):
                self._x("UPDATE mcp_servers SET env=?, headers=? WHERE id=?", (json.dumps(env), json.dumps(headers), m["id"]))
        if old_gh and not self.get_settings().get("github_token"):
            self.update_settings({"github_token": old_gh})
        self.clear_obsidian_map()
        self.clear_health(everything=True)
        self.update_settings({"obsidian_dir": "", "obsidian_auto": False})
        self._seed()
        return {"safety_copy": str(safety), "groups": len(self.list_groups()), "agents": len(self.list_agents()),
                "providers": len(self.list_providers()), "memories": len(self.list_memories(limit=1_000_000)),
                "docs": len(self.list_docs())}

    # ---------------------------------------------------------------------- mcp
    def list_mcp(self) -> list[dict]:
        rows = self._q("SELECT * FROM mcp_servers ORDER BY rowid")
        for r in rows:
            r["args"] = json.loads(r["args"])
            r["env"] = json.loads(r["env"])
            r["headers"] = json.loads(r.get("headers") or "{}")
            r["enabled"] = bool(r["enabled"])
        return rows

    def get_mcp(self, mid: str) -> dict | None:
        return next((m for m in self.list_mcp() if m["id"] == mid), None)

    def add_mcp(self, name: str, command: str = "", args: list[str] | None = None,
                env: dict | None = None, url: str = "", transport: str = "",
                headers: dict | None = None, description: str = "") -> dict:
        mid = new_id()
        self._x(
            "INSERT INTO mcp_servers(id,name,command,args,env,url,enabled,transport,headers,description) "
            "VALUES(?,?,?,?,?,?,1,?,?,?)",
            (mid, name, command, json.dumps(args or []), json.dumps(env or {}), url, transport,
             json.dumps(headers or {}), description),
        )
        return self.get_mcp(mid)  # type: ignore[return-value]

    def update_mcp(self, mid: str, patch: dict) -> dict | None:
        for k in ("name", "command", "url", "transport", "description"):
            if patch.get(k) is not None:
                self._x(f"UPDATE mcp_servers SET {k}=? WHERE id=?", (patch[k], mid))
        for k in ("args", "env", "headers"):
            if patch.get(k) is not None:
                self._x(f"UPDATE mcp_servers SET {k}=? WHERE id=?", (json.dumps(patch[k], ensure_ascii=False), mid))
        if patch.get("enabled") is not None:
            self._x("UPDATE mcp_servers SET enabled=? WHERE id=?", (int(bool(patch["enabled"])), mid))
        return self.get_mcp(mid)

    def delete_mcp(self, mid: str) -> None:
        self._x("DELETE FROM mcp_servers WHERE id=?", (mid,))
