#!/usr/bin/env python3
"""把一个素材目录**只读地**扫一遍：有哪些重复、哪些能当元件库用。

为什么用感知哈希而不是只比 sha256：素材里真正的问题是**同一张图的多个版本**——PSD 与它导出的
JPG、`图一.psd` 与 `图一-副本.psd`、草稿与定稿。它们的字节完全不同，sha256 一个都不会命中，
而它们占的是同一份空间、教的是同一个东西。所以两种指纹都给：

* `sha256` —— 字节级相同，可以直接按重复处理；
* `dhash`  —— 64 位感知指纹，汉明距离小 = 看起来就是同一张图。

⚠️ PSD 不走 `composite()`（合成一张 800×1000、20 个图层的稿子要几秒，572 张要半小时以上），
而是读文件头里内嵌的缩略图——实测 0.01–0.03 秒一张。缩略图只有 128×160，够判「是不是同一张」，
不够判「细节差多少」，所以 128×160 的指纹只用来**找候选**。

⚠️⚠️ 这个脚本**只读**：不移动、不重命名、不删除任何文件。去重是要由人看过报告之后决定的事，
尤其是素材盘上的东西 —— 两张看着一样的图，可能是「有图层」和「已压平」两个版本，删掉哪一张
都要先看清。

  python scripts/scan-illustration-library.py --root "/Volumes/Untitled/新建文件夹"
  python scripts/scan-illustration-library.py --root DIR --near-distance 4 --out report.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RASTER = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif")
VECTOR = (".svg", ".eps", ".ai")
DOC = (".pptx", ".ppt", ".docx", ".doc", ".pdf", ".zip", ".rar", ".7z", ".mp4", ".mov")
HASH_BLOCK = 1 << 20


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(HASH_BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def dhash(image, size: int = 8) -> int:
    """64-bit difference hash: each bit is "this pixel is brighter than the one to its right"."""
    import numpy as np
    from PIL import Image as PILImage
    # ⚠️⚠️ `PILImage.Resampling`, **not** `image.Resampling`. `image` is a PIL *instance* and has no
    # `Resampling` attribute, so the first version of this raised `AttributeError` on every raster
    # file — and on every PSD it was swallowed by a bare `except` in `_probe_psd`. The run then
    # reported "0 near-duplicate groups", which is exactly what a genuinely clean library looks
    # like. A check that never ran must never be able to report that there is nothing to find.
    g = image.convert("L").resize((size + 1, size), PILImage.Resampling.LANCZOS)
    a = np.asarray(g, dtype=np.int16)
    bits = np.packbits(a[:, 1:] > a[:, :-1])
    return int.from_bytes(bits.tobytes()[: size], "big")


def popcount(x: int) -> int:
    return bin(x).count("1")


def probe(path: Path) -> dict:
    """One file's row. Never raises: a file that cannot be read comes back with `error`."""
    row: dict = {"path": str(path)}
    try:
        st = path.stat()
        row.update(bytes=st.st_size, mtime=st.st_mtime)
    except OSError as e:
        return {**row, "error": f"stat: {type(e).__name__}: {e}"}
    ext = path.suffix.lower()
    row["ext"] = ext
    try:
        row["sha256"] = sha256_of(path)
    except OSError as e:
        row["error"] = f"read: {type(e).__name__}: {e}"
        return row
    if ext == ".psd" or ext == ".psb":
        return {**row, **_probe_psd(path)}
    if ext in RASTER:
        return {**row, **_probe_raster(path)}
    return row


def _probe_psd(path: Path) -> dict:
    try:
        from psd_tools import PSDImage
        im = PSDImage.open(path)
    except Exception as e:  # noqa: BLE001 — a broken PSD must not stop the walk
        return {"error": f"psd: {type(e).__name__}: {e}"}
    out = {"width": im.width, "height": im.height, "mode": im.color_mode}
    try:
        kids = list(im.descendants())
        out["layers"] = len(kids)
        names = [str(k.name) for k in kids if str(getattr(k, "name", "")).strip()]
        out["layer_names"] = names[:40]
        out["layer_names_more"] = max(0, len(names) - 40)
    except Exception:  # noqa: BLE001
        pass
    try:
        thumb = im.thumbnail()
        if thumb is not None:
            out["dhash"] = dhash(thumb)
            out["thumb"] = list(thumb.size)
        else:
            # Said out loud rather than left as "no fingerprint, no explanation": a PSD without an
            # embedded thumbnail is a real thing to know about, and silence here is how a whole
            # category of files disappears from the near-duplicate pass without anyone noticing.
            out["dhash_error"] = "no embedded thumbnail"
    except Exception as e:  # noqa: BLE001
        out["dhash_error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_raster(path: Path) -> dict:
    try:
        from PIL import Image
        # The library holds a 104 MP TIFF (measured); PIL's default 89 MP DecompressionBomb warning
        # is noise for files the user handed over on purpose. A hard refusal stays, just further out.
        Image.MAX_IMAGE_PIXELS = 400_000_000
        with Image.open(path) as im:
            # Size first: `draft` *changes* `im.size`, so reading it afterwards records the
            # downscaled draft rather than the file's own dimensions (a 4000×3000 JPEG would have
            # been written into the report as ~500×375).
            out = {"width": im.size[0], "height": im.size[1], "mode": im.mode}
            im.draft("RGB", (512, 512))    # JPEG decodes at 1/2, 1/4, 1/8 when this is called
            out["dhash"] = dhash(im)
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": f"image: {type(e).__name__}: {e}"}


def kind_of(ext: str) -> str:
    if ext in (".psd", ".psb"):
        return "psd"
    if ext in RASTER:
        return "raster"
    if ext in VECTOR:
        return "vector"
    if ext in DOC:
        return "doc"
    return "other"


def group(rows: list[dict], distance: int) -> tuple[list[list[dict]], list[list[dict]]]:
    """`(exact groups, near groups)`. Exact wins: a byte-identical set is never also reported as near."""
    exact: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("sha256"):
            exact[r["sha256"]].append(r)
    exact_groups = [g for g in exact.values() if len(g) > 1]
    claimed = {r["path"] for g in exact_groups for r in g}

    hashed = [r for r in rows if r.get("dhash") is not None and r["path"] not in claimed]
    near: list[list[dict]] = []
    seen: set[str] = set()
    for i, a in enumerate(hashed):
        if a["path"] in seen:
            continue
        bucket = [a]
        for b in hashed[i + 1:]:
            if b["path"] in seen:
                continue
            if popcount(a["dhash"] ^ b["dhash"]) <= distance:
                bucket.append(b)
        if len(bucket) > 1:
            for r in bucket:
                seen.add(r["path"])
            near.append(bucket)
    return exact_groups, near


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="", help="Markdown 报告路径")
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--near-distance", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"没有这个目录：{root}", file=sys.stderr)
        return 2
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))
    print(f"扫描 {len(files)} 个文件（只读）…", flush=True)

    started = time.time()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, r in enumerate(pool.map(probe, files), 1):
            rows.append(r)
            if n % 200 == 0 or n == len(files):
                print(f"  {n}/{len(files)}  ({time.time() - started:.0f}s)", flush=True)

    for r in rows:
        r["kind"] = kind_of(r.get("ext", ""))
        r["rel"] = str(Path(r["path"]).relative_to(root))
        r["top"] = Path(r["rel"]).parts[0] if len(Path(r["rel"]).parts) > 1 else "(根目录)"

    exact_groups, near_groups = group(rows, args.near_distance)
    total = sum(int(r.get("bytes") or 0) for r in rows)
    wasted = sum(int(g[0].get("bytes") or 0) * (len(g) - 1) for g in exact_groups)

    payload = {
        "root": str(root), "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": len(rows), "bytes": total, "near_distance": args.near_distance,
        "exact_groups": [[r["rel"] for r in g] for g in exact_groups],
        "near_groups": [[r["rel"] for r in g] for g in near_groups],
        "fingerprints": fingerprint_report(rows),
        "rows": [{k: v for k, v in r.items() if k != "layer_names"} for r in rows],
        "layer_names": {r["rel"]: r.get("layer_names", []) for r in rows if r.get("layer_names")},
    }
    print(f"\n精确重复组 {len(exact_groups)}（冗余 {human(wasted)}）· "
          f"近似重复组 {len(near_groups)} · 共 {len(rows)} 个文件 / {human(total)}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        print(f"json → {args.json_out}")
    if args.out:
        Path(args.out).write_text(render(payload, rows, exact_groups, near_groups), encoding="utf-8")
        print(f"报告 → {args.out}")
    return 0


def fingerprint_report(rows: list[dict]) -> dict:
    """How much of the library actually got a perceptual fingerprint.

    ⚠️ This exists because of a measured failure. The first version of this script produced **zero**
    fingerprints — every raster file raised, every PSD's error was swallowed — and the report then
    said "0 near-duplicate groups". Silence and success were indistinguishable in the output. So the
    coverage is now a field, and `silent` (an image with neither a fingerprint nor a reason) is
    reported as the defect it is rather than as a number nobody looks at.
    """
    images = [r for r in rows if r.get("kind") in ("psd", "raster")]
    return {
        "images": len(images),
        "fingerprinted": sum(1 for r in images if r.get("dhash") is not None),
        "explained": sum(1 for r in images if r.get("dhash") is None and r.get("dhash_error")),
        "silent": [r["rel"] for r in images if r.get("dhash") is None and not r.get("dhash_error")],
    }


def _risk(group: list[dict]) -> str:
    """这一组像「重复」，还是像「同一套图的不同张」。

    ⚠️ 判据来自误报本身：**同目录 + 同尺寸 + 同类型**的组，最可能是一套图的不同张 —— 手绘源文件
    常按 `1.psd … 7.psd` 这么存，把它们当重复会**删掉原创稿**，那是这批评审里赔不起的一种错。
    而「PSD 与它导出的 JPG」或「同一张跨目录存了两处」才是真重复。
    用户已确认按重复处理，所以这一列不是拦阻，是把风险摆到他眼前。
    """
    dirs = {str(Path(r["rel"]).parent) for r in group}
    sizes = {(r.get("width"), r.get("height")) for r in group}
    kinds = {r.get("kind") for r in group}
    if len(dirs) > 1:
        return "跨目录 → 同一张存了两处"
    if len(kinds) > 1:
        return "同类目录但格式不同 → 原稿与它的导出"
    if len(dirs) == 1 and len(sizes) == 1:
        return "⚠️ **同目录同尺寸同类型 → 像一套图的不同张**"
    return "同目录不同尺寸 → 需看一眼"


def _keep_first(group: list[dict]) -> list[dict]:
    """建议保留的那一张排在最前，其余是去重候选。

    判据是**可编辑性优先**，不是"文件名看着正式"：一张 `.psd` 和它导出的 `.jpg` 画面几乎一样，
    但保留 jpg 就把图层丢了 —— 那正是这批评审里最贵的一种损失。同类里再按体积降序（体积大的
    通常分辨率高、或者还带着未被裁掉的画布）。
    """
    rank = {"psd": 0, "vector": 1, "raster": 2, "doc": 3}
    return sorted(group, key=lambda r: (rank.get(str(r.get("kind")), 9), -int(r.get("bytes") or 0)))


def render(payload: dict, rows: list[dict], exact_groups: list[list[dict]],
           near_groups: list[list[dict]]) -> str:
    import collections
    tops = collections.Counter(r["top"] for r in rows)
    kinds = collections.Counter(r["kind"] for r in rows)
    bytes_by_top: dict[str, int] = collections.defaultdict(int)
    for r in rows:
        bytes_by_top[r["top"]] += int(r.get("bytes") or 0)
    broken = [r for r in rows if r.get("error")]
    psds = [r for r in rows if r["kind"] == "psd" and not r.get("error")]
    sizes = collections.Counter(f"{r.get('width')}×{r.get('height')}" for r in psds)
    layers = [r["layers"] for r in psds if r.get("layers")]
    L = [
        "# 素材盘扫描（只读）",
        "",
        f"- 目录：`{payload['root']}`",
        f"- 共 **{len(rows)} 个文件 / {human(sum(int(r.get('bytes') or 0) for r in rows))}**",
        f"- 扫描于 {payload['scanned_at']}；**没有移动、重命名或删除任何文件**",
        "",
        "## 构成",
        "",
        "| 顶层目录 | 文件 | 体积 | 类型分布 |",
        "| --- | --- | --- | --- |",
    ]
    for top, n in tops.most_common():
        sub = collections.Counter(r["ext"] for r in rows if r["top"] == top)
        dist = "、".join(f"{e or '(无扩展名)'} {c}" for e, c in sub.most_common(5))
        L.append(f"| {top} | {n} | {bytes_by_top[top] / 1024**3:.1f} GB | {dist} |")
    L += ["", "总体类型：" + "、".join(f"{k} {v}" for k, v in kinds.most_common()), ""]

    L += [
        "## 重复",
        "",
        f"精确重复（字节完全相同）**{len(exact_groups)} 组**，"
        f"可省 **{human(sum(int(g[0].get('bytes') or 0) * (len(g) - 1) for g in exact_groups))}**；"
        f"近似重复（感知距离 ≤ {payload['near_distance']}）**{len(near_groups)} 组**。",
        "",
    ]
    fp = payload["fingerprints"]
    L += [
        "### 指纹覆盖（**这一节决定上一节的近似重复能不能信**）",
        "",
        f"- 图片总数 {fp['images']}；算出了感知指纹 **{fp['fingerprinted']}**；"
        f"有明确原因没算出的 {fp['explained']}（如 PSD 内嵌缩略图缺失）。",
    ]
    if fp["silent"]:
        L += [
            "",
            f"⚠️⚠️ **{len(fp['silent'])} 个图片既没有指纹、也没有原因** —— 近似重复这一项**不可信**，"
            "不要据此下结论。前 10 个：",
            "",
        ] + [f"- `{p}`" for p in fp["silent"][:10]]
    else:
        L += ["", "没有「既无指纹又无原因」的文件，所以近似重复的结论可以采信。"]
    L.append("")
    if exact_groups:
        L += ["### 精确重复", "", "| 份数 | 体积/份 | 文件 |", "| --- | --- | --- |"]
        for g in sorted(exact_groups, key=lambda g: -int(g[0].get("bytes") or 0))[:40]:
            names = "<br>".join(f"`{r['rel']}`" for r in g[:6])
            more = f"<br>…另 {len(g) - 6} 个" if len(g) > 6 else ""
            L.append(f"| {len(g)} | {human(int(g[0].get('bytes') or 0))} | {names}{more} |")
        cross = collections.Counter()
        cross_bytes: dict[tuple[str, str], int] = collections.defaultdict(int)
        for g in exact_groups:
            dirs = sorted({str(Path(r["rel"]).parent) for r in g})
            for i, a in enumerate(dirs):
                for b in dirs[i + 1:]:
                    cross[(a, b)] += 1
                    cross_bytes[(a, b)] += int(g[0].get("bytes") or 0)
        if cross:
            L += ["", "**哪些目录其实是同一批东西**（精确重复里跨目录最多的 10 对）—— "
                      "两个目录互相重复，通常就是同一批素材存了两处，理清一处就够：", "",
                  "| 组数 | 这些组占的体积 | 目录 A | 目录 B |", "| --- | --- | --- | --- |"]
            for (a, b), n in cross.most_common(10):
                L.append(f"| {n} | {human(cross_bytes[(a, b)])} | `{a}` | `{b}` |")
    if near_groups:
        L += [
            "", "### 近似重复（**用户已确认按重复处理**）", "",
            "画面看起来几乎一样、但字节不同 —— PSD 与它导出的 JPG、草稿与定稿、同一套图的相邻几张。",
            "用户 2026-09-27 看过这批候选后确认：**不必再做全分辨率核对，按重复处理**。",
            "",
            "**保留哪一张**的判据（脚本按这个排的序，不是随机）：① 能编辑的优先 —— `.psd` > 其它；"
            "② 同类型里体积大的优先。**目录里的原稿留在原地，被列的只是「其余可以不要的」**。", "",
            "| 组 | 像不像重复 | 建议保留 | 其余（去重候选） | 可省 |", "| --- | --- | --- | --- | --- |"]
        for i, g in enumerate(sorted(near_groups, key=lambda g: -len(g))[:40], 1):
            ordered = _keep_first(g)
            head, rest = ordered[0], ordered[1:]
            saved = sum(int(r.get("bytes") or 0) for r in rest)
            def cell(r: dict) -> str:
                return (f"`{r['rel']}` ({human(int(r.get('bytes') or 0))}"
                        + (f", {r.get('width')}×{r.get('height')}" if r.get("width") else "") + ")")
            L.append(f"| {i} | {_risk(g)} | {cell(head)} | "
                     + ("<br>".join(cell(r) for r in rest[:5])
                        + (f"<br>…另 {len(rest) - 5} 个" if len(rest) > 5 else ""))
                     + f" | {human(saved)} |")
        total_saved = sum(sum(int(r.get("bytes") or 0) for r in _keep_first(g)[1:])
                          for g in near_groups)
        L += ["", f"这些近似组全部按重复处理的话，**再省约 {human(total_saved)}**。",
              "",
              "⚠️ **这份清单只是清单** —— 脚本没有移动、重命名或删除任何文件（素材盘上的东西"
              "要动之前得你点头）。要我按这份清单去重，说一声；我会先整盘备份再动，且只把候选"
              "**移进一个隔离目录**，不直接删。"]

    L += ["", "## 能不能当元件库（PSD 的结构）", ""]
    if psds:
        L += [
            f"可解析的 PSD **{len(psds)} 个**。",
            "",
            f"- 画布尺寸（前 8）：" + "、".join(f"{s}（{c}）" for s, c in sizes.most_common(8)),
        ]
        if layers:
            layers_sorted = sorted(layers)
            L.append(f"- 图层数：最少 {layers_sorted[0]}、中位 {layers_sorted[len(layers_sorted)//2]}、"
                     f"最多 {layers_sorted[-1]}")
            L.append(f"- 图层数 ≥ 5 的：{sum(1 for x in layers if x >= 5)} 个 "
                     f"（分层才有可复用的元件，压平的只有一张画面）")
    else:
        L.append("没有解析出 PSD —— 检查 `psd-tools` 是否装在当前解释器里。")
    if broken:
        L += ["", f"## 读不了的文件（{len(broken)} 个）", ""] + \
             [f"- `{r['rel']}` — {r['error']}" for r in broken[:20]]
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
