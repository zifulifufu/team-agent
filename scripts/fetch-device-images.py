#!/usr/bin/env python3
"""Fetch NeuroFlow's device pictures into a local reference library.

Why this exists: the film group's worst failures came from **drawing a device nobody had seen** —
a microcatheter that came out an even smooth tube, a stent that could not be drawn at all, a "flow
diverter" faked with a title. Members have no image search (only literature search, a document's
own figures, and `review_picture`), so "go and look up what a Solitaire looks like" was an
instruction they could not carry out. NeuroFlow's catalog names 513 real products with a picture
each; that is the reference layer that was missing.

⚠️⚠️ **Provenance is recorded per picture, never assumed.** The user confirmed the NeuroFlow
material is his own — but pictures added later may not be, and a reference file with no recorded
origin is exactly the one that ends up published by accident. Every row in `index.json` therefore
carries where it came from and under whose rights, and `own` is the only value that may be
published; anything else is internal reference only.

  python scripts/fetch-device-images.py                 # everything, resumable
  python scripts/fetch-device-images.py --only 微导管     # one category
  python scripts/fetch-device-images.py --limit 20 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ⚠️ No default path here on purpose. The first version of this script had the author's own
# NeuroFlow directory hard-coded in it, in a **public** repository whose `.gitignore` says outright
# that absolute paths and environment details do not belong in the tree. `--catalog` or
# `$NEUROFLOW_CATALOG` now, and a clear complaint when neither is given.
DEST = Path.home() / ".team-agent" / "illustration" / "devices"
INDEX = "index.json"
UA = {"User-Agent": "team-agent/1.0 (local reference library)"}
# Straight out, no proxy: this is a domestic CDN and the proxy in front of it is what turns a
# 3-second download into a 502.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Who says it is ours, and what we actually know about where it came from. These are **two
# different facts** and neither implies the other:
#
# ⚠️⚠️ 用户 2026-09-27 明确纠正过这里的语义：他说「这些素材都是我自己的」，指的是**他自己那批
# PSD 格式的素材**，不是这里抓的图。这 513 张转存自脑医汇公开产品目录，图中器械的原始作者是各
# 厂商 —— 那是**第三方**，没有人声明过可对外使用。第一版脚本把 `RIGHTS` 写成 `own`、把
# `publishable` 写成 `unconfirmed`，等于替用户认领了一批不是他的图；那正是用户强调
# 「后续再提供新的图片不一定是我自己的」时要防的事。
RIGHTS = "third-party"
RIGHTS_BY = ""                     # 没有人为这批图声明过权利
RIGHTS_NOTE = ("转存自脑医汇（brainmed.com）公开产品目录；图中器械的原始作者是各厂商。"
               "**不属于**用户的自有素材（用户 2026-09-27：他声明自有的是 PSD 格式的素材）。")
ORIGIN = "brainmed-product-photo"  # measured: reposted from a public product catalogue
ORIGIN_NOTE = ("转存自脑医汇（brainmed.com）公开产品目录；图中器械的原始作者是各厂商。"
               "作为**内部形态参考**使用（画器械时对照），不改变其原始权利归属。")
# 「内部参考不受限」和「能不能放进成片」是两件事，这里回答的是后者。
PUBLISHABLE = "no"
PUBLISHABLE_NOTE = "不可用于对外发布的成片。内部参考不受限（画之前对照着看）。"
# The one place the rule lives. Everything downstream — the sync script, the index a member reads,
# the expert's spec — renders **this** sentence instead of writing its own; a rule that exists in
# four spellings is how three of them end up disagreeing.
RULE = ("许可判据：只有 rights == 'own' 且 rights_declared_by == 'user' 的素材可用于对外发布的"
        "成片。本批全部是 'third-party'（转存自公开产品目录，原始作者为各厂商）→ 只作内部参考，"
        "不对外。用户自有的 PSD 素材在此之外，摄入时按 'own' / 'user' 标记。")


def slug(text: str, limit: int = 60) -> str:
    """A filename that survives every filesystem, while keeping Chinese legible."""
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(text or "")).strip("-._")
    return (s[:limit] or "item")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=os.environ.get("NEUROFLOW_CATALOG", ""),
                    help="NeuroFlow 的 device-catalog JSON；也可用环境变量 NEUROFLOW_CATALOG 指定")
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--only", default="", help="one category, e.g. 微导管")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not str(args.catalog).strip():
        print("需要 --catalog <brainmed.json>（或设 NEUROFLOW_CATALOG）：脚本不猜路径，"
              "因为仓库是公开的，个人绝对路径不该写进来", file=sys.stderr)
        return 2
    cat_path, dest = Path(args.catalog).expanduser(), Path(args.dest)
    if not cat_path.is_file():
        print(f"catalog not found: {cat_path}", file=sys.stderr)
        return 2
    items = json.loads(cat_path.read_text(encoding="utf-8")).get("items") or []
    if args.only:
        items = [i for i in items if i.get("category") == args.only]
    if args.limit:
        items = items[: args.limit]
    print(f"{len(items)} products → {dest}")

    if args.dry_run:
        for i in items[:10]:
            print("  would fetch", i["category"], "/", slug(i["name"]), "←", (i.get("productImage") or "")[:90])
        return 0

    # Existing files are reused: this must be safe to re-run after an interruption.
    old = {}
    index_path = dest / INDEX
    if index_path.is_file():
        try:
            old = {r["id"]: r for r in json.loads(index_path.read_text(encoding="utf-8")).get("items", [])}
        except (OSError, ValueError, KeyError):
            old = {}

    rows: list[dict] = []
    failed: list[dict] = []
    started = time.time()

    def fetch(item: dict) -> dict:
        url = str(item.get("productImage") or "")
        folder = dest / slug(item.get("category") or "未分类")
        ext = Path(url.split("?")[0]).suffix.lower() or ".png"
        target = folder / f"{slug(item.get('name'))}{ext}"
        previous = old.get(item.get("id") or "")
        if previous and Path(previous.get("file", "")).is_file():
            # The picture is already on disk — reuse the file, but **re-stamp the provenance**:
            # inheriting yesterday's wording is how a corrected rule silently stays wrong.
            return {**previous, "reused": True,
                    "rights": RIGHTS, "rights_declared_by": RIGHTS_BY, "rights_note": RIGHTS_NOTE,
                    "origin": ORIGIN, "origin_note": ORIGIN_NOTE,
                    "publishable": PUBLISHABLE, "publishable_note": PUBLISHABLE_NOTE}
        if not url:
            raise ValueError("no productImage")
        folder.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for _ in range(2):
            try:
                with OPENER.open(urllib.request.Request(url, headers=UA), timeout=60) as r:
                    data = r.read()
                if not data:
                    raise ValueError("empty body")
                target.write_bytes(data)
                return {"id": item.get("id"), "name": item.get("name"),
                        "category": item.get("category"), "manufacturer": item.get("manufacturer"),
                        "file": str(target), "bytes": len(data),
                        "source_url": url, "source_page": item.get("sourceUrl"),
                        "source_site": "脑医汇（brainmed.com）公开产品目录",
                        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "rights": RIGHTS, "rights_declared_by": RIGHTS_BY, "rights_note": RIGHTS_NOTE,
                        "origin": ORIGIN, "origin_note": ORIGIN_NOTE,
                        "publishable": PUBLISHABLE, "publishable_note": PUBLISHABLE_NOTE}
            except Exception as e:  # noqa: BLE001 — one picture must not stop the run
                last = e
                time.sleep(1.5)
        raise last or RuntimeError("download failed")

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, item in enumerate(pool.map(lambda i: _safe(fetch, i), items), 1):
            if item.get("error"):
                failed.append(item)
            else:
                rows.append(item)
            if n % 50 == 0 or n == len(items):
                print(f"  {n}/{len(items)}  ok={len(rows)} failed={len(failed)} "
                      f"({time.time() - started:.0f}s)")

    # Merge with what was already known so a partial run does not lose earlier rows.
    merged = {r["id"]: r for r in old.values() if r.get("id")}
    for r in rows:
        merged[r["id"]] = r
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": {"catalog": str(cat_path), "site": "脑医汇（brainmed.com）公开产品目录"},
        "rights": RIGHTS,
        "rights_declared_by": RIGHTS_BY,
        "rights_note": RIGHTS_NOTE,
        "origin": ORIGIN,
        "origin_note": ORIGIN_NOTE,
        # The rule the zone and the drawing expert both read. Written as one sentence so it cannot
        # be paraphrased into something looser at the far end.
        "rule": RULE,
        "publishable_note": PUBLISHABLE_NOTE,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(merged),
        "items": sorted(merged.values(), key=lambda r: (r.get("category") or "", r.get("name") or "")),
        "failed": failed,
    }
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(int(r.get("bytes") or 0) for r in merged.values())
    print(f"wrote {index_path}")
    print(f"  {len(merged)} pictures · {total / 1024 / 1024:.0f} MB · failed {len(failed)} · "
          f"took {time.time() - started:.0f}s")
    return 0


def _safe(fn, item: dict) -> dict:
    """One failure must not take the pool down with it — the row comes back with its reason."""
    try:
        return fn(item)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "id": item.get("id"), "name": item.get("name"),
                "category": item.get("category"), "source_url": item.get("productImage")}


if __name__ == "__main__":
    raise SystemExit(main())
