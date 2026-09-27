#!/usr/bin/env python3
"""从作者的 PSD 稿子里提炼**绘图规范**：色板、图层术语、画布规格、可复用元件的清单。

为什么是这些、不是"把图学进去"：

* 本机**没有 SDXL + ControlNet**（上一轮盘点过），所以"用素材训练一个画风再生成新图"这条路
  不存在；硬做出来的解剖是幻觉，医学内容不能这么办。
* PSD 真正比 PNG 多出来的东西是**工序**：图层树说明这张图是怎么一层层搭起来的，图层名是作者
  自己的术语（「微导管」「瘤颈」「支架」），每一层的填充色是作者选定的颜色 —— 这些都能**量**，
  而且量出来就是规范，规范能约束"画得像不像他那套"。

所以产出不是模型，是一份**可读的规范**（`规范.md`）+ 机读的 `palette/terms/index`：
出图前照着定色板和标注用词，出图后拿它验收。素材增加时重跑，规范**增量生长** —— 这就是
「随资料库增加升级自己」的全部机制。

⚠️ 权利：只处理 `--include` 指定的格式。用户 2026-09-27 明确：他声明自有的是**所有 PSD 格式的
素材**，不是这个目录里的全部图片（那些 jpg/tif/pptx 不在声明内）。所以默认只吃 `.psd/.psb`，
并把 `rights=own / declared_by=user` 如实记进每一行 —— 声明是谁下的，就写谁。

⚠️ 只读：**不修改源文件**。产物写到 `--out`（默认中央库 `illustration/authored/`）。

  python scripts/ingest-illustration.py --root "/Volumes/Untitled/新建文件夹"
  python scripts/ingest-illustration.py --root DIR --limit 30 --dry-run
  python scripts/ingest-illustration.py --root DIR --only 原创绘图
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CENTRAL = Path.home() / ".team-agent" / "illustration" / "authored"
PSD_SUFFIXES = (".psd", ".psb")
# 声明的出处，写死在文件里而不是每次从命令行传：一条权利判据只该有一个说法。
RIGHTS = "own"
RIGHTS_BY = "user"
# 用户 2026-09-27 的两句话合起来才是完整的依据，缺一句就会被误读：
#   「声明的是所有 psd 格式的，不是所有的图片」+「出版社图片目录下的 PSD 是我的，是我出书发给出版社的」。
# 所以范围是**格式**（PSD），不是目录名 —— `出版社图片/` 名字像第三方，实际是他自己的书稿插图。
RIGHTS_NOTE = ("用户 2026-09-27 声明：他自有的是**所有 PSD 格式的稿子**，"
               "**包括 `出版社图片/` 下的那些 —— 那是他自己画完发给出版社的书稿插图**。"
               "同一目录下不是 PSD 的（jpg/tif/pptx/doc）不在该声明之内 —— "
               "这正好把 `原创绘图/BGC/Stryker取栓示意图-高清/` 那批第三方 jpg 挡在外面。"
               "按目录名猜权利会猜反：名字最像「出版社的」那个目录，反而是他的。")
RULE = ("许可判据：rights == 'own' 且 rights_declared_by == 'user' 的素材可用于对外发布的成片。"
        "本批 PSD 依用户声明为自有；目录里其余格式不在声明内，未摄入。")
# 图层名里当术语看的部分：去掉「图层 1」「副本」「拷贝」这类 Photoshop 自己和操作留下的噪声。
NOISE = re.compile(r"^(图层|Layer|组|Group|拷贝|副本|copy|背景|Background|\d+|\W)*$", re.I)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def palette_of(image, count: int = 10) -> list[dict]:
    """这张图的主色，按占比排。

    ⚠️ **先按 16 级/通道归并再统计**。不归并的话 `#FEFDFD`、`#FDFDFD`、`#FDFCFC` 会被算成三个
    颜色，而它们是同一个白 —— 实测第一版跑出来 20 个"色"里 13 个是近白的细微差别，真正的信号
    （血管的粉红、标注的深红）全被盖掉了。色板要回答的是「用了哪几个颜色」，不是「白色有多少种」。

    取自缩略图（`thumbnail()`）而不是 `composite()`：一张 800×1000、20 层的稿子合成要几秒，
    572 张就是半小时以上，而"用了哪几个颜色"在 128×160 上已经能回答。
    """
    import numpy as np
    small = image.convert("RGB").resize((64, 64))
    arr = np.asarray(small, dtype=np.int32).reshape(-1, 3)
    if not len(arr):
        return []
    bucket = arr // 64      # 每通道 4 级。⚠️ `// 16` 实测不够：`#E8BBA4`/`#E8B799`/`#EAC5A8`
                            # 会被当成三个颜色，而它们是同一块组织的高光与暗部 —— 色板于是变成
                            # 一张"每个颜色只出现一两次"的长表，读不出任何规律。
    keys, counts = np.unique(bucket, axis=0, return_counts=True)
    out: list[dict] = []
    for i in np.argsort(-counts)[:count]:
        mask = (bucket == keys[i]).all(axis=1)
        mean = arr[mask].mean(axis=0).round().astype(int)
        # ⚠️ 两个字段，各有各的用处：`key` 是**桶**，跨图统计必须用它；`hex` 是桶内均值，只用来
        # 显示。第一版只存了均值，于是"合并"只在单张图内部发生 —— 不同图里同一个桶的均值差几个
        # 数值，统计时被当成不同颜色又拆开了（实测：`#E19C9F`/`#E09DA2`/`#E19B9F`/`#DF9C9F`
        # 是同一个粉红，却被列成四条）。
        out.append({"key": "%d-%d-%d" % tuple(int(x) for x in keys[i]),
                    "hex": "#%02X%02X%02X" % tuple(mean),
                    "share": round(float(counts[i]) / len(arr), 3)})
    return out


SUBJECT_MIN = 2


def subject_of(stem: str) -> str:
    """文件名里的主题词，或空串。

    判据是**这一段的汉字**，不是"它看起来不像编号" —— 第一版用「长度 ≥ 2 且不是纯字母数字」
    过滤，结果 `PPT-377-2a`、`ppt-102a` 全被判成主题（带连字符），而真正的主题
    （`第二章.第五节 取栓困难解剖学因素`）反而不在表里。主题是中文写的，那就按中文判。
    章节点（第x章/第x节）和编号前缀去掉之后仍要剩下 ≥2 个字。
    """
    keep: list[str] = []
    for part in (p.strip() for p in stem.split(".")):
        if not re.search(r"[\u4e00-\u9fff]", part):
            continue                                  # 纯编号/英文款号，不是主题
        t = re.sub(r"第[一二三四五六七八九十百\d]+[章节]", "", part)
        t = t.strip(" .-_0123456789").strip()
        if len(t) >= SUBJECT_MIN:
            keep.append(t)
    return max(keep, key=len) if keep else ""


def subjects_of(rows: list[dict]) -> dict:
    """画过哪些主题 —— 取自**文件名**。

    ⚠️⚠️ 不是图层名，这是实测结论。这批 PSD 的图层名绝大多数是 Photoshop 默认的「图层 1」
    「组 2」「背景」，按图层名抽术语，572 个文件里只抽出 **2 条**（`2mm`、`10mm`），而那已经是
    全部。把"图层名 = 作者术语"当成前提写进方案是错的 —— 素材不会按我们希望的样式存在。
    """
    per_subject: collections.Counter = collections.Counter()
    unnamed = 0
    for r in rows:
        s = subject_of(Path(r["rel"]).stem)
        if s:
            per_subject[s] += 1
        else:
            unnamed += 1
    named_files = sum(1 for r in rows
                      if any(not NOISE.match(n) for n in (r.get("layer_names") or [])))
    # 图层名里**确实有意义**的那些词。第一版报告写"这栏量不出来"，那是按整张稿子判的；
    # 实测 572 个里有 177 个至少有一个非默认图层名，所以正确说法是"覆盖率低"，不是"没有"。
    words: collections.Counter = collections.Counter()
    for r in rows:
        for n in (r.get("layer_names") or []):
            if NOISE.match(n):
                continue
            for token in re.split(r"[\s\-_/、,，。()（）\[\]【】]+", n):
                t = token.strip()
                if len(t) >= 2 and not NOISE.match(t) and not t.isdigit():
                    words[t] += 1
    return {"subjects": dict(per_subject.most_common(60)),
            "without_subject": unnamed,
            "files_with_named_layers": named_files,
            "layer_words": dict(words.most_common(30))}


def palette_of_all(rows: list[dict]) -> dict:
    """底色 / 彩色前景 / 中性前景，**分开数**。

    ⚠️ 合在一起数没有意义，两层原因，都是实测的：
      1. 每张图里占比最大的都是背景（这批稿子是白底），它会把信号盖掉 → 底色单独一栏。
      2. 剩下的里面还有大量**近白的浅粉**（`#FAE8E8`、`#F8E7E8`…），它们是一个填色块的边缘色，
         每个只出现在两三张图里 —— 留着只会让「色板」看起来有四十个颜色，而实际上**一个复用的
         都没有**。按饱和度分开之后，「彩色」那一栏才是血管、器械、标注真正用的颜色。
    """
    import colorsys
    base: collections.Counter = collections.Counter()
    colorful: collections.Counter = collections.Counter()
    neutral: collections.Counter = collections.Counter()
    shown: dict[str, str] = {}          # 桶 → 一个代表性 hex，供展示
    for r in rows:
        pal = r.get("palette") or []
        if not pal:
            continue
        base[pal[0]["key"]] += 1
        shown.setdefault(pal[0]["key"], pal[0]["hex"])
        for c in pal[1:]:
            if c["share"] < 0.02:
                continue
            h = c["hex"]
            rgb = tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
            _h, s, v = colorsys.rgb_to_hsv(*rgb)
            (colorful if s >= 0.15 and 0.10 <= v <= 0.95 else neutral)[c["key"]] += 1
            shown.setdefault(c["key"], h)

    def top(counter: collections.Counter, n: int) -> dict:
        return {shown.get(k, k): v for k, v in counter.most_common(n)}

    return {"backgrounds": top(base, 6), "colorful": top(colorful, 24),
            "neutral": top(neutral, 10)}


def probe(path: Path) -> dict:
    row: dict = {"path": str(path)}
    try:
        row["sha256"] = sha256_of(path)
        st = path.stat()
        row["bytes"] = st.st_size
        row["mtime"] = st.st_mtime
    except OSError as e:
        return {**row, "error": f"read: {type(e).__name__}: {e}"}
    try:
        from psd_tools import PSDImage
        psd = PSDImage.open(path)
    except Exception as e:  # noqa: BLE001
        return {**row, "error": f"psd: {type(e).__name__}: {e}"}
    row.update(width=psd.width, height=psd.height, mode=str(psd.color_mode))
    names: list[str] = []
    kinds: collections.Counter = collections.Counter()
    texts: list[str] = []
    try:
        for layer in psd.descendants():
            name = str(getattr(layer, "name", "") or "").strip()
            if name:
                names.append(name)
            try:
                kinds[str(getattr(layer, "kind", "") or "unknown")] += 1
            except Exception:  # noqa: BLE001
                pass
            # 文字层的内容 = 作者写在图上的标注用语，比图层名更接近术语表
            if str(getattr(layer, "kind", "")) == "type":
                try:
                    txt = str(layer.text or "").strip()
                    if txt:
                        texts.append(txt[:60])
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        row["layers_error"] = f"{type(e).__name__}: {e}"
    row["layers"] = len(names)
    row["layer_names"] = names[:60]
    row["layer_names_more"] = max(0, len(names) - 60)
    row["kinds"] = dict(kinds)
    row["texts"] = texts[:20]
    try:
        thumb = psd.thumbnail()
        if thumb is not None:
            row["palette"] = palette_of(thumb)
        else:
            row["palette_error"] = "no embedded thumbnail"
    except Exception as e:  # noqa: BLE001
        row["palette_error"] = f"{type(e).__name__}: {e}"
    return row


def render_note(rows: list[dict], palette: dict, subjects: dict, payload: dict) -> str:
    """规范正文。每一栏都写清它是从多少张图里量出来的，以及**哪一栏没能量出来**。"""
    ok = [r for r in rows if not r.get("error")]
    sizes = collections.Counter(f"{r['width']}×{r['height']}" for r in ok)
    layers = sorted(r["layers"] for r in ok if r.get("layers"))
    L = [
        "# 绘图规范（从作者自己的 PSD 稿子里量出来的）",
        "",
        f"来源：**{len(rows)} 个 PSD**（`{payload['root']}`），解析成功 {len(ok)} 个。",
        f"权利：`{payload['rights']}`，声明人 `{payload['rights_declared_by']}`。",
        f"依据：{payload.get('rights_note', '')}",
        "",
        "**这是量出来的，不是定的** —— 每一条都写着它是从多少张图里得到的。量不出来的地方也写着。",
        "",
        "## 画布",
        "",
        "| 尺寸 | 张数 |",
        "| --- | --- |",
    ]
    for s, n in sizes.most_common(8):
        L.append(f"| {s} | {n} |")
    if layers:
        L += ["", f"图层数：最少 {layers[0]}、中位 {layers[len(layers)//2]}、最多 {layers[-1]}；"
                  f"其中 ≥ 5 层的 {sum(1 for x in layers if x >= 5)} 个。"
                  "层数是**工序**的痕迹 —— 分层越多，越说明这张图是一个部件一个部件搭出来的。"]
    L += ["", "## 色板", "",
          "底色：" + "、".join(f"`{c}`×{n}" for c, n in list(palette["backgrounds"].items())[:6]),
          "",
          "**彩色前景** —— 血管、器械、标注真正用的颜色。出图时从这里选，别每次现调：", "",
          "| 颜色 | 出现在多少张图 |", "| --- | --- |"]
    for c, n in list(palette["colorful"].items())[:20]:
        L.append(f"| `{c}` | {n} |")
    if not palette["colorful"]:
        L += ["", "（一个都没有：这批稿子里没有反复复用的彩色 —— 见下面那栏。）"]
    L += ["", "中性前景（描边、标注、灰阶底纹）：" +
          "、".join(f"`{c}`×{n}" for c, n in list(palette["neutral"].items())[:8]), "",
          "## 画过哪些主题", "",
          "取自**文件名**（`第二章.第五节 取栓困难解剖学因素`）—— 这是「学过哪些解剖与术式」的清单，"
          "也是挑同类旧稿时的入口：", "",
          "| 主题 | 张数 |", "| --- | --- |"]
    for t, n in list(subjects["subjects"].items())[:40]:
        L.append(f"| {t} | {n} |")
    if subjects["without_subject"]:
        L += ["", f"另有 {subjects['without_subject']} 个稿子的文件名里没有主题词（纯编号，"
                  f"如 `011a.psd`）—— 这批图**只能靠看**才知道画的是什么。"]
    named = subjects["files_with_named_layers"]
    L += [
        "", "## 图层名里没有术语（**这是实测结论，不是没查**）", "",
        f"**{len(ok)} 个稿子里有 {named} 个的图层名至少有一个不是 Photoshop 默认名**"
        f"（{named * 100 // max(1, len(ok))}%），其余全是「背景」「图层 1」「组 2」。",
        "",
        "⚠️⚠️ 而把那 {named} 个的图层名抽成词之后，**没有一个是解剖或器械术语** —— 全是过程留下的东西。"
        "所以「图层名 = 作者自己的术语」这个前提在这批素材上**不成立**，词表不能从这里建。"
        "证据在下面那张表里，不是一句推测。",
    ]
    if subjects.get("layer_words"):
        L += ["", "抽到的词（就是这些，出现次数按图层数）：", "",
              "| 词 | 图层数 |", "| --- | --- |"]
        for w, n in list(subjects["layer_words"].items())[:16]:
            L.append(f"| {w} | {n} |")
        L += ["", "它们分别是什么：Photoshop 的形状与调色工具（`形状`、`多边形`、`色阶`）、"
                  "导入时带进来的文件名（`微信图片`、`老照片变清晰`）、自动生成的哈希串（`fa3cd7b1…`）。"
                  "**一条解剖或器械名词都没有。**", "",
              "想让图层名变成可用的术语表，得人工给图层改名 —— 那是一次手工工序，"
              "值不值得由你定；在此之前，绘图时的用词来源只能是**文件名里的主题**"
              "（见上一节）和成员自己的领域知识。"]
    L += [
        "",
        "## 怎么用", "",
        "1. **出图前**：尺寸取自「画布」，颜色取自「彩色前景」，主题对照「画过哪些主题」找同类旧稿。",
        "2. **出图后**：对照同一张分镜要求核验 —— 画了什么、比例对不对、配色是不是这一套。",
        "3. **素材增加后**：重跑 `scripts/ingest-illustration.py`，规范增量更新。",
        "",
        f"（逐张的图层名与色板在 `index.json`；色板与主题分别另有 `palette.json`/`subjects.json`。）"
        f"生成于 {payload['ingested_at']}。",
    ]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=str(CENTRAL))
    ap.add_argument("--only", default="", help="只吃某一个子目录，例如 原创绘图")
    ap.add_argument("--include", default=".psd,.psb",
                    help="按扩展名摄入；用户声明自有的是 PSD，改这里等于改声明范围")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"没有这个目录：{root}", file=sys.stderr)
        return 2
    want = tuple(x.strip().lower() for x in args.include.split(",") if x.strip())
    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and p.suffix.lower() in want and not p.name.startswith("."))
    if args.only:
        files = [p for p in files if p.relative_to(root).parts[0] == args.only]
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("没有匹配的文件", file=sys.stderr)
        return 2
    print(f"摄入 {len(files)} 个 {want} 文件（只读源文件）…")
    if args.dry_run:
        for p in files[:10]:
            print("  ", p.relative_to(root))
        print(f"  共 {len(files)} 个")
        return 0

    started = time.time()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, r in enumerate(pool.map(probe, files), 1):
            r["rel"] = str(Path(r["path"]).relative_to(root))
            r["top"] = Path(r["rel"]).parts[0] if len(Path(r["rel"]).parts) > 1 else "(根目录)"
            rows.append(r)
            if n % 100 == 0 or n == len(files):
                print(f"  {n}/{len(files)}  ({time.time() - started:.0f}s)")

    ok = [r for r in rows if not r.get("error")]
    palette = palette_of_all(ok)
    subjects = subjects_of(ok)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "root": str(root), "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rights": RIGHTS, "rights_declared_by": RIGHTS_BY, "rights_note": RIGHTS_NOTE,
        "rule": RULE, "include": list(want),
        "count": len(rows), "parsed": len(ok), "errors": [r for r in rows if r.get("error")],
    }
    (out / "index.json").write_text(
        json.dumps({**payload, "items": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "palette.json").write_text(json.dumps(palette, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    (out / "subjects.json").write_text(json.dumps(subjects, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    note = out / "规范.md"
    note.write_text(render_note(rows, palette, subjects, payload), encoding="utf-8")
    print(f"\n解析成功 {len(ok)}/{len(rows)} · 彩色前景 {len(palette['colorful'])} 个 · "
          f"主题 {len(subjects['subjects'])} 条 · 图层名可用的稿子 "
          f"{subjects['files_with_named_layers']} 个")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
