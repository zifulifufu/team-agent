#!/usr/bin/env python3
"""把器械参考库同步进某个群的工作目录，并留下一份成员能读的索引。

为什么必须**拷**，而不是链接：

* 成员能读到的路径只有**自己工作目录里真实存在的文件**。`assemble._local` 把路径
  `resolve()` 之后再用 `coderun.inside()` 比，软链接一律 resolve 到目录外 → 判越界；
  `toolhub._read_workspace_file` 与 `_find_in_workspace` 更是显式写了 `not is_symlink()`。
  所以「软链一份进群」这条路是**关着的**，不是没想到。
* 硬链接也不行，虽然它不占空间：硬链接与源文件是**同一个 inode**，成员一旦往那个路径
  `append`（`write_document` 就有 append 模式），改的是**中央库原件**。一份只读参考库
  被写坏，比多占几百 MB 严重得多。

于是剩下唯一安全的做法：拷贝。为了不让 380 MB × N 个群，长边超过 `--max-edge` 的图会被
缩到该边长（成员看图本来就会先被 `attachments.shrink_image` 缩，参考图不需要 8000px），
并以 `src_sha` 做增量：中央库没变、群里那份也还在 → 跳过。

  python scripts/sync-device-reference.py --to ~/.team-agent/workspaces/<gid>
  python scripts/sync-device-reference.py --to <dir> --max-edge 2400 --only 微导管
  python scripts/sync-device-reference.py --to <dir> --dry-run
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CENTRAL = Path.home() / ".team-agent" / "illustration" / "devices"
# 作者的稿子里量出来的**规范**（色板、主题）。和器械参考图并列进群：一个说"器械长什么样"，
# 一个说"作者自己那套图是怎么画的"。文件不多，整份拷，内容一样就跳过。
AUTHORED = Path.home() / ".team-agent" / "illustration" / "authored"
AUTHORED_FILES = ("规范.md", "palette.json", "subjects.json")
AUTHORED_FOLDER = "参考资料/绘图规范"
INDEX = "index.json"
FOLDER = "参考资料/器械"        # 落在工作目录里的位置
NOTE = "索引.md"               # 成员真正会读的那份
MANIFEST = ".manifest.json"    # 点开头 → 成员的工具看不见它，理由见 `_workspace_listing` 的注释
DEFAULT_EDGE = 1600
DEFAULT_QUALITY = 88


def slug(text: str, limit: int = 60) -> str:
    """A filename that survives every filesystem, while keeping Chinese legible."""
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(text or "")).strip("-._")
    return (s[:limit] or "item")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def place(item: dict) -> Path:
    """Where this product goes inside a workspace, as a relative path — **without** the suffix.

    The suffix is the encoder's decision, not the caller's (see `shrink`). Picking it here and then
    writing JPEG bytes to a `.png` name is the same lie this codebase has been bitten by before: a
    `.mp3` holding WAVE bytes that ffmpeg reads happily and nothing ever reports.
    """
    folder = slug(item.get("category") or "未分类")
    return Path(FOLDER) / folder / slug(item.get("name"))


def plan_paths(rows: list[dict]) -> dict[str, Path]:
    """`id → 落点`, with **names that collide pulled apart**.

    ⚠️ Two products can slug to the same leaf — measured: 3 groups, 6 entries (one pair differs only
    by a space after the registered-trademark sign). They then race to write the same file, so which
    one survives depends on who finished last, the recorded byte count matches nothing, and every
    re-run re-encodes the same picture. A name collision is a content collision: elsewhere in this
    codebase it produced `anim-镜2-2.mp4.mp4` and a microcatheter nobody had seen.

    The id suffix is decided from the whole set, not from run order, so the same library always
    lands on the same paths and the incremental check keeps working.
    """
    base = {str(r.get("id") or i): place(r) for i, r in enumerate(rows)}
    seen = collections.Counter(p.as_posix() for p in base.values())
    out: dict[str, Path] = {}
    for id_, p in base.items():
        out[id_] = p.with_name(f"{p.name}-{slug(id_, 24)}") if seen[p.as_posix()] > 1 else p
    return out


def suffix_for(src: Path) -> str:
    """The container this picture is actually in — anything unknown is re-encoded as JPEG."""
    s = src.suffix.lower()
    return s if s in (".png", ".jpg", ".jpeg") else ".jpg"


def has_transparency(im) -> bool:
    """Whether this image really has see-through pixels — measured, not taken from the mode.

    A PNG a catalogue uploaded is almost never actually transparent (measured on this library:
    73 of 80 sampled PNGs). Keeping it as PNG then costs 2.6× the bytes for a background nobody
    can see: 139 MB of the 184 MB this sync writes.
    """
    if im.mode in ("RGBA", "LA"):
        return im.getchannel("A").getextrema()[0] < 250
    if im.mode == "P":
        return "transparency" in im.info
    return False


def shrink(src: Path, stem: Path, edge: int, quality: int) -> tuple[Path, bool, int]:
    """`(real path, resized, bytes)`. `edge` of 0 copies as-is — no re-encode, no generation loss.

    ⚠️ The suffix is decided by **what was actually encoded**, never by what the source was called:
    JPEG bytes in a `.png` name (or the reverse) is the same lie as a `.mp3` full of WAVE bytes —
    nothing downstream can tell, so nothing ever reports it.
    """
    from PIL import Image
    with Image.open(src) as im:
        im.load()
        w, h = im.size
        keep_png = has_transparency(im)
        dst = stem.with_suffix(".png" if keep_png else ".jpg")
        dst.parent.mkdir(parents=True, exist_ok=True)
        real = src.suffix.lower()
        if not edge:
            if real == dst.suffix:
                shutil.copyfile(src, dst)
                return dst, False, dst.stat().st_size
            # Wrong container, but "do not resize" was asked for: still re-encode so the name
            # tells the truth.
            return _encode(im, dst, quality, keep_png)
        if max(w, h) <= edge and real == dst.suffix:
            # Small enough *and* already in the right container: copy the bytes rather than
            # re-encode. A reference photo must not lose a generation of detail for nothing.
            shutil.copyfile(src, dst)
            return dst, False, dst.stat().st_size
        if max(w, h) > edge:
            scale = edge / max(w, h)
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        path, _, size = _encode(im, dst, quality, keep_png)
        return path, True, size


def _encode(im, dst: Path, quality: int, png: bool) -> tuple[Path, bool, int]:
    if png:
        out = im if im.mode in ("RGBA", "L", "LA", "P") else im.convert("RGBA")
        out.save(dst, "PNG", optimize=True)
    else:
        out = im if im.mode == "RGB" else im.convert("RGB")
        out.save(dst, "JPEG", quality=quality, optimize=True)
    return dst, True, dst.stat().st_size


def write_note(rows: list[dict], manifest: dict, edge: int) -> str:
    """The index a member reads: what these pictures are, what they are **not**, and how to look.

    The two sentences that matter most are the ones about permission and about these not being
    shot material — a folder of 500 attractive device photos sitting in the workspace is otherwise
    exactly what a storyboard artist would paste straight into the film.

    ⚠️ The permission verdict is **computed from the rows**, not written here. The rule has exactly
    one home (`index.json`'s `rule`, produced by `fetch-device-images.py`); a second copy in this
    file is how the first version came to promise "unconfirmed, ask the user" for a library the user
    had never claimed — and how, in this codebase's history, three of four spellings of one rule
    ended up disagreeing in three different directions.
    """
    cats = collections.Counter(r["category"] for r in rows)
    rights = collections.Counter(str(r.get("rights") or "未标注") for r in rows)
    mine = [r for r in rows if r.get("rights") == "own" and r.get("rights_declared_by") == "user"]
    if mine and len(mine) == len(rows):
        verdict = "本目录全部由用户声明为自有素材，可用于成片。"
    elif mine:
        verdict = f"其中 {len(mine)} 张由用户声明为自有，可用于成片；**其余不可**。"
    else:
        verdict = ("**一张都不能用进对外发布的成片。** 本目录全部是第三方素材（转存自公开产品"
                   "目录，图中器械的原始作者为各厂商）。要发布就自己画——而画得像的前提，"
                   "正是先看过这里的图。")
    lines = [
        "# 器械参考图 · 内部参考",
        "",
        "**这不是分镜素材，不要把它放进片子里。** 这些是真实产品的照片，用来在动手画之前"
        "看清某类器械**实际长什么样**——微导管的头端塑形与显影环、支架是编织网还是激光切割、"
        "弹簧圈的形态家族。对照完再自己画。",
        "",
        "## 怎么用",
        "",
        "1. 先找这一类的图：`list_workspace_files(query=\"参考资料/器械/微导管\")`",
        "2. 再看：`review_picture(paths=[\"参考资料/器械/微导管/A.png\", \"…/B.png\"], "
        "question=\"这几根微导管的头端塑形和显影环各在什么位置？\")`",
        "3. 一次可以并行看多张（`paths` 传列表，比一张一张看快得多）；看的是形态，不是照抄。",
        "",
        "## 许可（**先读这段再决定能不能用**）",
        "",
        "内部参考不受限。对外发布的成片：",
        "",
        verdict,
        "",
        "权利构成：" + "、".join(f"`{k}` {v} 张" for k, v in rights.most_common()) + "。",
        "",
        manifest.get("rule", ""),
        "",
        f"（`{MANIFEST}` 记着每一张的来源 URL 与权利字段，供核对；成员工具看不到它。）",
        "",
        f"## 目录（{len(rows)} 张 / {len(cats)} 类，长边 ≤ {edge or '原尺寸'} px）",
        "",
        "| 类别 | 张数 | 目录 |",
        "| --- | --- | --- |",
    ]
    for cat, n in sorted(cats.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {cat} | {n} | `{FOLDER}/{slug(cat)}/` |")
    lines += ["", f"同步于 {manifest['synced_at']}，来源 `{manifest['from']}`。"]
    return "\n".join(lines)


def group_workspaces() -> list[tuple[str, Path]]:
    """`(群名, 工作目录)` for every group that is not archived, read from the app's own database.

    ⚠️ The path is **handed to `coderun.workspace_path`**, never assembled here. `<data dir>/
    workspaces/<gid>` looks obvious and is wrong: a group may point at a directory the user picked,
    and the rule that normalises it (macOS makes `/tmp` a symlink, and a workspace under one could
    not write a single file until that was fixed) lives in exactly one place. Re-deriving it in a
    script is how this project has repeatedly ended up with four spellings of one rule and three of
    them disagreeing.
    """
    import sqlite3
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.app import coderun, store as appstore
    data_dir = Path(appstore.default_data_dir())
    db = data_dir / "team-agent.db"
    if not db.is_file():
        print(f"没有应用数据库 {db}", file=sys.stderr)
        return []
    settings: dict = {}
    out: list[tuple[str, Path]] = []
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        try:
            for r in con.execute("SELECT key,value FROM settings"):
                try:
                    settings[r["key"]] = json.loads(r["value"])
                except ValueError:
                    pass
        except sqlite3.Error:
            pass                       # no settings table yet: the defaults are what apply
        for r in con.execute("SELECT id,name,workspace FROM groups "
                             "WHERE COALESCE(archived,0) = 0 ORDER BY name"):
            out.append((r["name"], coderun.workspace_path(data_dir, settings, r["id"],
                                                          r["workspace"] or "")))
    return out


def sync_authored(dest: Path, what: str = "resize") -> dict:
    """把作者的绘图规范拷进群。**内容一样就跳过** —— 每 6 秒轮询成果栏的那个前端，是按文件数
    判断"有没有新成果"的，一份每次都重写的规范会让它每轮都弹出来。"""
    out = {"folder": AUTHORED_FOLDER, "written": [], "skipped": [], "bytes": 0}
    if not AUTHORED.is_dir():
        out["why"] = f"还没有 {AUTHORED}（先跑 scripts/ingest-illustration.py）"
        return out
    target = dest / AUTHORED_FOLDER
    for name in AUTHORED_FILES:
        src = AUTHORED / name
        if not src.is_file():
            continue
        dst = target / name
        data = src.read_bytes()
        if dst.is_file() and dst.read_bytes() == data:
            out["skipped"].append(name)
            continue
        target.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        out["written"].append(name)
        out["bytes"] += len(data)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=str(CENTRAL))
    ap.add_argument("--to", action="append", help="群工作目录，可给多次")
    ap.add_argument("--all-groups", action="store_true",
                    help="同步到所有未归档的群（工作目录从应用库里读）")
    ap.add_argument("--only", default="", help="只同步某一个类别，例如 微导管")
    ap.add_argument("--max-edge", type=int, default=DEFAULT_EDGE, help="0 = 原尺寸直拷")
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.to and not args.all_groups:
        ap.error("给一个 --to，或用 --all-groups")

    src = Path(args.src)
    index_path = src / INDEX
    if not index_path.is_file():
        print(f"中央库没有 {index_path}，先跑 scripts/fetch-device-images.py", file=sys.stderr)
        return 2
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    rows = [r for r in (payload.get("items") or []) if Path(str(r.get("file") or "")).is_file()]
    if args.only:
        rows = [r for r in rows if r.get("category") == args.only]
    if not rows:
        print("没有可同步的条目", file=sys.stderr)
        return 2

    targets: list[tuple[str, Path]] = []
    if args.all_groups:
        found = group_workspaces()
        if not found:
            print("应用里没有可同步的群", file=sys.stderr)
        targets += found
    targets += [("", Path(t).expanduser().resolve()) for t in (args.to or [])]

    places = plan_paths(rows)
    for name, dest in targets:
        label = f"{name} " if name else ""
        if not dest.is_dir():
            print(f"跳过 {label}{dest}：不是一个已存在的目录"
                  f"（群里还没有工作目录时先在里面说一句话）", file=sys.stderr)
            continue
        print(f"→ {label}{dest}")
        if args.dry_run:
            for r in rows[:8]:
                s = Path(r["file"])
                print(f"    {(places[str(r['id'])].as_posix() + suffix_for(s))}  ← {s.name} "
                      f"({r.get('bytes', 0) // 1024} KB)")
            print(f"    共 {len(rows)} 张")
            continue
        out = sync_one(src, dest, rows, places, payload, args)
        out["authored"] = sync_authored(dest)
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def sync_one(src: Path, dest: Path, rows: list[dict], places: dict[str, Path],
             payload: dict, args) -> dict:
    manifest_path = dest / FOLDER / MANIFEST
    old: dict = {}
    if manifest_path.is_file():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8")).get("items") or {}
        except (OSError, ValueError):
            old = {}
    started = time.time()
    stats = {"added": 0, "updated": 0, "skipped": 0, "bytes": 0, "resized": 0, "failed": []}
    fresh: dict = {}

    def one(row: dict) -> tuple[str, dict]:
        s = Path(row["file"])
        id_ = str(row.get("id") or place(row).as_posix())
        stem = places[id_]
        try:
            sha = digest(s)
        except OSError as e:
            return "failed", {"id": id_, "error": f"{type(e).__name__}: {e}"}
        was = old.get(id_) or {}
        record = {"id": id_, "src_sha": sha, "src_bytes": s.stat().st_size,
                  **{k: row.get(k) for k in ("name", "category", "manufacturer", "source_url",
                                             "source_page", "rights", "publishable")}}
        # Where the last run actually put it — the record is what knows, because the suffix is the
        # encoder's choice and cannot be re-derived from the source name.
        prev = Path(str(was.get("dest"))) if was.get("dest") else None
        if (prev and was.get("src_sha") == sha and (dest / prev).is_file()
                and (dest / prev).stat().st_size == was.get("dest_bytes")):
            record.update(dest=prev.as_posix(), dest_bytes=was["dest_bytes"],
                          resized=was.get("resized", False))
            return "skipped", record
        try:
            final, resized, size = shrink(s, dest / stem, args.max_edge, args.quality)
        except Exception as e:  # noqa: BLE001 — one picture must not stop the run
            return "failed", {"id": id_, "error": f"{type(e).__name__}: {e}"}
        if prev and final != dest / prev:
            # The source was replaced by one in another container: leaving the old leaf behind makes
            # a file nothing indexes — and this workspace is scanned by members, so a stray picture
            # of an obsolete product is a reference somebody will draw from.
            (dest / prev).unlink(missing_ok=True)
        record.update(dest=final.relative_to(dest).as_posix(), dest_bytes=size, resized=resized)
        return ("updated" if was else "added"), record

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, (state, rec) in enumerate(pool.map(one, rows), 1):
            fresh[str(rec.get("id") or rec.get("dest"))] = rec
            if state == "failed":
                stats["failed"].append(rec)
            else:
                stats[state] += 1
                stats["bytes"] += int(rec.get("dest_bytes") or 0)
                stats["resized"] += 1 if rec.get("resized") else 0
            if n % 100 == 0 or n == len(rows):
                print(f"    {n}/{len(rows)}  added={stats['added']} updated={stats['updated']} "
                      f"skipped={stats['skipped']} failed={len(stats['failed'])} "
                      f"({time.time() - started:.0f}s)")

    # Merge, so `--only 微导管` does not erase what an earlier run recorded about the other 42.
    merged = {**old, **fresh}
    manifest = {
        "from": str(src), "folder": FOLDER, "max_edge": args.max_edge,
        "quality": args.quality, "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # The rule travels with the pictures: a member that finds a file without its provenance
        # has no way to know it may not be published.
        "rule": payload.get("rule", ""),
        "publishable_note": payload.get("publishable_note", ""),
        "count": len(merged),
        "items": merged,
    }
    (dest / FOLDER).mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    note = dest / FOLDER / NOTE
    note.write_text(write_note(rows, manifest, args.max_edge), encoding="utf-8")
    stats["total_bytes"] = sum(int(r.get("dest_bytes") or 0) for r in merged.values())
    stats["note"] = str(note.relative_to(dest))
    stats["took_s"] = round(time.time() - started, 1)
    return stats


if __name__ == "__main__":
    raise SystemExit(main())
