"""记忆 ⇄ Obsidian:把记忆双向同步到你 Obsidian 库里的一个文件夹。

- 每条记忆是一个 .md 文件,开头的 frontmatter 记着它属于谁(ta_id / scope / kind / pinned),正文就是记忆内容。
- 你在 Obsidian 里改文字、改属性、新建笔记(没有 frontmatter 的笔记会被当作新记忆导入)、删除文件,
  下次同步都会反映到程序里;在程序里的改动也会写回文件。本地数据库始终是完整的一份,同步只是镜像。
- 两边都改了同一条:以较新的一方为准,被覆盖的那一版存到 `_冲突备份/`。
- 程序里删除的记忆,对应文件移到 `_已删除/`(不会真删你的文件);Obsidian 里删除的文件,对应记忆会被删除,
  但如果一次要删掉一大半(多半是文件夹被移走/盘没挂载),会拒绝执行并提示。
- 只读写你选定的文件夹;以 `_` 或 `.` 开头的子文件夹(如 .obsidian、_冲突备份)不参与同步;不跟随符号链接。
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

SYNC_KINDS = ("preference", "fact", "decision", "lesson")   # 「过往做法」是程序自己记的流水账,不同步
# File-name prefixes and note headings. Kept as pairs (rather than calling pick_now here)
# because a module-level call would be evaluated once at import and freeze the language.
KIND_LABEL = {"preference": ("Preference", "偏好"), "fact": ("Fact", "事实"),
              "decision": ("Decision", "决定"), "lesson": ("Lesson", "教训")}


def kind_label(kind: str) -> str:
    pair = KIND_LABEL.get(kind)
    return i18n.pick_now(*pair) if pair else kind
OUR_KEYS = ("ta_id", "scope", "scope_id", "scope_name", "kind", "pinned", "source", "updated")
MAX_FILES, MAX_BYTES, MAX_DEPTH, MAX_CONTENT = 5000, 256 * 1024, 4, 500   # 500 字:和「记忆」页手动添加的上限一致
MASS_DELETE_MIN = 5


class ObsidianError(Exception):
    pass


# ------------------------------------------------------------------ 解析 / 生成
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
    """→ (我们认得的属性, frontmatter 里的其它行(原样保留), 正文)。"""
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
    return _BAD.sub("", s).strip().strip(".").lstrip("_. ")[:n] or i18n.pick_now("Untitled", "未命名")   # 开头不能是 _ 或 .,否则同步会当成隐藏文件夹跳过


@dataclass
class Report:
    ok: bool = True
    at: float = 0.0
    written: int = 0        # 程序 → Obsidian:新建/更新的文件数
    pulled: int = 0         # Obsidian → 程序:更新的记忆数
    imported: int = 0       # Obsidian → 程序:新导入的记忆数
    deleted_memories: int = 0
    removed_files: int = 0  # 移到 _已删除 的文件数
    conflicts: int = 0
    files: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

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
        self._lock = threading.Lock()   # 自动同步和手动同步不能同时跑,否则同一批新笔记会被导入两次

    # ----------------------------------------------------------------- 配置
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
        """读 Obsidian 自己的配置文件,列出本机已有的库(只读)。"""
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

    # ----------------------------------------------------------------- 扫描
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

    # ----------------------------------------------------------------- 写文件
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
        """文件里写的归属 → (scope, scope_id);对不上就退回记忆原来的(新导入的退回全局)。"""
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
        """把聊天记录等导出物写进库里的 `_聊天记录/`(下划线开头,不参与记忆同步)。"""
        base = self.base()
        if not base or not base.is_dir():
            raise ObsidianError(i18n.pick_now("No Obsidian folder is set, or the folder is not available right now", "还没有设置 Obsidian 文件夹,或文件夹现在不可用"))
        p = base.resolve() / i18n.pick_now("_chat-log", "_聊天记录") / f"{_safe(name, 60)}.md"
        self._write(base, p, text)
        return p

    # ----------------------------------------------------------------- 同步
    def sync(self, force: bool = False) -> dict:
        rep = Report(at=time.time())
        if not self._lock.acquire(blocking=False):
            rep.ok, rep.error = False, "已经有一次同步正在进行,稍后再试"
            return rep.to_dict()
        try:
            self._sync(rep, force)
        except ObsidianError as e:
            rep.ok, rep.error = False, str(e)
        except OSError as e:
            rep.ok, rep.error = False, i18n.pick_now(f"Error reading or writing the folder: {e}", f"读写文件夹出错:{e}")
        except Exception as e:  # noqa: BLE001 — 任何意外都要变成一条可读的报告,不能让接口 500、让自动同步停掉
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
            except ObsidianError as e:   # 比如库里某个文件夹是指向别处的符号链接:只跳过这一条,不影响其它
                rep.warnings.append(str(e))
                return
            st.set_obsidian_map(m["id"], str(path.resolve().relative_to(base.resolve())), _mem_hash(m))
            rep.written += 1

        for nf in files:
            tid = str(nf.ours.get("ta_id") or "")
            if tid and tid in maps and tid not in mems:
                # 程序里已经删掉了这条:文件挪进 _已删除,不真删
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
                st.set_obsidian_map(mid, nf.rel, base_hash or d_hash)   # 文件在 Obsidian 里被移动/改名
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

        # Obsidian 里被删掉/移走的文件
        # (文件读不了、太深、被截断等情况下,原路径上文件还在,就不能算「被删了」)
        root = base.resolve()
        gone = [mid for mid in maps if mid not in seen and mid in mems
                and not any(f.ours.get("ta_id") == mid for f in files) and not (root / maps[mid]["rel_path"]).exists()]
        stale = [mid for mid in maps if mid not in mems and mid not in seen]
        for mid in stale:
            st.del_obsidian_map(mid)
        live = sum(1 for mid in maps if mid in mems)
        if gone and not force and (
            len(gone) > max(MASS_DELETE_MIN, live // 2)      # 一次删掉一大半
            or (len(gone) >= 2 and len(gone) >= live)         # 全部都不见了(空文件夹 / 没挂载)
            or not files                                      # 文件夹里一篇笔记都没有了
        ):
            rep.warnings.append(i18n.pick_now(f"{len(gone)} of the matching files are missing (almost all of them); the folder was probably moved, emptied or unmounted. Nothing was deleted, for safety. Once you have checked, click Force sync.", f"有 {len(gone)} 个对应的文件不见了(几乎是全部),很可能是文件夹被移走、清空或没挂载,为安全起见没有删除任何记忆。确认无误可点「强制同步」。"))
            gone = []
        for mid in gone:
            m = mems[mid]
            try:   # 删记忆之前把内容留一份在 _已删除,在 Obsidian 里误删/误移走也找得回来
                self._stash(base, i18n.pick_now("_deleted", "_已删除"), Path(maps[mid]["rel_path"]).name, render_note(m, self._scope_name(m, gn, an)))
            except (ObsidianError, OSError):
                pass
            st.delete_memory(mid)
            st.del_obsidian_map(mid)
            rep.deleted_memories += 1
        # 程序里新增的记忆 → 新文件
        for mid, m in mems.items():
            if mid in seen or mid in maps:
                continue
            push(m, self._new_path(base, m, gn, an))

    def _import(self, base: Path, nf: NoteFile, gn: dict, an: dict, rep: Report, seen: set[str]) -> None:
        """Obsidian 里新建的笔记(或从别处拷来的带 ta_id 的文件) → 新记忆。"""
        if not nf.body:
            return
        st = self.store
        if nf.too_long:   # 长文章不是「一句话记忆」:不截断、不改写,原样留着
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
