#!/usr/bin/env python3
"""把 Obsidian Vault 里白名单内的资料灌进知识库。

    python3 scripts/ingest-vault.py --whitelist ~/.team-agent/vault-whitelist.json --only neuroangio

设计上的四个决定，每个都对应一个会出问题的地方：

* **白名单是唯一入口。** 要灌什么写在 JSON 里（含每一条的理由），这个脚本只认路径。
  把整个 Vault 丢进来会让 USMLE 题库和《地毯》笔记进检索结果 —— 那是「为什么搜不到」有据可查的反面。
* **`filename` 存绝对路径，这是多模态的开关。** `Library.figures()` 不是索引出来的，而是
  每次从磁盘上读笔记、解析它的 `![](attachments/x.png)` / `![[x.jpg]]`。所以只要这个路径是真的，
  检索命中之后就能把图谱调出来 —— 不需要为任何一张图调用视觉模型。
* **重跑是替换，不是追加。** 先 `find_by_title` 拿 `did` 再传进去（`store.add_doc(did=…)` 会先删旧行
  再插入、保留 enabled）。否则灌两次就有两份，检索结果里同一条出现两遍，而且删不掉。
* **文件先过滤再读。** 跳过大文件和 .obsidian/.git，并且在读之前就按后缀判断 ——
  25000 个文件的 Vault 里，一次误扫的代价是分钟级的。
* **向量是第二步，不是第一步。** 文字先进库（几秒），再按需补向量（每段一次嵌入）。两者分开，
  是因为向量那步会跑几分钟到几小时，而它失败时文字那一半必须已经好好地在库里了。

⚠️ 它写的是 `TEAM_AGENT_DATA` 指向的数据目录。**先在克隆目录上跑**（见 README 的测试约定），
别对着真库做第一次实验。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import embed  # noqa: E402
from app.library import MAX_CHARS, Library, LibraryError, _decode, note_figures  # noqa: E402
from app.store import Store  # noqa: E402

SKIP_DIRS = {".obsidian", ".git", ".trash", ".smart-env", "node_modules", "__pycache__"}
# 一个 markdown 笔记超过这个大小就不是笔记了（多半是导出的数据或日志）。
MAX_NOTE_BYTES = 2_000_000

# 白名单里的层级 -> (键名, 取哪个字段)。tier3 的「试点」和「全量候选」都能灌，但**灌了会发现
# 它们没有文字层** —— 这正是要的结果：让「需要 OCR」变成报告里的一行，而不是一个静默的空文档。
TIERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tier1": ("tier1_markdown_核心", ("roots",)),
    "tier2": ("tier2_pdf_文字层", ("files",)),
    "tier3": ("tier3_扫描件_需OCR", ("pilot", "全量候选")),
}

# 超过这个比例的替换字符/控制字符就判定「编码坏了」。正经英文或中文正文基本是 0；
# 编码坏掉的 PDF 常见 5%~30%，所以 1% 留的余量很大，不会误伤。
GARBLE_LIMIT = 0.01


def notes_under(root: Path):
    """白名单里一个目录下的所有 markdown，路径已排序（可重跑、可比对）。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("..")]
        for fn in filenames:
            if Path(fn).suffix.lower() not in (".md", ".markdown"):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > MAX_NOTE_BYTES:
                    continue
            except OSError:
                continue
            out.append(p)
    out.sort()
    return out


def title_of(path: Path, text: str) -> str:
    """笔记标题：第一个 `# 标题`，否则用文件名。

    Frontmatter 里的 `title:` 更权威，但它在 `---` 块内、常常带引号；`note_figures` 已经会把
    frontmatter 解析出来，这里就不重复实现一遍解析器了 —— 标题只用于去重与展示。
    """
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:200]
        if s and not s.startswith("---") and not s.startswith(">") and not s.startswith("!"):
            break
    return path.stem[:200]


async def embed_pass(store: Store, lib: Library, kb_id: str, data_dir: str, *,
                     max_chunks: int = 0) -> dict:
    """给还没有向量的段落补上向量。

    先确认模型服务在跑；不在就按设置**启动一次并等它加载完**（同机实测：冷启动约 13 秒，第一次
    下载 2.3 GB 权重则是分钟级 —— 而下载这一步永远不会自己发生，`--allow-download` 才允许）。

    服务起不来就明说并跳过。文字那一半已经好好在库里了：空着向量只让「按意思搜」不可用，
    而假装嵌过了会让「为什么搜不到」变成一个查不出来的问题。
    """
    settings = store.get_settings()
    if not settings.get("embed_enabled", True):
        print("\n向量嵌入：设置里已关闭，跳过。")
        return {"skipped": "disabled"}
    print("\n检查向量模型服务 %s …" % embed.base_url(settings))
    st = await embed.ensure(settings, data_dir=data_dir, wait=300)
    if not st.get("ready"):
        why = st.get("error") or st.get("reason") or st.get("state")
        print("！向量嵌入跳过：模型服务没就绪（%s）" % why)
        print("  照这条启动它：%s" % embed.fetch_command(settings))
        return {"skipped": str(why)}
    print("向量模型：%s @ %s（%s，加载 %.1f 秒）" % (
        st.get("model"), embed.base_url(settings), st.get("device"), float(st.get("load_seconds") or 0)))
    began = time.time()
    tick = [began]

    def progress(done: int, total: int) -> None:
        now = time.time()
        if now - tick[0] < 5 and done < total:
            return
        tick[0] = now
        rate = done / max(0.001, now - began)
        print("  已嵌入 %d/%d 段（%.1f 段/秒，约剩 %.0f 秒）" % (
            done, total, rate, (total - done) / rate if rate else 0), flush=True)

    report = await lib.index_vectors(kb_docs(lib, kb_id), progress=progress, max_chunks=max_chunks)
    print("向量嵌入：%d 段 / %d 篇，用时 %.1f 秒，还剩 %d 段%s" % (
        report["indexed"], report["documents"], report["seconds"], report["left"],
        "；错误：" + report["errors"][0] if report["errors"] else ""))
    return report


def kb_docs(lib: Library, kb_id: str) -> list[str]:
    """这个知识库里启用中的文档 id —— 只看这一批，别把整个库都嵌一遍。"""
    return [d["id"] for d in lib.store.list_docs(kb_id) if d["enabled"]]


def by_path(store: Store, kb_id: str) -> dict[str, dict]:
    """知识库里**路径 → 文档**的一张表，摄入时用它认人。

    标题不是身份：两篇笔记叫同一个名字是常事（同一篇笔记的两个版本、同一主题的两篇病例），而按标题
    找只会拿到第一份 —— 于是另一份被当成新文档灌进去，每次重跑多一份，检索结果里同一页出现两三遍。
    """
    out: dict[str, dict] = {}
    for d in store.list_docs(kb_id):
        if d.get("filename"):
            out.setdefault(d["filename"], d)
    return out


def drop_duplicates(lib: Library, store: Store, kb_id: str) -> int:
    """同一个路径留下最完整的那一份，其余删掉；返回删了几份。

    只在启动时做一次、且按数据驱动（不看「是否跑过」的标记）：历史上被重复灌进来的那些行不会自己
    消失，而它们会让检索结果里同一页出现多次 —— 那看起来像检索不准，其实是库里有重复。保留的那份
    按「有向量 > 段落多 > 新」挑，因为这三样正是重灌一遍要花时间的东西。
    """
    groups: dict[str, list[dict]] = {}
    for d in store.list_docs(kb_id):
        if d.get("filename"):
            groups.setdefault(d["filename"], []).append(d)
    gone = 0
    for path, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda d: (0 if store.doc_has_vectors(d["id"]) else 1,
                                 -int(d.get("chunks") or 0), -float(d.get("created_at") or 0)))
        for d in rows[1:]:
            lib.delete(d["id"])
            gone += 1
    return gone


def pdf_pages(path: Path) -> list[str]:
    """A PDF's text, page by page — streamed from disk, not from a bytes copy in memory.

    Page by page rather than one blob because two decisions need the boundaries: a book longer than
    the library's own limit has to be split *somewhere*, and the only honest place to split it is a
    page boundary — the parts then still carry a page range as their provenance.

    Streamed because the file is what it is: a scanned textbook can be a few hundred megabytes, and
    reading it once already costs that much. (A first version read the bytes and then let pypdf read
    them again, and the run was killed part way through a large book.)

    Extracted here rather than through `Library.extract_text` for one reason: a scanned book has to be
    *named*, not skipped. The library's extractor raises "no extractable text" and the caller cannot
    tell that apart from a broken file — and the difference matters, because one of them needs OCR (a
    decision with a price) and the other needs a person. The page count falls out of this for free,
    and that is what an OCR estimate is made of.
    """
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)   # one warning per undecodable font, per page
    with path.open("rb") as fh:
        reader = PdfReader(fh)
        return [((p.extract_text() or "").strip()) for p in reader.pages]


def looks_garbled(text: str) -> float:
    """Share of characters that came out as replacement or unprintable code points.

    Worth looking at because a PDF whose fonts lack a proper encoding extracts to text that reads
    like static — and static indexes and retrieves just as confidently as prose does. A library full
    of it is worse than a library missing the book: a search result gives no hint that the passage is
    unreadable, and the reader who trusts it is the one who is misled.
    """
    if not text:
        return 0.0
    bad = sum(1 for c in text if c == "\ufffd" or (ord(c) < 32 and c not in "\n\t\r"))
    return bad / len(text)


def split_pages(pages: list[str], limit: int) -> list[tuple[int, int, str]]:
    """A book cut into parts no longer than `limit` characters: [(first page, last page, text)].

    The limit is the library's own (`library.MAX_CHARS`), and a 1343-page textbook exceeds it. Letting
    the whole book be refused would mean the most systematic source in the collection never enters the
    index; cutting it at arbitrary character offsets would mean a passage could name a page range it
    is not in. Cutting on page boundaries costs nothing and keeps the provenance honest.
    """
    out: list[tuple[int, int, str]] = []
    start, buf, size = 1, [], 0
    for i, page in enumerate(pages, 1):
        if size and size + len(page) > limit:
            out.append((start, i - 1, "\n\n".join(buf)))
            start, buf, size = i, [], 0
        buf.append(page)
        size += len(page) + 2
    if buf:
        out.append((start, max(1, len(pages)), "\n\n".join(buf)))
    return out


def plan_of(spec: dict, tiers: list[str]) -> tuple[list[dict], list[dict]]:
    """(markdown trees, individual documents) for the tiers asked for, in the order given.

    Two shapes because the whitelist has two: a tier is either a *tree* of notes to walk, or a list of
    named files that were each checked by hand for a text layer. Reading them the same way would mean
    walking a whole book folder and quietly indexing the scans as zero-length documents.
    """
    trees: list[dict] = []
    files: list[dict] = []
    for tier in tiers:
        key, fields = TIERS.get(tier, ("", ()))
        body = spec.get(key) or {}
        if not body:
            print("！白名单里没有 %s（%s）" % (tier, key), file=sys.stderr)
            continue
        for field in fields:
            for item in body.get(field) or []:
                if not item.get("path"):
                    continue
                (trees if field == "roots" else files).append({**item, "tier": tier})
    return trees, files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--whitelist", default=str(Path.home() / ".team-agent" / "vault-whitelist.json"))
    ap.add_argument("--tier", default="tier1,tier2",
                    help="要灌哪几层（tier1=markdown 目录, tier2=有文字层的 PDF, tier3=扫描件试点）")
    ap.add_argument("--only", default="", help="只灌路径里含这个片段的 root（调试用）")
    ap.add_argument("--limit", type=int, default=0, help="每个 root 最多灌几篇（调试用）")
    ap.add_argument("--report", default="", help="把结果写成 JSON")
    ap.add_argument("--no-embed", action="store_true", help="只灌文字，不补向量")
    ap.add_argument("--embed-limit", type=int, default=0, help="最多嵌入几段（先试点）")
    args = ap.parse_args()

    spec = json.loads(Path(args.whitelist).expanduser().read_text(encoding="utf-8"))
    vault = Path(spec["vault"]).expanduser()
    if not vault.is_dir():
        print("！Vault 不存在：%s" % vault, file=sys.stderr)
        return 2

    tiers = [t.strip() for t in args.tier.split(",") if t.strip()]
    roots, files = plan_of(spec, tiers)
    if args.only:
        roots = [r for r in roots if args.only in r["path"]]
        files = [f for f in files if args.only in f["path"]]

    data_dir = os.environ.get("TEAM_AGENT_DATA") or str(Path.home() / ".team-agent")
    store = Store(data_dir)
    lib = Library(store)
    kb = lib.shared_kb(create=True)
    print("数据目录 : %s" % data_dir)
    print("知识库   : %s (%s)" % (kb["name"], kb["id"]))
    print("Vault    : %s" % vault)
    print("层级     : %s（%d 个 markdown 目录 + %d 个文件）\n" % (", ".join(tiers), len(roots), len(files)))
    if args.only and not (roots or files):
        print("！--only %r 没有匹配任何条目" % args.only, file=sys.stderr)
        return 2

    added = replaced = failed = removed = 0
    figures_total = 0
    notes_with_figures = 0
    needs_ocr: list[dict] = []
    garbled: list[dict] = []
    t0 = time.time()
    per_root: list[dict] = []

    for r in roots:
        base = vault / r["path"]
        if not base.is_dir():
            print("  [跳过] %s（不是目录）" % r["path"])
            continue
        notes = notes_under(base)
        if args.limit:
            notes = notes[: args.limit]
        # 一个笔记的身份是它的**路径**，不是标题。按标题找会在两篇笔记同名时找错人，而找错的后果是
        # 「每次重跑又多一份」——实测 585 篇里有 28 篇这样被灌成了两份，检索结果里同一页出现两三次。
        index = by_path(store, kb["id"])
        removed += drop_duplicates(lib, store, kb["id"])
        index = by_path(store, kb["id"])
        n_new = n_rep = 0
        for p in notes:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                failed += 1
                print("  [读失败] %s: %s" % (p, e))
                continue
            if not text.strip():
                continue
            title = title_of(p, text)
            # 内嵌的图在这里就登记下来（不调用任何模型）：它决定这条笔记能不能「多模态」。
            try:
                figs, _meta = note_figures(str(p))
            except Exception:  # noqa: BLE001 — 图只是附加信息，取不到不该让笔记进不了库
                figs = []
            if figs:
                figures_total += len(figs)
                notes_with_figures += 1
            # 重跑即替换：按**路径**找到自己那一份，把它的 id 交给 add_text。
            prev = index.get(str(p))
            did = prev["id"] if prev else None
            try:
                lib.add_text(title=title, text=text, filename=str(p), kind="note",
                             size=len(text.encode()), did=did, kb_id=kb["id"])
            except LibraryError as e:
                failed += 1
                print("  [拒绝] %s: %s" % (p.name, e))
                continue
            if did:
                replaced += 1
                n_rep += 1
            else:
                added += 1
                n_new += 1
        per_root.append({"root": r["path"], "files": len(notes), "added": n_new,
                         "replaced": n_rep})
        print("  %-52s %4d 篇（新增 %d / 替换 %d）" % (r["path"][:52], len(notes), n_new, n_rep))

    # ---- 单个文件（tier2 的 PDF、tier3 的试点）：一本一本点名，扫不出来就说出来
    index = by_path(store, kb["id"])
    files_in: list[dict] = []
    for f in files:
        p = vault / f["path"]
        if not p.is_file():
            failed += 1
            print("  [不在] %s" % f["path"])
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            failed += 1
            print("  [读失败] %s: %s" % (f["path"], e))
            continue
        if p.suffix.lower() == ".pdf":
            try:
                pages = pdf_pages(p)
            except Exception as e:  # noqa: BLE001 — pypdf 的花样够多，报出来比崩掉强
                failed += 1
                print("  [解析失败] %s: %s: %s" % (p.name, type(e).__name__, e))
                continue
            text = "\n\n".join(pg for pg in pages if pg).strip()
            if not text:
                # 扫描件：**明说、不静默跳过**。它需要 OCR，而那是一个要花钱花时间的决定，
                # 也得让「为什么这本书搜不到」有一个写在报告里的答案。
                needs_ocr.append({"path": f["path"], "pages": len(pages), "chars": 0, "tier": f["tier"]})
                print("  [需要 OCR] %-56s %4d 页，0 字符" % (p.name[:56], len(pages)))
                continue
            garble = looks_garbled(text)
            if garble > GARBLE_LIMIT:
                # 编码坏了：抽出来的是「雪花」。它和正经文字一样能被检索到、被引用，所以宁可
                # 把它挡在库外并点名，也不要让一条读不通的「出处」进到临床答案里。
                garbled.append({"path": f["path"], "pages": len(pages), "chars": len(text),
                                "garbled": round(garble, 4), "tier": f["tier"]})
                print("  [乱码 %.1f%%] %-48s %4d 页，%d 字符" % (garble * 100, p.name[:48], len(pages), len(text)))
                continue
            parts = split_pages(pages, MAX_CHARS)
            kind = "pdf"
            for first, last, part in parts:
                ident = str(p) if len(parts) == 1 else "%s#p%d-%d" % (p, first, last)
                title = p.stem[:180] if len(parts) == 1 else "%s（第 %d-%d 页）" % (p.stem[:160], first, last)
                prev = index.get(ident)
                did = prev["id"] if prev else None
                try:
                    lib.add_text(title=title, text=part, filename=ident, kind=kind, size=size,
                                 did=did, kb_id=kb["id"])
                except LibraryError as e:
                    failed += 1
                    print("  [拒绝] %s: %s" % (title[:40], e))
                    continue
                if did:
                    replaced += 1
                else:
                    added += 1
            # 切成几份之后再改一次文本层长度，份数可能变少：旧的那一份要走，否则同一页会在结果里
            # 出现两次，而「重复」正是上一轮踩过的坑（28 篇同名笔记被灌成两份）。
            stale = [d for d in store.list_docs(kb["id"])
                     if str(d.get("filename") or "").startswith(str(p) + "#p")
                     and str(d.get("filename")) not in {("%s#p%d-%d" % (p, a, b)) for a, b, _ in parts}]
            for d in stale:
                lib.delete(d["id"])
                removed += 1
            note = "%d 页 / %d 字符%s" % (len(pages), len(text),
                                          "" if len(parts) == 1 else "（拆成 %d 份）" % len(parts))
            print("  %-56s %-24s" % (p.name[:56], note))
            files_in.append({"path": f["path"], "title": p.stem, "kind": kind, "note": note,
                             "parts": len(parts)})
            del pages, text
            continue
        else:
            text = _decode(p.read_bytes())
            if not text.strip():
                failed += 1
                print("  [空] %s" % f["path"])
                continue
            kind, title, note = "note", title_of(p, text), "%d 字符" % len(text)
        prev = index.get(str(p))
        did = prev["id"] if prev else None
        try:
            lib.add_text(title=title, text=text, filename=str(p), kind=kind, size=size,
                         did=did, kb_id=kb["id"])
        except LibraryError as e:
            failed += 1
            print("  [拒绝] %s: %s" % (p.name, e))
            continue
        if did:
            replaced += 1
        else:
            added += 1
        files_in.append({"path": f["path"], "title": title, "kind": kind, "note": note,
                         "replaced": bool(did)})
        print("  %-56s %-22s %s" % (p.name[:56], note, "替换" if did else "新增"))

    took = time.time() - t0
    print("\n共 %d 篇：新增 %d，替换 %d，失败 %d，清理重复 %d，用时 %.1f 秒" % (
        added + replaced, added, replaced, failed, removed, took))
    print("带内嵌图的笔记 %d 篇，共登记 %d 张图（未调用任何视觉模型）" % (notes_with_figures, figures_total))
    if needs_ocr:
        pages = sum(int(n["pages"]) for n in needs_ocr)
        print("！%d 个文件没有文字层（共 %d 页），**需要 OCR 才能进检索** —— 它们不在库里，不是「搜不到」："
              % (len(needs_ocr), pages))
        for n in needs_ocr[:8]:
            print("     %s（%d 页）" % (n["path"], n["pages"]))
        if len(needs_ocr) > 8:
            print("     …等共 %d 个" % len(needs_ocr))
    if garbled:
        print("！%d 个文件抽出来的正文是乱码（编码坏了），**没有入库** —— 它们会像正经文字一样被检索到、"
              "被引用，所以宁可挡在外面:" % len(garbled))
        for n in garbled:
            print("     %s（乱码 %.1f%%）" % (n["path"], n["garbled"] * 100))

    docs = store.list_docs(kb["id"]) if hasattr(store, "list_docs") else []
    print("知识库文档数：%d" % (len(docs) or (added + replaced)))

    vectors: dict = {}
    if not args.no_embed:
        vectors = asyncio.run(embed_pass(store, lib, kb["id"], data_dir,
                                        max_chunks=args.embed_limit))
    cov = store.vector_coverage()
    print("段落 %d 段，其中 %d 段有向量（%d 段待补）" % (cov["chunks"], cov["with_vectors"], cov["missing"]))

    if args.report:
        Path(args.report).write_text(json.dumps(
            {"tiers": tiers, "added": added, "replaced": replaced, "failed": failed,
             "duplicates_removed": removed,
             "figures": figures_total, "notes_with_figures": notes_with_figures,
             "seconds": round(took, 1), "roots": per_root,
             "documents": files_in, "needs_ocr": needs_ocr, "garbled": garbled,
             "vectors": vectors, "coverage": cov}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
