#!/usr/bin/env python3
"""Turn NeuroFlow's device catalog into an index a drawing expert can actually use.

Why this exists: the film group's worst failures were **drawing a device the member had never seen** —
a microcatheter that was a smooth even tube, a stent that could not be drawn at all, a "flow diverter"
faked with a title. The members have no image search (only literature search, a document's own
figures, and `review_picture`), so "look up what a Solitaire looks like" was an instruction they
could not carry out. NeuroFlow already holds 513 real products from 59 makers, with an official
product image for every one of them — that is the reference layer this was missing.

Reads:  <NeuroFlow>/data/device-catalog/brainmed.json   (read-only)
Writes: docs/neuro-device-catalog-index-YYYY-MM-DD.md

Path comes from `--catalog` or `$NEUROFLOW_CATALOG`. ⚠️ It is deliberately **not** defaulted to a
path on the author's machine: this repository is public, and `.gitignore` says outright that
absolute paths and environment details do not belong in it.

⚠️ The `productImage` URLs point at the manufacturer's own assets. This index records them as
**internal reference** — enough to look at when drawing, never to be published as a film's picture.

  python scripts/index-device-catalog.py --catalog "/path/to/brainmed.json"
"""
import collections
import datetime
import json
import os
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "docs"

# The categories the film group actually asked for, in the order their complaints came in.
FOCUS = ["微导管", "微导丝", "弹簧圈", "取栓支架", "血流导向密网支架", "辅助支架",
         "球囊扩张导管", "抽吸导管", "导引导管", "远端通路导管", "动脉瘤辅助球囊封堵导管"]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=os.environ.get("NEUROFLOW_CATALOG", ""),
                    help="NeuroFlow 的 device-catalog JSON；也可用环境变量 NEUROFLOW_CATALOG 指定")
    args = ap.parse_args()
    if not str(args.catalog).strip():
        print("需要 --catalog <brainmed.json>（或设 NEUROFLOW_CATALOG）："
              "脚本不猜路径，因为仓库是公开的，个人绝对路径不该写进来", file=sys.stderr)
        return 2
    catalog = Path(args.catalog).expanduser()
    if not catalog.exists():
        print(f"catalog not found: {catalog}", file=sys.stderr)
        return 2
    cat = json.loads(catalog.read_text(encoding="utf-8"))
    items = cat.get("items") or []
    if not items:
        print("catalog has no items", file=sys.stderr)
        return 2

    by_cat = collections.Counter(i.get("category") for i in items)
    by_maker = collections.Counter(i.get("manufacturer") for i in items)
    newest = collections.Counter(i["category"] for i in items if i.get("newest"))
    makers_of = collections.defaultdict(collections.Counter)
    for i in items:
        makers_of[i.get("category")][i.get("manufacturer")] += 1

    today = datetime.date.today().isoformat()
    out = [
        f"# 神经介入器械参考索引（{today}）",
        "",
        f"来源：**{(cat.get('source') or {}).get('name')}** —— "
        f"`{(cat.get('source') or {}).get('url')}`",
        f"抓取时间：{cat.get('fetchedAt')} · 源站总数 {cat.get('sourceTotal')} · "
        f"本地条目 **{len(items)}** · 厂商 **{len(by_maker)}** 家",
        "",
        "**这份文件给「绘图专家」用**：画某个器械之前，先在这里查它属于哪一类、有哪些真实产品可对照。",
        "",
        "⚠️ **版权**：`productImage` 指向厂商自己的产品图。它们是**内部参考**——画的时候可以看，",
        "**不能**直接当镜头画面发布。要发布就得自己画，而「画得像」的前提正是看过这些参考图。",
        "",
        "## 一、分类学：神经介入到底有哪些器械",
        "",
        "| 类别 | 产品数 | 其中新品 | 主要厂商（前三） |",
        "|---|---|---|---|",
    ]
    for c, n in by_cat.most_common():
        top = "、".join(f"{m}({k})" for m, k in makers_of[c].most_common(3))
        out.append(f"| {c} | {n} | {newest[c]} | {top} |")

    out += ["", f"共 **{len(by_cat)}** 个类别。", ""]

    missing = [c for c in FOCUS if c not in by_cat]
    if missing:
        out += [f"⚠️ 本目录里**没有**这几类（说明它们要么归在别的名字下，要么没被收录）："
                f"{'、'.join(missing)}", ""]

    out += ["## 二、按类别看代表产品（画之前先看这些）", ""]
    for name in FOCUS:
        rows = [i for i in items if i.get("category") == name]
        if not rows:
            continue
        rows.sort(key=lambda i: (not i.get("hot"), not i.get("newest"), i.get("name") or ""))
        out.append(f"### {name}（{len(rows)} 个）")
        out.append("")
        out.append("| 产品 | 厂商 | 参考图 |")
        out.append("|---|---|---|")
        for i in rows[:8]:
            img = i.get("productImage") or ""
            out.append(f"| {i.get('name')} | {i.get('manufacturer')} | {'[图](' + img + ')' if img else '—'} |")
        out.append("")

    out += ["## 三、厂商 × 类别（谁做什么）", ""]
    for m, n in by_maker.most_common(15):
        cats = collections.Counter(i["category"] for i in items if i.get("manufacturer") == m)
        out.append(f"- **{m}**（{n}）: " + "、".join(f"{c}×{k}" for c, k in cats.most_common()))
    out.append("")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / f"neuro-device-catalog-index-{today}.md"
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {dest} ({dest.stat().st_size} bytes) · "
          f"{len(items)} items · {len(by_cat)} categories · {len(by_maker)} makers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
