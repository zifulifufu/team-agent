#!/usr/bin/env python
"""把即梦（jimeng.jianying.com）的公开作品收进知识库，做成「创意大师」的底料。

**收的是提示词和风格标签，不是别人的成片。** 这个区别是整件事的立场：要学的是「什么样的词
让画面变成那样」，而不是把别人的作品攒成一堆。每一条笔记都带 `source:` 指向它在即梦上的
原始页面，图只作为该条提示词的**参考画面**存在同一个目录里。

为什么走这个接口：即梦的 explore **列表页**是客户端渲染的，匿名拿到的 `_SSR_DATA` 是空的；
但页面自己用来加载 feed 的那个接口是公开的、不需要登录：

    GET /jsonp/mweb/v1/get_explore?category_id=<id>&feed_refer=feed_enterauto&_callback=cb

⚠️ 两个必须知道的坑（实测）：
  * **少了 `feed_refer` 参数会 `ret:1000 invalid parameter`**，看不出是缺参数。
  * 图片直链带 `x-expires` 签名，**会过期**。所以图是下载到本地留着的，不是存个链接。

站点的 `robots.txt` 是 `User-Agent: * / Allow: /`，并主动声明了 sitemap —— 抓公开页面是被
允许的。但这**不改变作品归作者所有**这件事，所以：笔记里保留作者与出处，别把图片重新发布，
也别把它当素材直接用在别人的成片里。

用法：

    <repo>/.venv/bin/python scripts/ingest-jimeng.py --pages 3
    <repo>/.venv/bin/python scripts/ingest-jimeng.py --pages 20 --report out.json

⚠️ 它写 `TEAM_AGENT_DATA` 指向的数据目录。**先在克隆目录上跑**（见 README 的测试约定）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

from app import embed  # noqa: E402
from app.library import Library  # noqa: E402
from app.store import Store  # noqa: E402

HOST = "https://jimeng.jianying.com"
FEED = "/jsonp/mweb/v1/get_explore"
DETAIL = HOST + "/ai-tool/work-detail/%s?itemType=9&workDetailType=Image"
# A browser UA, and that is all: the endpoint needs no cookie, no token and no signature.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
PAUSE = 0.4                 # between requests: this is somebody's site, not a dataset
TIMEOUT = 30.0
# Which of the cover's sizes to keep. 1080 is big enough to read a composition off and small enough
# that a few hundred of them stay a few hundred megabytes; 4096 is neither.
WANT_SIZES = ("1080", "720", "2400", "480", "360", "4096")


# ------------------------------------------------------------------ the feed
def jsonp(text: str) -> dict:
    """The object out of `window["__get_explore_result"] = {…}`.

    Parsed by bracket matching rather than a regex: the payload contains braces inside strings
    (a draft's `content` is itself JSON), and a greedy pattern silently returns a truncated object —
    which then looks like "the site changed its format" instead of "my parser is wrong".
    """
    i = text.find("{")
    if i < 0:
        raise ValueError("no JSON object in the reply")
    depth, j, instr, esc = 0, i, False, False
    while j < len(text):
        c = text[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[i:j + 1])
        j += 1
    raise ValueError("the JSON object in the reply is not closed")


def feed(client: httpx.Client, category: str, cursor: str = "") -> dict:
    params = {"category_id": category, "feed_refer": "feed_enterauto", "_callback": "cb"}
    if cursor:
        params["cursor"] = cursor
    r = client.get(HOST + FEED, params=params, headers={"user-agent": UA,
                                                        "referer": HOST + "/ai-tool/explore"})
    r.raise_for_status()
    got = jsonp(r.text)
    # `ret` comes back as the **string** "0", not the number 0 — comparing it to `0` fails and reads
    # as "the feed refused", which is exactly the kind of mistake that gets blamed on the site.
    if str(got.get("ret", "0")).strip() not in ("0", "", "None"):
        raise RuntimeError(f"the feed answered ret={got.get('ret')} errmsg={got.get('errmsg')!r}")
    return got.get("data") or {}


# ------------------------------------------------------------------ one work -> one note
def prompt_of(item: dict) -> str:
    """The words that made the picture. Two places hold it and either may be the only one there.

    `text2image_params.prompt` is the generation prompt; `reference_prompt` is what a *template*
    ("一键同款") carries. Both are the thing worth learning from; a work with neither is a picture
    with no lesson in it and is skipped rather than stored as an empty note.
    """
    aigc = (item.get("aigc_image_params") or {})
    t2i = (aigc.get("text2image_params") or {})
    return str(t2i.get("prompt") or aigc.get("reference_prompt") or "").strip()


def cover_of(item: dict) -> str:
    ca = item.get("common_attr") or {}
    sizes = ca.get("cover_url_map") or {}
    for want in WANT_SIZES:
        if sizes.get(want):
            return str(sizes[want])
    return str(ca.get("cover_url") or "")


def work_id_of(item: dict) -> str:
    ca = item.get("common_attr") or {}
    return str(ca.get("id") or ca.get("published_item_id") or "").strip()


def note_of(item: dict, image_rel: str) -> tuple[str, str]:
    """(title, markdown) for one work. The order is the order somebody learns from it.

    Prompt first and in a fenced block, so it can be copied out and edited rather than read as
    prose. Then what makes it that way (tags, the template it came from), then the picture, then
    where it came from. `source:` is in the frontmatter so the knowledge base can cite it.
    """
    ca = item.get("common_attr") or {}
    author = item.get("author") or {}
    si = item.get("sharing_info") or {}
    stats = item.get("statistic") or {}
    wid = work_id_of(item)
    prompt = prompt_of(item)
    tags = [str(t.get("name")) for t in (si.get("hash_tags") or []) if t.get("name")]
    effect = ""
    try:
        effect = str(json.loads(ca.get("metadata_param") or "{}").get("effect_id") or "")
    except (ValueError, AttributeError):
        effect = ""
    kinds = [f.get("type") for f in ((item.get("ai_feature") or {}).get("features") or [])
             if f.get("type")]
    title = str(ca.get("title") or "").strip() or (prompt[:24] if prompt else wid)
    lines = [
        "---",
        f"source: {DETAIL % wid}" if wid else "source: (unknown)",
        f"author: {author.get('name') or author.get('uid') or '(unknown)'}",
        f"tags: [{', '.join(tags)}]" if tags else "tags: []",
        f"kind: {', '.join(kinds)}" if kinds else "kind: (unknown)",
    ]
    if effect:
        lines.append(f"template_id: {effect}")
    lines += ["---", "", f"# {title}", ""]
    if tags:
        lines.append("**标签**：" + "、".join(tags))
        lines.append("")
    lines += ["**提示词**", "", "```", prompt or "(这条没有提示词)", "```", ""]
    if effect:
        lines.append(f"**出自模板/特效**：`{effect}`（即梦里的「一键同款」）")
        lines.append("")
    if image_rel:
        lines += ["**参考画面**", "", f"![]({image_rel})", ""]
    if stats:
        lines.append("**热度**：播放 %s · 使用 %s · 收藏 %s" % (
            stats.get("play_num", "-"), stats.get("usage_num", "-"), stats.get("favorite_num", "-")))
        lines.append("")
    lines += [
        "**怎么用**：这条是**提示词的例子**，不是可复用的素材。学的是它把风格、光线、构图、材质"
        "都写成了具体的词。要照它的路子写，就把那些词换成本次要讲的内容；画面本身请自己生成。",
        "",
    ]
    return title, "\n".join(lines)


# ------------------------------------------------------------------ running it
def fetch_images(client: httpx.Client, items: list[dict], attach: Path, *, want: bool) -> dict:
    """Download each work's cover. Returns `{work_id: relative path}`.

    The links carry an `x-expires` signature, so a note that stored the URL instead of the bytes
    would have a broken picture within days — and a knowledge base whose figures silently 404 is one
    nobody can check a claim against.
    """
    got: dict = {}
    if not want:
        return got
    attach.mkdir(parents=True, exist_ok=True)
    for item in items:
        wid = work_id_of(item)
        url = cover_of(item)
        if not wid or not url:
            continue
        dest = attach / f"{wid}.jpg"
        if dest.is_file() and dest.stat().st_size > 0:
            got[wid] = f"attachments/{wid}.jpg"
            continue
        try:
            r = client.get(url, headers={"user-agent": UA})
            r.raise_for_status()
        except (httpx.HTTPError, OSError) as e:
            print("   [图取不到] %s: %s" % (wid, type(e).__name__))
            continue
        dest.write_bytes(r.content)
        got[wid] = f"attachments/{wid}.jpg"
        time.sleep(PAUSE)
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3, help="抓几页 feed（每页约 19 条）")
    ap.add_argument("--limit", type=int, default=0, help="最多收几条（调试用）")
    ap.add_argument("--category-id", default="11222", help="即梦的作品分类 id，默认 11222（绘画）")
    ap.add_argument("--out", default="", help="笔记目录，默认 <数据目录>/jimeng-notes")
    ap.add_argument("--kb", default="", help="灌进哪个知识库（默认共享知识库）")
    ap.add_argument("--no-images", action="store_true", help="只收提示词，不下图")
    ap.add_argument("--no-embed", action="store_true", help="只灌文字，不补向量")
    ap.add_argument("--report", default="", help="把结果写成 JSON")
    args = ap.parse_args()

    data_dir = os.environ.get("TEAM_AGENT_DATA") or str(Path.home() / ".team-agent")
    out = Path(args.out).expanduser() if args.out else Path(data_dir) / "jimeng-notes"
    out.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir)
    lib = Library(store)
    kb = lib.find_by_title(args.kb) if args.kb else None
    if args.kb and not kb:
        print("！没有叫「%s」的知识库" % args.kb, file=sys.stderr)
        return 2
    if not kb:
        kb = lib.shared_kb(create=True) or {}
    print("数据目录 : %s" % data_dir)
    print("知识库   : %s (%s)" % (kb.get("name"), kb.get("id")))
    print("笔记目录 : %s\n" % out)

    existing = {str(d.get("filename") or ""): d["id"] for d in store.list_docs(kb["id"])}

    added = replaced = skipped = no_prompt = 0
    prompts: list[str] = []
    t0 = time.time()
    cursor = ""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for page in range(1, max(1, args.pages) + 1):
            try:
                data = feed(client, args.category_id, cursor)
            except (httpx.HTTPError, RuntimeError, ValueError) as e:
                print("！第 %d 页取不到：%s: %s" % (page, type(e).__name__, e), file=sys.stderr)
                break
            items = data.get("item_list") or []
            if not items:
                print("第 %d 页没有作品，停。" % page)
                break
            # Which of this page we are actually keeping is decided **before** anything is
            # downloaded: fetching the cover of a work that is about to be skipped spends somebody
            # else's bandwidth on nothing.
            keep: list[dict] = []
            for item in items:
                if args.limit and added + replaced + len(keep) >= args.limit:
                    break
                if not work_id_of(item):
                    skipped += 1
                    continue
                if not prompt_of(item):
                    no_prompt += 1
                    continue
                keep.append(item)
            images = fetch_images(client, keep, out / "attachments", want=not args.no_images)
            for item in keep:
                wid = work_id_of(item)
                title, body = note_of(item, images.get(wid, ""))
                path = out / f"{wid}.md"
                path.write_text(body, encoding="utf-8")
                did = existing.get(str(path))
                try:
                    lib.add_text(title, body, filename=str(path), kind="note", did=did,
                                 kb_id=kb["id"])
                except Exception as e:  # noqa: BLE001 — 一条坏笔记不该停下整轮
                    print("   [灌不进去] %s: %s: %s" % (wid, type(e).__name__, e))
                    skipped += 1
                    continue
                existing[str(path)] = did or ""
                prompts.append(prompt_of(item))
                if did:
                    replaced += 1
                else:
                    added += 1
            print("第 %d 页：看 %d 条，收下 %d 条（累计 新增 %d / 替换 %d / 无提示词 %d）"
                  % (page, len(items), len(keep), added, replaced, no_prompt))
            cursor = str(data.get("next_offset") or "")
            if not data.get("has_more"):
                break
            time.sleep(PAUSE)

    vectors: dict = {}
    if not args.no_embed and (added or replaced):
        try:
            vectors = asyncio.run(_embed(lib, store))
        except Exception as e:  # noqa: BLE001
            print("！补向量失败（文字已经在库里了）：%s: %s" % (type(e).__name__, e))

    took = time.time() - t0
    print("\n共收 %d 条：新增 %d，替换 %d；无提示词跳过 %d，其它跳过 %d；用时 %.1f 秒"
          % (added + replaced, added, replaced, no_prompt, skipped, took))
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"added": added, "replaced": replaced, "skipped": skipped,
             "no_prompt": no_prompt, "seconds": round(took, 1), "out": str(out),
             "kb": kb.get("id"), "vectors": vectors,
             "sample_prompts": prompts[:5]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n提示：这些是**提示词的例子**，不是素材。用它们当风格与写法参考，画面自己生成。")
    return 0


async def _embed(lib: Library, store: Store) -> dict:
    r = await lib.index_vectors()
    cov = store.vector_coverage()
    print("补向量：%d 段 / %d 篇，用时 %.1f 秒，还剩 %d 段"
          % (r["indexed"], r["documents"], r["seconds"], r["left"]))
    return {"indexed": r["indexed"], "left": r["left"], "chunks": cov["chunks"],
            "with_vectors": cov["with_vectors"], "model": embed.model_of(store.get_settings())}


if __name__ == "__main__":
    sys.exit(main())
