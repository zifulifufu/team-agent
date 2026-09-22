"""SQLite persistence layer: providers, models, settings, agents, group chats, messages."""

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
from . import channels
from . import video
from .catalog import Catalog
from .local_models import LocalCatalog
from . import i18n
from .presets import (
    DEFAULT_SETTINGS,
    PRESET_BY_ID,
    SEED_AGENTS,
    SEED_PROMPTS,
    builtin_for,
    model_member_prompt,
    model_member_role,
)

from . import secrets as secrets_store
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
    model_id TEXT,                     -- NULL = use the router's default chain
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
-- Usage stats are queried by "time range, agent messages only"; without this index a
-- 50k-message table scans in full (measured 3.2ms) against 0.26ms for a range lookup.
-- IF NOT EXISTS lets an older database pick it up the next time it is opened.
CREATE INDEX IF NOT EXISTS idx_messages_agent_time ON messages(sender_type, created_at);
-- Images attached to a message. The bytes live in <data dir>/attachments/, the row here
-- only carries what the UI and the prompt need; a message references its images by id
-- inside its own `meta`, so the transcript keeps working without a join.
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at REAL NOT NULL
);
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
    "skills": [],       # skills enabled for this group (shared by everyone, usually the "group rules" kind)
    "plugins": [],      # plugin ids enabled for this group (plugin file name)
    "mcp": [],          # MCP server ids enabled for this group
    # Which knowledge bases this group searches. all = everything it can reach (its workspace's
    # own + the shared ones) | selected = the kb_ids listed plus the members of the collection_ids
    # listed | off = none. Selection is by knowledge base, not by document.
    "library": {"mode": "all", "kb_ids": [], "collection_ids": []},
    "plan": "inherit",  # inherit = follow the global setting | auto | on | off
    "memory": True,     # whether this group reads and writes memory
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
        for key in ("kb_ids", "collection_ids"):
            if isinstance(lib.get(key), list):
                out["library"][key] = [str(x) for x in dict.fromkeys(lib[key])]
    if ext.get("plan") in ("inherit", "auto", "on", "off"):
        out["plan"] = ext["plan"]
    if isinstance(ext.get("memory"), bool):
        out["memory"] = ext["memory"]
    return out


def default_data_dir() -> Path:
    return Path(os.environ.get("TEAM_AGENT_DATA", Path.home() / ".team-agent"))


_SECRET_FLAG = re.compile(r"(key|token|secret|passw|auth|bearer)", re.I)


def _mask_args(args: list) -> list:
    """When the backup carries no keys, values in the launch arguments that look like keys are
masked as well (--api-key XXX / --token=XXX)."""
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
        # one group collaboration writes several messages, and the default delete journal +
# synchronous=FULL fsyncs on every commit.
        # with WAL + NORMAL, writes only reach disk at checkpoints: measured add_message
# 0.27ms -> 0.03ms and set_health 0.20ms -> 0.01ms,
        # reads are unaffected; WAL only guarantees "no corruption", and a hard power loss can lose
# the last few committed records (the usual trade-off for a desktop app).
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        # backup/restore opens another connection to the same database, so leave a retry window to
# avoid the occasional "database is locked".
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
        """Add columns to older databases (CREATE TABLE IF NOT EXISTS never alters an existing table)."""
        adds = [
            ("models", "strengths", "TEXT"),                                 # NULL = inferred automatically, otherwise the JSON list the user edited
            ("agents", "tags", "TEXT NOT NULL DEFAULT '[]'"),
            ("agents", "origin", "TEXT NOT NULL DEFAULT ''"),                 # 'model' = a member created automatically by pulling a model straight into a group
            ("agents", "engine", "TEXT NOT NULL DEFAULT ''"),                 # non-empty = external agent member (e.g. workbuddy), which skips model routing
            ("agents", "engine_cfg", "TEXT NOT NULL DEFAULT '{}'"),           # settings of the external agent (permission level, working directory, ...)
            ("groups", "ext", "TEXT NOT NULL DEFAULT '{}'"),
            ("groups", "prompt", "TEXT NOT NULL DEFAULT ''"),
            ("mcp_servers", "transport", "TEXT NOT NULL DEFAULT ''"),      # stdio | sse | http, empty = auto-detect
            ("mcp_servers", "headers", "TEXT NOT NULL DEFAULT '{}'"),
            ("mcp_servers", "description", "TEXT NOT NULL DEFAULT ''"),
            # Knowledge bases: documents moved from "a group's library" to "a knowledge base"
            ("library_docs", "kb_id", "TEXT NOT NULL DEFAULT ''"),
        ]
        for table, col, decl in adds:
            cols = {r["name"] for r in self._q(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._x(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # Indexes on a migrated column cannot live in the schema scripts: on an older database
        # CREATE TABLE IF NOT EXISTS does nothing, so the column only exists after the ALTER
        # above and the index creation would fail with "no such column" before reaching it.
        for sql in ("CREATE INDEX IF NOT EXISTS idx_docs_kb ON library_docs(kb_id)",
                    "CREATE INDEX IF NOT EXISTS idx_kbs_group ON knowledge_bases(group_id)"):
            self._x(sql)

        self._docs_to_knowledge_bases()

    def _docs_to_knowledge_bases(self) -> None:
        """Move the pre-knowledge-base library into knowledge bases.

        A document used to belong either to one group's library (`group_id = <group>`) or to
        everyone (`group_id = ''`). Each bucket becomes one knowledge base that keeps exactly
        the visibility it had: a group's library becomes that workspace's own knowledge base,
        and the shared bucket becomes a single shared one. No document changes hands, and a
        database from before the per-group library (no `group_id` column at all) simply ends up
        with everything in the shared knowledge base — which is what it was.

        Driven by the data rather than by a "have I run?" flag: a crash halfway through would
        otherwise leave the flag set and the remaining documents unassigned for good. Re-running
        only picks up what is still unassigned, so it is safe on every open.
        """
        cols = {r["name"] for r in self._q("PRAGMA table_info(library_docs)")}
        if not cols or "kb_id" not in cols:
            return
        by_group = "group_id" in cols
        names = {g["id"]: g["name"] for g in self.list_groups()}
        if by_group:
            # `COALESCE` because the column is not declared NOT NULL: a row that was inserted with
            # no owner at all carries NULL, and NULL means the same thing here as the empty string
            # — "no group". It is not cosmetic: `knowledge_bases.group_id` *is* NOT NULL, so
            # handing this NULL on to `add_kb` raised `IntegrityError: NOT NULL constraint failed`
            # while the database was being opened, which left the whole app unable to start for
            # anyone whose data contained such a row.
            leftovers = [r["gid"] for r in self._q(
                "SELECT DISTINCT COALESCE(group_id,'') AS gid FROM library_docs WHERE kb_id=''")]
        else:
            leftovers = [""] if self._one("SELECT 1 AS x FROM library_docs WHERE kb_id='' LIMIT 1") else []
        for gid in leftovers:
            gid = "" if gid is None else gid
            existing = self._one("SELECT * FROM knowledge_bases WHERE group_id=?", (gid,))
            if existing:
                kb = existing
            else:
                # Named after the group when it still exists; a bucket whose group has been
                # deleted keeps a name the user can recognise and rename, rather than borrowing
                # the shared one and colliding with it.
                kb = self.add_kb(
                    names.get(gid) or ("Shared knowledge base" if not gid else f"Workspace {gid}"),
                    "Documents every group can search" if not gid else "This group's own documents",
                    gid,
                )
            if not by_group:
                self._x("UPDATE library_docs SET kb_id=? WHERE kb_id=''", (kb["id"],))
            elif gid:
                self._x("UPDATE library_docs SET kb_id=? WHERE kb_id='' AND group_id=?", (kb["id"], gid))
            else:
                # The shared bucket, which is where an unowned document belongs — including the
                # NULL row that the equality test cannot reach.
                self._x("UPDATE library_docs SET kb_id=? WHERE kb_id='' AND (group_id='' OR group_id IS NULL)",
                        (kb["id"],))
        self._library_selection_to_kbs()
        if by_group:
            # The column is fully derived from the knowledge base now, so it goes: leaving it
            # behind would invite someone to read ownership from two places that can disagree.
            self._x("DROP INDEX IF EXISTS idx_docs_group")
            try:
                self._x("ALTER TABLE library_docs DROP COLUMN group_id")
            except sqlite3.OperationalError as e:      # older SQLite, or a dependency we cannot see
                print("could not drop library_docs.group_id:", e)

    def _library_selection_to_kbs(self) -> None:
        """A group that picked individual documents now picks knowledge bases.

        The old `ext.library.ids` listed documents; the new shape lists knowledge bases and
        collections. Mapping the picked documents to the knowledge bases that hold them can only
        widen the scope within what that group could already reach — never across a workspace
        boundary — because a document could only ever be picked from what the group could see.
        """
        moved = 0
        # The stored JSON, not `list_groups()`: `normalize_ext` already drops the retired `ids`
        # field, so a normalised view would show nothing left to migrate.
        for row in self._q("SELECT id, ext FROM groups"):
            try:
                raw = json.loads(row["ext"] or "{}")
            except ValueError:
                continue
            lib = (raw.get("library") or {}) if isinstance(raw, dict) else {}
            ids = lib.get("ids") or []
            if not ids:
                continue
            kb_ids = [d["kb_id"] for d in self.list_docs() if d["id"] in set(map(str, ids)) and d["kb_id"]]
            g = self.get_group(row["id"])
            if not g:
                continue
            ext = dict(g["ext"])
            ext["library"] = {**ext.get("library", {}), "kb_ids": list(dict.fromkeys(kb_ids))}
            self.update_group(row["id"], {"ext": ext})
            moved += 1
        if moved:
            print(f"library selection migrated to knowledge bases for {moved} group(s)")

    def _flag(self, key: str) -> bool:
        """One-off markers (for example "the example prompts have already been written"), so
something the user deleted is not seeded again."""
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
            # SEED_AGENTS carries `<field>_zh` alongside the English base value; those
            # are for display only, so they never reach create_agent().
            ids = [self.create_agent(**{k: v for k, v in a.items() if not k.endswith("_zh")})["id"]
                   for a in SEED_AGENTS]
            lang = i18n.current()
            g = self.create_group(
                i18n.pick(lang, "Product launch group", "产品发布小组"),
                host_agent_id=ids[0], member_ids=ids,
            )
            self.add_message(
                g["id"], "system", None, i18n.pick(lang, "System", "系统"),
                i18n.pick(
                    lang,
                    "Welcome! Just send a message and Aide, the coordinator, will pick it up; "
                    "you can also @mention a member. Start by adding a DeepSeek API key on the "
                    "Models page — without one (or when it is unavailable) this falls back to a "
                    "local model.",
                    "欢迎!直接发消息,协调员「小助」会响应;也可以用 @成员名 点名。"
                    "先在「模型」页给 DeepSeek 填入 API Key,未填或不可用时会自动回退到本地模型。",
                ),
            )

        if not self._flag("backfill_seed_tags"):
            # upgrade from an older version: the four built-in members had no role strengths; fill them
# in once (only the empty ones, never ones the user changed)
            for a in self.list_agents():
                seed = builtin_for(a["name"])
                if seed and not a["tags"] and seed.get("tags"):
                    self.update_agent(a["id"], {"tags": seed["tags"]})

        if not self._flag("seed_prompts"):
            for p in SEED_PROMPTS:
                self.add_prompt(p["title"], p["content"], p["kind"], p["use_globally"])

        if not self._flag("auto_check_off_by_default"):
            # upgrade from an older version: "check for updates automatically" used to default to on,
# and the backend went online by itself 20 seconds after start.
            # the new default is off, so existing installs that still have it on are switched off here.
# This happens exactly once — anything you turn on later in settings is not reverted.
            self.update_settings({"auto_check_updates": False})

        if not self._flag("keys_to_keychain"):
            # upgrade from an older version: API keys and GitHub tokens used to be stored in plaintext in
# the database; move them into the system keychain (leave them in plaintext if they cannot move)
            self._move_keys_to_keychain()

        if not self._flag("tags_to_ascii_ids"):
            # Older builds stored the Chinese label itself as the strength-tag id
            # ("代码" instead of "coding"). The values are unchanged, only the ids are
            # normalized so they stay language-neutral. See app/strengths.ALIASES.
            self._normalize_stored_tags()

    # ------------------------------------------------- settings
    # Settings whose value belongs in the keychain rather than the database: the stored
    # form is a `keychain:` reference, and only `get_settings()` resolves it. Keeping the
    # list here (rather than special-casing one key) means a new secret cannot be added
    # half-way — forgetting to register it here is the difference between "stored safely"
    # and "written in plaintext into a database that ends up in backups".
    SECRET_SETTINGS: dict[str, tuple[str, str]] = {
        "github_token": ("github-token", "default"),
        # One entry per secret field a channel declares, so a new channel cannot ship a
        # credential that is quietly written into the database in the clear.
        **channels.secrets(),
    }

    def get_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for r in self._q("SELECT key,value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        for k in self.SECRET_SETTINGS:                                   # reference -> real value
            out[k] = self._secret_off(out[k])
        return out

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            if k in self.SECRET_SETTINGS and isinstance(v, str):
                v = self._secret_on(*self.SECRET_SETTINGS[k], v)         # the real value goes to the keychain, only the reference stays in the database
            self._x(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )
        return self.get_settings()

    # ------------------------------------------------------- sensitive values (API keys etc.)
    def _secret_on(self, scope: str, ident: str, value: str) -> str:
        """Write: store only a reference when the system keychain is available; store it as-is
otherwise (fall back to plaintext, never lose the key)."""
        ref = secrets_store.ref_name(scope, ident)
        if not value:
            secrets_store.delete(ref)          # clearing also removes the keychain entry while we are at it
            return value
        if secrets_store.is_ref(value):
            return value
        return secrets_store.make_ref(ref) if secrets_store.put(ref, value) else value

    def _secret_off(self, stored: Any) -> str:
        """Read: for a reference, fetch the real value from the keychain; when it cannot be read
(different machine / deleted) treat it as unconfigured rather than crashing."""
        if not secrets_store.is_ref(stored):
            return stored or ""
        got = secrets_store.get(secrets_store.parse_ref(stored))
        return got if got is not None else ""

    def secret_backend(self) -> str:
        """Where keys are stored: `keychain` = the system keychain, `plaintext` = fell back to
plaintext (non-macOS / keychain unavailable)."""
        return "keychain" if secrets_store.backend_available() else "plaintext"

    def _normalize_stored_tags(self) -> int:
        """Replace stored strength tags whose id is a Chinese tag name with the ASCII id
        (see strengths.ALIASES).

        Only the id changes, never the meaning: old value "代码" -> new value "coding"; what the
        UI shows is decided by the language. Unknown tags are dropped (the same rule as when a
        user submits tags). Returns the number of rows changed.
        """
        changed = 0
        for r in self._q("SELECT id, strengths FROM models WHERE strengths IS NOT NULL AND strengths<>''"):
            try:
                old = json.loads(r["strengths"])
            except ValueError:
                continue
            new = strength_lib.clean_tags(old)
            if new != old:
                self._x("UPDATE models SET strengths=? WHERE id=?", (json.dumps(new, ensure_ascii=False), r["id"]))
                changed += 1
        for r in self._q("SELECT id, tags FROM agents WHERE tags IS NOT NULL AND tags<>''"):
            try:
                old = json.loads(r["tags"])
            except ValueError:
                continue
            new = strength_lib.clean_tags(old)
            if new != old:
                self._x("UPDATE agents SET tags=? WHERE id=?", (json.dumps(new, ensure_ascii=False), r["id"]))
                changed += 1
        return changed

    def _move_keys_to_keychain(self) -> int:
        """Move the API keys / GitHub tokens that an older database stored in plaintext into the
        keychain. **The value is read back and verified before anything is rewritten**; if it
        cannot be written (keychain locked, etc.) the plaintext is left as-is and never cleared.
        Returns how many entries were moved."""
        if not secrets_store.backend_available():
            return 0
        moved = 0
        for r in self._q("SELECT id, api_key FROM providers WHERE api_key<>''"):
            if secrets_store.is_ref(r["api_key"]):
                continue
            ref = secrets_store.ref_name("provider", r["id"])
            if secrets_store.put(ref, r["api_key"]):
                secrets_store.forget_cache(ref)
                if secrets_store.get(ref) == r["api_key"]:      # only rewrite when the value can really be read back
                    self._x("UPDATE providers SET api_key=? WHERE id=?", (secrets_store.make_ref(ref), r["id"]))
                    moved += 1
        row = self._one("SELECT value FROM settings WHERE key='github_token'")
        if row:
            val = json.loads(row["value"])
            if isinstance(val, str) and val and not secrets_store.is_ref(val):
                ref = secrets_store.ref_name("github-token", "default")
                if secrets_store.put(ref, val):
                    secrets_store.forget_cache(ref)
                    if secrets_store.get(ref) == val:
                        self._x("UPDATE settings SET value=? WHERE key='github_token'",
                                (json.dumps(secrets_store.make_ref(ref)),))
                        moved += 1
        return moved

    # ---------------------------------------------------------------- providers
    def list_providers(self) -> list[dict]:
        rows = self._q("SELECT * FROM providers ORDER BY sort, rowid")
        for r in rows:
            r["enabled"] = bool(r["enabled"])
            r["is_local"] = bool(r["is_local"])
            r["api_key"] = self._secret_off(r["api_key"])      # reference -> real key (callers need not know how it is stored)
        return rows

    def get_provider(self, pid: str) -> dict | None:
        r = self._one("SELECT * FROM providers WHERE id=?", (pid,))
        if r:
            r["enabled"] = bool(r["enabled"])
            r["is_local"] = bool(r["is_local"])
            r["api_key"] = self._secret_off(r["api_key"])
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
            (pid, name, kind, base_url, self._secret_on("provider", pid, api_key), int(enabled), int(is_local), n),
        )
        return self.get_provider(pid)  # type: ignore[return-value]

    def add_provider_from_preset(self, preset_id: str, api_key: str = "") -> dict:
        # The name is stored in its canonical English, like the rest of the built-in content: the
        # display layer puts the reader's language on top (see `presets.localize_provider`).
        # Storing it already localized meant a provider added from a Chinese interface was called
        # 月之暗面 Kimi for ever, in an English one too.
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
                if k == "api_key":
                    v = self._secret_on("provider", pid, v)     # only the reference is written to the database, the real key goes to the keychain
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
            self.delete_agent(a["id"])   # "model members" follow their model: once the provider is deleted they are meaningless too
# (same as deleting a single model)
        self._x("UPDATE agents SET model_id=NULL WHERE model_id LIKE ?", (f"{pid}/%",))
        self._x("DELETE FROM providers WHERE id=?", (pid,))

    # ------------------------------------------------------------------- models
    MODEL_SELECT = (
        "SELECT m.*, p.name AS provider_name, p.kind, p.is_local, p.enabled AS provider_enabled, "
        "p.base_url AS provider_base_url FROM models m JOIN providers p ON p.id=m.provider_id "
    )

    def _model_row(self, r: dict) -> dict:
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
        return r

    def list_models(self) -> list[dict]:
        """The chat models, i.e. everything a member can be pointed at.

        Media providers (MiniMax H3 and friends) are generation services with no model string at
        all, so their rows are filtered out here. This is the single place "the models" are
        enumerated, which is what keeps a media row out of the model picker, the routing chain and
        the health probe at once. Anything that needs a specific row rather than the roster must
        use `get_model`, which is a plain lookup and deliberately knows nothing about this.
        """
        marks = ",".join("?" * len(video.MEDIA_KINDS))
        rows = self._q(
            self.MODEL_SELECT + f"WHERE p.kind NOT IN ({marks}) ORDER BY p.sort, m.rowid",
            tuple(video.MEDIA_KINDS),
        )
        return [self._model_row(r) for r in rows]

    def get_model(self, model_id: str) -> dict | None:
        """One row, whatever kind of provider it hangs off.

        A direct query rather than a scan of `list_models()`: that list is the *chat* roster and
        filters media providers out, so building this on top of it would make a storage lookup
        fail for rows that exist — which is exactly what happened when the filter went in
        (`add_model` returned None and the API answered with a validation error).
        """
        r = self._one(self.MODEL_SELECT + "WHERE m.id=?", (model_id,))
        return self._model_row(r) if r else None

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
        if "strengths" in patch:  # None = go back to inferring automatically
            tags = strength_lib.clean_tags(patch["strengths"]) if patch["strengths"] is not None else None
            self._x("UPDATE models SET strengths=? WHERE id=?",
                    (json.dumps(tags, ensure_ascii=False) if tags is not None else None, model_id))
        return self.get_model(model_id)

    def delete_model(self, model_id: str) -> None:
        for a in self._q("SELECT id FROM agents WHERE origin='model' AND model_id=?", (model_id,)):
            self.delete_agent(a["id"])  # a "model member" is the model itself, so it is meaningless once the model is gone
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

    def ensure_model_agent(self, model_id: str) -> dict | None:
        """Turn "a model I added" into a member that can be pulled into a group: reuse it when it
already exists, otherwise create it (name and strengths are both taken from the model)."""
        m = self.get_model(model_id)
        if not m or m.get("kind") in video.MEDIA_KINDS:
            return None                 # a video provider has no chat model to turn into a member
        for a in self.list_agents():
            if a.get("origin") == "model" and a["model_id"] == model_id:
                return a
        base = re.sub(r"[\s@]+", "-", (m["display_name"] or m["model_name"]).strip()).strip("-") \
            or i18n.pick_now("Model", "模型")
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
        # Stored canonically in English: the display layer and the prompt builder both
        # run this member through presets.localize_model_member().
        kind = "Local" if m["is_local"] else (m.get("provider_name") or "Cloud")
        avatar = self.MODEL_AVATARS[sum(ord(ch) for ch in m["provider_id"]) % len(self.MODEL_AVATARS)]
        return self.create_agent(name, avatar, model_member_role(kind), model_member_prompt(),
                                 model_id, [], [], origin="model")

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
            cur_ext = dict(cur["ext"]) if cur else {}
            merged = {**cur_ext, **patch["ext"]}  # type: ignore[index]
            # `library` is merged one level deeper than the rest. The other keys are whole lists,
            # so replacing them is the point; `library` is an object, and replacing it wholesale
            # means a caller that sends only `kb_ids` resets `mode` to its default — which is
            # "all", i.e. every knowledge base the group can reach, silently wider than the ones
            # the user ticked. Every caller today spreads the whole object, so this guards a shape
            # of call that does not exist yet; the cost of being wrong about that is not symmetric.
            lib_patch = patch["ext"].get("library")
            if isinstance(lib_patch, dict) and isinstance(cur_ext.get("library"), dict):
                merged["library"] = {**cur_ext["library"], **lib_patch}
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
        # the outer query cannot ORDER BY rowid directly: in newer SQLite (>= 3.51) the rowid of a
# subquery is not visible to the outer query and it reports "no such column: rowid".
        # so it is selected as _rid inside the subquery, the outer query sorts by _rid, and _rid is
# dropped afterwards.
        rows = self._q(
            "SELECT * FROM (SELECT *, rowid AS _rid FROM messages WHERE group_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?) "
            "ORDER BY created_at, _rid",
            (gid, limit),
        )
        for r in rows:
            r.pop("_rid", None)
            r["meta"] = json.loads(r["meta"])
        return rows

    def has_later_user_message(self, gid: str, mid: str) -> bool:
        """Is there a user message in this group after `mid`?

        A round of collaboration stores its user message and broadcasts it *before* it takes the
        group lock, so that the sender sees their own message immediately. If someone sends another
        one while it waits, the later round reads both (the history is the whole group) and answers
        both — so this round must stand down rather than answer the same question twice. Ids are
        opaque, hence the rowid comparison.
        """
        row = self._one("SELECT rowid AS rid FROM messages WHERE id=?", (mid,))
        if row is None:
            return False
        return self._one(
            "SELECT 1 AS x FROM messages WHERE group_id=? AND sender_type='user' AND rowid>? LIMIT 1",
            (gid, row["rid"]),
        ) is not None

    def clear_messages(self, gid: str) -> None:
        self._x("DELETE FROM messages WHERE group_id=?", (gid,))

    def clear_all_messages(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self._db.execute("DELETE FROM messages")
            self._db.commit()
        return n

    # ------------------------------------------------------------------ attachments
    def add_attachment(self, gid: str, aid: str, name: str, mime: str, nbytes: int) -> dict:
        self._x(
            "INSERT INTO attachments(id,group_id,name,mime,bytes,created_at) VALUES(?,?,?,?,?,?)",
            (aid, gid, name, mime, nbytes, time.time()),
        )
        return self.get_attachment(aid)  # type: ignore[return-value]

    def get_attachment(self, aid: str) -> dict | None:
        return self._one("SELECT * FROM attachments WHERE id=?", (aid,))

    def delete_attachment(self, aid: str) -> None:
        self._x("DELETE FROM attachments WHERE id=?", (aid,))

    def used_attachment_ids(self) -> set[str]:
        """Ids referenced by a message. Only messages that carry images are scanned."""
        used: set[str] = set()
        for r in self._q("SELECT meta FROM messages WHERE meta LIKE '%\"images\"%'"):
            try:
                used |= {str(i["id"]) for i in (json.loads(r["meta"]).get("images") or []) if isinstance(i, dict) and i.get("id")}
            except (TypeError, ValueError):
                continue
        return used

    def stale_attachments(self, older_than: float) -> list[dict]:
        """Attachments old enough to sweep and not referenced by any message.

        An image the user uploaded but never sent would otherwise sit in the data directory
        for good. One that a message does reference is kept, however old the message is —
        the thumbnail in the transcript has to keep working.
        """
        used = self.used_attachment_ids()
        return [r for r in self._q("SELECT * FROM attachments WHERE created_at<?", (older_than,)) if r["id"] not in used]

    def agent_message_rows(self, since: float = 0) -> list[dict]:
        """For statistics: the model, fallback and elapsed-time info of every agent message."""
        rows = self._q(
            "SELECT model_id, fallback_from, meta, created_at FROM messages "
            "WHERE sender_type='agent' AND created_at>=? ORDER BY created_at",
            (since,),
        )
        for r in rows:
            r["meta"] = json.loads(r["meta"])
        return rows

    def backup_to(self, dest: Path | str, include_keys: bool = False) -> None:
        """Use SQLite's online backup API to produce a consistent snapshot; API keys are stripped
by default."""
        dest = Path(dest)
        with self._lock:
            out = sqlite3.connect(dest)
            try:
                self._db.backup(out)
                if not include_keys:
                    out.execute("UPDATE providers SET api_key=''")
                    out.execute("UPDATE mcp_servers SET env='{}', headers='{}'")  # env/headers often carry all kinds of secrets
                    for mid, url, args in out.execute("SELECT id, url, args FROM mcp_servers").fetchall():
                        out.execute("UPDATE mcp_servers SET url=?, args=? WHERE id=?",
                                    (url.split("?", 1)[0], json.dumps(_mask_args(json.loads(args or "[]")), ensure_ascii=False), mid))
                    out.execute("UPDATE settings SET value='\"\"' WHERE key='github_token'")
                    out.commit()
                else:
                    # the real keys live in the system keychain and do not travel with the .db file; they are
# written back explicitly when exporting a "with keys" backup
                    for p in self.list_providers():
                        if p["api_key"]:
                            out.execute("UPDATE providers SET api_key=? WHERE id=?", (p["api_key"], p["id"]))
                    out.execute("UPDATE settings SET value=? WHERE key='github_token'",
                                (json.dumps(self.get_settings().get("github_token") or ""),))
                    out.commit()
                out.execute("VACUUM")
            finally:
                out.close()

    RESTORE_TABLES = {"providers", "models", "agents", "groups", "messages", "settings"}

    def _check_backup(self, work: Path) -> None:
        """On a temporary copy, bring the backup "up to the shape of the current version and check it
        through"; any step that fails raises ValueError — that way a bad file is rejected before
        it ever touches the real database."""
        try:
            con = sqlite3.connect(work)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError(i18n.pick_now("The file is damaged", "文件已损坏"))
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not self.RESTORE_TABLES <= tables:
                    raise ValueError(i18n.pick_now("Required tables are missing", "缺少必要的数据表"))
                con.executescript(SCHEMA)
                con.executescript(SCHEMA_EXT)
                for _k, v in con.execute("SELECT key, value FROM settings").fetchall():
                    json.loads(v)
                con.commit()
            finally:
                con.close()
        except sqlite3.DatabaseError:
            raise ValueError(i18n.pick_now(
                "This is not a valid backup (not a Team Agent SQLite database, or an unsupported version)",
                "这不是有效的备份文件(不是 Team Agent 的 SQLite 数据库,或者版本不兼容)")) from None
        except ValueError as e:
            raise ValueError(i18n.pick_now(
                f"This backup cannot be used: {e}",
                f"这不是可用的 Team Agent 备份:{e}")) from None

    def restore_from(self, src: Path | str) -> dict:
        """Replace all current data with a backup file. A copy of the current data is kept first
        (backups/pre-restore-*.db). When the backup is the "without keys" flavour, the existing
        API keys / MCP secrets / GitHub token are preserved and not cleared. The Obsidian folder
        and sync mappings recorded in the backup are not carried over (paths from that machine
        may not exist here, and syncing blindly could delete memories)."""
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
            # the main database is WAL now; the temporary copy still uses the delete journal, but
# -wal/-shm are cleaned up too so a future change cannot miss a file
            for f in (work, *[Path(str(work) + s) for s in ("-journal", "-wal", "-shm")]):
                f.unlink(missing_ok=True)
        self._migrate()
        for p in self.list_providers():
            if not p["api_key"] and old_keys.get(p["id"]):
                # go through update_provider so the key goes to the keychain instead of being written back
# to the database in plaintext
                self.update_provider(p["id"], {"api_key": old_keys[p["id"]]})
        for m in self.list_mcp():
            env, headers = old_mcp.get(m["id"], ({}, {}))
            if not m["env"] and not m["headers"] and (env or headers):
                self._x("UPDATE mcp_servers SET env=?, headers=? WHERE id=?", (json.dumps(env), json.dumps(headers), m["id"]))
        if old_gh and not self.get_settings().get("github_token"):
            self.update_settings({"github_token": old_gh})
        self.clear_obsidian_map()
        self.clear_health(everything=True)
        self.update_settings({"obsidian_dir": "", "obsidian_auto": False})
        self._move_keys_to_keychain()      # if the backup carries plaintext keys, move them into the keychain as well after the restore
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
