"""Memory <-> Obsidian: two-way sync of memories into one folder of your Obsidian vault.

- Each memory is a .md file; the frontmatter at the top records who it belongs to
  (ta_id / scope / kind / pinned) and the body is the memory text.
- Text and property edits, new notes (notes without frontmatter are imported as new
  memories) and deleted files in Obsidian are all reflected into the app on the next sync,
  and changes made in the app are written back to the files. The local database always
  holds the complete copy; syncing is only a mirror.
- When both sides changed the same entry the newer one wins, and the overwritten version
  is stored under `_冲突备份/`.
- A memory deleted in the app has its file moved to `_已删除/` (your file is never really
  deleted); a file deleted in Obsidian deletes the matching memory, but if one sync would
  delete more than half of them (usually the folder was moved away or the disk is not
  mounted) it refuses and reports it.
- Only the folder you picked is read and written; subfolders starting with `_` or `.`
  (such as .obsidian or _冲突备份) are not synced; symlinks are not followed.
"""

from __future__ import annotations

from . import i18n

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .memory import looks_sensitive
from .store import Store

SYNC_KINDS = ("preference", "fact", "decision", "lesson")   # the activity log is the program's own running record and is not synced
# File-name prefixes and note headings. Kept as pairs (rather than calling pick_now here)
# because a module-level call would be evaluated once at import and freeze the language.
KIND_LABEL = {"preference": ("Preference", "偏好"), "fact": ("Fact", "事实"),
              "decision": ("Decision", "决定"), "lesson": ("Lesson", "教训")}


def kind_label(kind: str) -> str:
    pair = KIND_LABEL.get(kind)
    return i18n.pick_now(*pair) if pair else kind
OUR_KEYS = ("ta_id", "scope", "scope_id", "scope_name", "kind", "pinned", "source", "updated")
MAX_FILES, MAX_BYTES, MAX_DEPTH, MAX_CONTENT = 5000, 256 * 1024, 4, 500   # 500 characters: same limit as manual entries on the Memory page
MASS_DELETE_MIN = 5


class ObsidianError(Exception):
    pass


# ------------------------------------------------------------------ parse / render
def _val(raw: str):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw.strip("'")


def parse_note(text: str) -> tuple[dict, list[str], str]:
    """-> (the properties we recognize, the other lines of the frontmatter (kept as-is), the body)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, [], text.strip()
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, [], text.strip()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, [], text.strip()
    ours: dict = {}
    other: list[str] = []
    for ln in lines[1:end]:
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
        if m and m.group(1) in OUR_KEYS:
            ours[m.group(1)] = _val(m.group(2))
        else:
            other.append(ln)
    return ours, other, "\n".join(lines[end + 1:]).strip()


def render_note(mem: dict, scope_name: str, other: list[str] | None = None) -> str:
    fm = [
        f"ta_id: {mem['id']}",
        f"scope: {mem['scope']}",
        f"scope_id: {json.dumps(mem['scope_id'], ensure_ascii=False)}",
        f"scope_name: {json.dumps(scope_name, ensure_ascii=False)}",
        f"kind: {mem['kind']}",
        f"pinned: {'true' if mem['pinned'] else 'false'}",
        f"source: {mem['source']}",
        f"updated: {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(mem['updated_at']))}",
        *(other or []),
    ]
    return "---\n" + "\n".join(fm) + "\n---\n" + mem["content"].strip() + "\n"


def state_hash(scope: str, scope_id: str, kind: str, pinned: bool, content: str) -> str:
    return hashlib.sha1(json.dumps([scope, scope_id, kind, bool(pinned), content.strip()], ensure_ascii=False).encode()).hexdigest()


def _mem_hash(m: dict) -> str:
    return state_hash(m["scope"], m["scope_id"], m["kind"], m["pinned"], m["content"])


_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\r\n\t]')


def _safe(s: str, n: int = 40) -> str:
    return _BAD.sub("", s).strip().strip(".").lstrip("_. ")[:n] or i18n.pick_now("Untitled", "未命名")   # must not start with _ or ., otherwise sync treats it as a hidden folder and skips it


@dataclass
class Report:
    ok: bool = True
    at: float = 0.0
    written: int = 0        # app -> Obsidian: number of files created or updated
    pulled: int = 0         # Obsidian -> app: number of memories updated
    imported: int = 0       # Obsidian -> app: number of memories newly imported
    deleted_memories: int = 0
    removed_files: int = 0  # number of files moved to _已删除
    conflicts: int = 0
    files: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    # Almost every mapped file vanished at once, so the deletion was held back. A flag rather
    # than a phrase the client has to match on: the warning text follows the request language.
    mass_missing: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class NoteFile:
    path: Path
    rel: str
    mtime: float
    ours: dict
    other: list[str]
    body: str
    raw: str

    @property
    def too_long(self) -> bool:
        return len(self.body) > MAX_CONTENT


class ObsidianSync:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()   # auto sync and manual sync must not run at the same time, otherwise the same batch of
# new notes is imported twice

    # ----------------------------------------------------------------- config
    def base(self) -> Path | None:
        d = (self.store.get_settings().get("obsidian_dir") or "").strip()
        return Path(d).expanduser() if d else None

    @staticmethod
    def validate_dir(raw: str, data_dir: Path) -> Path:
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute():
            raise ObsidianError(i18n.pick_now("Enter the full folder path", "请填写完整的文件夹路径"))
        p = p.resolve()
        if p == Path(p.anchor) or p == Path.home().resolve() or p.is_relative_to(data_dir.resolve()) or data_dir.resolve().is_relative_to(p):
            raise ObsidianError(i18n.pick_now("Choose a dedicated folder inside your vault — not the filesystem root, your home directory, or this app's data directory", "请选择库里的一个专用文件夹,不要选磁盘根目录、用户主目录,或本程序的数据目录"))
        if p.exists() and not p.is_dir():
            raise ObsidianError(i18n.pick_now("That path is a file, not a folder", "这个路径是文件,不是文件夹"))
        return p

    @staticmethod
    def in_vault(p: Path) -> bool:
        return any((a / ".obsidian").is_dir() for a in [p, *p.parents])

    @staticmethod
    def detect_vaults() -> list[dict]:
        """Read Obsidian's own config file and list the vaults that exist on this machine (read-only)."""
        home = Path.home()
        cands = [home / "Library/Application Support/obsidian/obsidian.json",
                 home / ".config/obsidian/obsidian.json",
                 Path(os.environ.get("APPDATA", str(home / "AppData/Roaming"))) / "obsidian/obsidian.json"]
        out: list[dict] = []
        for c in cands:
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for v in (data.get("vaults") or {}).values():
                p = v.get("path") if isinstance(v, dict) else None
                if isinstance(p, str) and Path(p).is_dir() and p not in [o["path"] for o in out]:
                    out.append({"path": p, "name": Path(p).name})
        return out

    def status(self) -> dict:
        base = self.base()
        exists = bool(base and base.is_dir())
        notes = 0
        if exists:
            try:
                notes = sum(1 for _ in self._iter_md(base))  # type: ignore[arg-type]
            except OSError:
                notes = 0
        last = self.store.get_meta("obsidian_last")
        return {
            "dir": str(base) if base else "", "auto": bool(self.store.get_settings().get("obsidian_auto")),
            "exists": exists, "in_vault": bool(base and self.in_vault(base)), "notes": notes,
            "mapped": len(self.store.obsidian_map()), "last": json.loads(last) if last else None,
        }

    # ----------------------------------------------------------------- scan
    def _iter_md(self, base: Path):
        root = base.resolve()
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.is_symlink() or e.name.startswith((".", "_")):
                    continue
                if e.is_dir():
                    if depth + 1 <= MAX_DEPTH:
                        stack.append((e, depth + 1))
                elif e.suffix.lower() == ".md":
                    yield e

    def _scan(self, base: Path, rep: Report) -> list[NoteFile]:
        root = base.resolve()
        out: list[NoteFile] = []
        for p in self._iter_md(base):
            if len(out) >= MAX_FILES:
                rep.warnings.append(i18n.pick_now(f"Too many files; only the first {MAX_FILES} were processed", f"文件太多,只处理前 {MAX_FILES} 个"))
                break
            try:
                p.relative_to(root).as_posix().encode("utf-8")
            except (UnicodeEncodeError, ValueError):
                rep.warnings.append(i18n.pick_now(f"Skipped a file whose name could not be decoded: {p.name.encode('utf-8', 'replace').decode()}", f"跳过文件名无法识别的文件:{p.name.encode('utf-8', 'replace').decode()}"))
                continue
            try:
                st = p.stat()
                if st.st_size > MAX_BYTES:
                    rep.warnings.append(i18n.pick_now(f"Skipped an oversized file: {p.name}", f"跳过过大的文件:{p.name}"))
                    continue
                raw = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                rep.warnings.append(i18n.pick_now(f"Could not read it (not UTF-8 text?): {p.name}", f"读不了(不是 UTF-8 文本?):{p.name}"))
                continue
            ours, other, body = parse_note(raw)
            out.append(NoteFile(p, str(p.relative_to(root)), st.st_mtime, ours, other, body, raw))
        rep.files = len(out)
        return out

    # ----------------------------------------------------------------- write files
    def _inside(self, base: Path, p: Path) -> Path:
        root = base.resolve()
        rp = p.resolve() if p.exists() else p.parent.resolve() / p.name
        if not rp.is_relative_to(root):
            raise ObsidianError(i18n.pick_now(f"Path outside the chosen folder: {p}", f"路径越界:{p}"))
        return rp

    def _write(self, base: Path, p: Path, text: str) -> None:
        p = self._inside(base, p)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _stash(self, base: Path, sub: str, name: str, text: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._write(base, base.resolve() / sub / f"{Path(name).stem}-{stamp}.md", text)

    def _names(self) -> tuple[dict[str, str], dict[str, str]]:
        return ({g["id"]: g["name"] for g in self.store.list_groups()}, {a["id"]: a["name"] for a in self.store.list_agents()})

    def _scope_name(self, m: dict, gn: dict, an: dict) -> str:
        return {"global": i18n.pick_now("Global", "全局"), "group": gn.get(m["scope_id"], ""), "agent": an.get(m["scope_id"], "")}[m["scope"]]

    def _new_path(self, base: Path, m: dict, gn: dict, an: dict) -> Path:
        folder = {"global": i18n.pick_now("Global", "全局"), "group": i18n.pick_now(f"Groups/{_safe(gn.get(m['scope_id'], 'unknown group'))}", f"群聊/{_safe(gn.get(m['scope_id'], '未知群'))}"),
                  "agent": i18n.pick_now(f"Members/{_safe(an.get(m['scope_id'], 'unknown member'))}", f"成员/{_safe(an.get(m['scope_id'], '未知成员'))}")}[m["scope"]]
        name = f"{kind_label(m['kind'])}-{_safe(m['content'], 24)}-{m['id'][:6]}.md"
        return base.resolve() / folder / name

    def _resolve_scope(self, ours: dict, m: dict | None, gn: dict, an: dict, rep: Report, label: str) -> tuple[str, str]:
        """Ownership written in the file -> (scope, scope_id); falls back to the memory's current
one when it does not match (newly imported ones fall back to global)."""
        scope = ours.get("scope") if ours.get("scope") in ("global", "group", "agent") else (m["scope"] if m else "global")
        if scope == "global":
            return "global", ""
        names = gn if scope == "group" else an
        sid = str(ours.get("scope_id") or "")
        if sid in names:
            return scope, sid
        nm = str(ours.get("scope_name") or "")
        hit = next((k for k, v in names.items() if v == nm and nm), None)
        if hit:
            return scope, hit
        if m and m["scope"] == scope:
            return scope, m["scope_id"]
        rep.warnings.append(i18n.pick_now(f"The {'group' if scope == 'group' else 'member'} named in \"{label}\" was not found, so this became a global memory", f"「{label}」里写的{'群聊' if scope == 'group' else '成员'}找不到,已改为全局记忆"))
        return "global", ""

    def write_export(self, name: str, text: str) -> Path:
        """Write exports such as chat logs into `_聊天记录/` in the vault (leading underscore, so
it takes no part in memory sync)."""
        base = self.base()
        if not base or not base.is_dir():
            raise ObsidianError(i18n.pick_now("No Obsidian folder is set, or the folder is not available right now", "还没有设置 Obsidian 文件夹,或文件夹现在不可用"))
        p = base.resolve() / i18n.pick_now("_chat-log", "_聊天记录") / f"{_safe(name, 60)}.md"
        self._write(base, p, text)
        return p

    # ----------------------------------------------------------------- sync
    def sync(self, force: bool = False) -> dict:
        rep = Report(at=time.time())
        if not self._lock.acquire(blocking=False):
            rep.ok, rep.error = False, i18n.pick_now("A sync is already running — try again in a moment", "已经有一次同步正在进行,稍后再试")
            return rep.to_dict()
        try:
            self._sync(rep, force)
        except ObsidianError as e:
            rep.ok, rep.error = False, str(e)
        except OSError as e:
            rep.ok, rep.error = False, i18n.pick_now(f"Error reading or writing the folder: {e}", f"读写文件夹出错:{e}")
        except Exception as e:  # noqa: BLE001 — any surprise must become a readable report; it must not make the
# endpoint return 500 or stop the automatic sync
            rep.ok, rep.error = False, i18n.pick_now(f"Sync failed: {type(e).__name__}: {e}", f"同步出错:{type(e).__name__}: {e}")
        finally:
            self._lock.release()
        self.store.set_meta("obsidian_last", json.dumps(rep.to_dict(), ensure_ascii=False))
        return rep.to_dict()

    def _sync(self, rep: Report, force: bool) -> None:
        base = self.base()
        if not base:
            raise ObsidianError(i18n.pick_now("No Obsidian folder has been chosen yet", "还没有选择 Obsidian 文件夹"))
        if not base.is_dir():
            raise ObsidianError(i18n.pick_now(f"The folder does not exist, or is not available right now: {base} (if it is an external drive, mount it first; nothing was changed)", f"文件夹不存在或暂时不可用:{base}(如果是外接盘,请先挂载;没有做任何改动)"))
        st = self.store
        gn, an = self._names()
        mems = {m["id"]: m for m in st.list_memories(limit=1_000_000) if m["kind"] in SYNC_KINDS}
        maps = st.obsidian_map()
        files = self._scan(base, rep)
        by_path = {v["rel_path"]: k for k, v in maps.items()}
        seen: set[str] = set()

        def push(m: dict, path: Path, other: list[str] | None = None) -> None:
            try:
                self._write(base, path, render_note(m, self._scope_name(m, gn, an), other))
            except ObsidianError as e:   # e.g. a folder in the vault that is a symlink to somewhere else: skip only this one,
# do not affect the rest
                rep.warnings.append(str(e))
                return
            st.set_obsidian_map(m["id"], str(path.resolve().relative_to(base.resolve())), _mem_hash(m))
            rep.written += 1

        for nf in files:
            tid = str(nf.ours.get("ta_id") or "")
            if tid and tid in maps and tid not in mems:
                # already deleted in the app: move the file into _已删除, never really delete it
                self._stash(base, i18n.pick_now("_deleted", "_已删除"), nf.path.name, nf.raw)
                nf.path.unlink()
                st.del_obsidian_map(tid)
                rep.removed_files += 1
                continue
            mid = tid if tid in mems else by_path.get(nf.rel)
            if mid not in mems:
                self._import(base, nf, gn, an, rep, seen)
                continue
            if mid in seen:
                rep.warnings.append(i18n.pick_now(f"Two files map to the same memory; ignoring the second: {nf.rel}", f"两个文件对应同一条记忆,忽略后一个:{nf.rel}"))
                continue
            seen.add(mid)
            m = mems[mid]
            if nf.too_long:
                rep.warnings.append(i18n.pick_now(f"\"{nf.rel}\" is longer than {MAX_CONTENT} characters, so it does not work as a memory and was not synced (the file was left alone)", f"「{nf.rel}」超过 {MAX_CONTENT} 字,不适合当记忆,这一条没有同步(文件没有动)"))
                continue
            scope, sid = self._resolve_scope(nf.ours, m, gn, an, rep, nf.rel)
            kind = nf.ours.get("kind") if nf.ours.get("kind") in SYNC_KINDS else m["kind"]
            pinned = nf.ours["pinned"] if isinstance(nf.ours.get("pinned"), bool) else m["pinned"]
            content = nf.body if nf.body else m["content"]
            f_hash, d_hash = state_hash(scope, sid, kind, pinned, content), _mem_hash(m)
            base_hash = (maps.get(mid) or {}).get("hash")
            if maps.get(mid) and maps[mid]["rel_path"] != nf.rel:
                st.set_obsidian_map(mid, nf.rel, base_hash or d_hash)   # file was moved or renamed in Obsidian
            if f_hash == d_hash:
                st.set_obsidian_map(mid, nf.rel, d_hash)
                if not nf.ours.get("ta_id"):
                    push(m, nf.path, nf.other)
                continue
            f_changed, d_changed = base_hash is None or f_hash != base_hash, base_hash is None or d_hash != base_hash
            if f_changed and d_changed:
                rep.conflicts += 1
                if nf.mtime >= m["updated_at"]:
                    self._stash(base, i18n.pick_now("_conflict-backup", "_冲突备份"), nf.path.name, render_note(m, self._scope_name(m, gn, an)))
                    f_changed, d_changed = True, False
                else:
                    self._stash(base, i18n.pick_now("_conflict-backup", "_冲突备份"), nf.path.name, nf.raw)
                    f_changed, d_changed = False, True
            if f_changed:
                st.update_memory(mid, {"content": content, "kind": kind, "pinned": pinned, "scope": scope, "scope_id": sid})
                m2 = st.get_memory(mid)
                assert m2
                st.set_obsidian_map(mid, nf.rel, _mem_hash(m2))
                rep.pulled += 1
                if not nf.ours.get("ta_id"):
                    push(m2, nf.path, nf.other)
            else:
                push(m, nf.path, nf.other)

        # files deleted or moved away in Obsidian
        # (if the file cannot be read, is nested too deep, was truncated, etc., the file is
# still at its original path, so it does not count as "deleted")
        root = base.resolve()
        gone = [mid for mid in maps if mid not in seen and mid in mems
                and not any(f.ours.get("ta_id") == mid for f in files) and not (root / maps[mid]["rel_path"]).exists()]
        stale = [mid for mid in maps if mid not in mems and mid not in seen]
        for mid in stale:
            st.del_obsidian_map(mid)
        live = sum(1 for mid in maps if mid in mems)
        if gone and not force and (
            len(gone) > max(MASS_DELETE_MIN, live // 2)      # more than half would be deleted at once
            or (len(gone) >= 2 and len(gone) >= live)         # everything is gone (empty folder / not mounted)
            or not files                                      # the folder has no notes left at all
        ):
            rep.mass_missing = True
            rep.warnings.append(i18n.pick_now(f"{len(gone)} of the matching files are missing (almost all of them); the folder was probably moved, emptied or unmounted. Nothing was deleted, for safety. Once you have checked, click Force sync.", f"有 {len(gone)} 个对应的文件不见了(几乎是全部),很可能是文件夹被移走、清空或没挂载,为安全起见没有删除任何记忆。确认无误可点「强制同步」。"))
            gone = []
        for mid in gone:
            m = mems[mid]
            try:   # keep a copy of the content in _已删除 before deleting the memory, so an accidental
# delete or move in Obsidian can still be recovered
                self._stash(base, i18n.pick_now("_deleted", "_已删除"), Path(maps[mid]["rel_path"]).name, render_note(m, self._scope_name(m, gn, an)))
            except (ObsidianError, OSError) as e:
                # The file is already gone from the vault, so that copy is the only place this memory
                # would still exist: failing to make it is a reason to keep the memory, not to delete it.
                rep.warnings.append(i18n.pick_now(f"Could not keep a copy of {maps[mid]['rel_path']} before deleting it, so the memory was left alone: {e}", f"删除前没能为 {maps[mid]['rel_path']} 留下副本,所以这条记忆没有删除:{e}"))
                continue
            st.delete_memory(mid)
            st.del_obsidian_map(mid)
            rep.deleted_memories += 1
        # memories added in the app -> new files
        for mid, m in mems.items():
            if mid in seen or mid in maps:
                continue
            push(m, self._new_path(base, m, gn, an))

    def _import(self, base: Path, nf: NoteFile, gn: dict, an: dict, rep: Report, seen: set[str]) -> None:
        """Notes created in Obsidian (or files copied in from elsewhere that carry a ta_id)
-> new memories."""
        if not nf.body:
            return
        st = self.store
        if nf.too_long:   # a long article is not a "one-line memory": do not truncate or rewrite it, keep it as-is
            rep.warnings.append(i18n.pick_now(f"\"{nf.rel}\" is longer than {MAX_CONTENT} characters, so it does not work as a memory and was skipped (the file was left alone). Write it as a sentence or two to use it as one.", f"「{nf.rel}」超过 {MAX_CONTENT} 字,不适合当记忆,已跳过(文件没有动)。想当记忆用,请写成一两句话"))
            return
        if looks_sensitive(nf.body):
            rep.warnings.append(i18n.pick_now(f"\"{nf.rel}\" looks like it contains a key, a password or a long digit string, so it was not imported (the file was left alone)", f"「{nf.rel}」看起来含密钥/密码/长数字串,出于安全没有导入(文件没有动)"))
            return
        scope, sid = self._resolve_scope(nf.ours, None, gn, an, rep, nf.rel)
        kind = nf.ours.get("kind") if nf.ours.get("kind") in SYNC_KINDS else "fact"
        content = nf.body
        m = st.add_memory(content, scope, sid, kind, "obsidian", bool(nf.ours.get("pinned") is True))
        if m["id"] in seen or m["id"] in st.obsidian_map():
            rep.warnings.append(i18n.pick_now(f"\"{nf.rel}\" has the same content as an existing memory, so it was not imported again", f"「{nf.rel}」和已有的一条记忆内容相同,没有重复导入"))
            return
        seen.add(m["id"])
        rep.imported += 1
        self._write(base, nf.path, render_note(m, self._scope_name(m, gn, an), nf.other))
        st.set_obsidian_map(m["id"], nf.rel, _mem_hash(m))
