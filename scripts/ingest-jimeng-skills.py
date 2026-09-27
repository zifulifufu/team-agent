#!/usr/bin/env python
"""把即梦（jimeng.jianying.com）的**技能**收进知识库，给「创意大师」当运镜 / 分镜 / 导演的写法底料。

为什么是技能而不是作品：作品 feed（`ingest-jimeng.py`）一条只给「一张图 + 一条提示词」，
而**镜头语言怎么组织、分镜表怎么排、导演怎么调度**这类流程性的写法，在即梦上是以「技能」
（Agent 的 skill）的形式存在的 —— 每个技能带一份完整的 `instruction` 正文。实测「一图成片-电影广告
全能导演」正文 35163 字、「叙事短片导演分镜」22897 字，比一条作品提示词有用得多。

两个来源，都**匿名可取**（2026-09-25 实测，无需 Cookie、无需签名）：

    POST /mweb/v1/creation_agent/v2/skill/list          body {"source":1,…}
         → data.official_skills   官方预置技能（7 个：视频反解 / 创作分镜 / 全流程广告片导演 /
                                   影视故事短片 / 电商套图 / 海报设计 / Logo设计）
                                   只有标题、描述、引导语 —— 正文在服务端，不公开
    POST /mweb/v1/creation_agent/v2/skill/market/search body {"keyword":…,"isTest":false,"offset":0,"limit":N}
         → data.skills            市场技能，**含 `instruction` 正文** + 作者 + 使用次数 + 示范媒体

⚠️ 实测踩到的坑，都写在这里免得下次重踩：

  * **列表接口用不了**：`skill/market/list` 无论怎么传都报
    `invalid skill parameter: invalid source: 0`（`source` 是整数枚举，1/2 能过校验但 `skills` 恒为
    null）→ 所以这里是**按关键词逐个搜**，而不是拉全量列表。关键词表见 `KEYWORDS`。
  * **首页显示的名字 ≠ 市场里的名字**：「电影级长镜头运镜」在市场上叫「电影级长镜头」；
    「微表情导演」叫「AI演员微表情导演」；「电影广告全能导演」叫「一图成片-电影广告全能导演」。
    而「创作分镜」**根本不在市场里** —— 它是官方预置技能（`web_agent_skill_seedance_shot_design`）。
    所以两个来源都要，缺一个就会得出「即梦没有这个技能」的错误结论。
  * 一部分技能 `instruction` 是**空的**（逻辑在服务端）→ 照样收，但笔记里标明「正文不公开」，
    不假装它是一份可用的方法论。
  * `showcase_media` 的链接带签名、**会过期** → 不下载、不当素材，只记「它有示范片」这件事。
  * 技能**没有匿名可读的详情页**（`/ai-tool/skill/<id>` 是 SPA，SSR 里什么都没有）→ 出处只能记
    `skill_id` + 作者 + 在即梦首页「技能」里搜这个名字，不编造一个点不开的 URL。

立场：收的是**别人写的技能正文**（平台公开可见）。用途是学「它把镜头运动 / 分镜 / 调度写成了什么
样的可执行指令」。所以每条笔记都带作者与 `skill_id`，并且写到笔记末尾那句话上：**不要整段照抄
对外发布**，照它的路子重写。

用法：

    <repo>/.venv/bin/python scripts/ingest-jimeng-skills.py                 # 默认关键词，每词 20 条
    <repo>/.venv/bin/python scripts/ingest-jimeng-skills.py --per-keyword 40
    <repo>/.venv/bin/python scripts/ingest-jimeng-skills.py --keywords 运镜,分镜
    <repo>/.venv/bin/python scripts/ingest-jimeng-skills.py --no-embed      # 只灌文字

⚠️ 它写 `TEAM_AGENT_DATA` 指向的数据目录。**先在克隆目录上跑**（见 README 的测试约定）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

from app import embed  # noqa: E402
from app.library import Library, category_of  # noqa: E402
from app.store import Store  # noqa: E402

HOST = "https://jimeng.jianying.com"
SKILL_LIST = HOST + "/mweb/v1/creation_agent/v2/skill/list"
SKILL_SEARCH = HOST + "/mweb/v1/creation_agent/v2/skill/market/search"
HOME = HOST + "/ai-tool/home"

# A browser UA, and that is all: neither endpoint needs a cookie, a token or a signature.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
PAUSE = 0.4                 # between requests: somebody else's site, not a dataset
TIMEOUT = 30.0

# The capabilities that were asked for by name, plus the words that reach the same family. Every
# 抓回来的技能按「用处」归档(判定规则在 `app/library.py` 的 `category_of`,只有一份)。
# 这里只是把 token 换成中文打印出来 —— 界面上的词在 `desktop/src/i18n.tsx`。
CATEGORY_LABEL = {
    "camera": "运镜与镜头", "storyboard": "分镜", "performance": "表演与微表情",
    "director": "导演风格", "commercial": "广告与营销", "trailer": "预告片与片头",
    "effect": "特效与转场", "character": "角色与数字人", "story": "剧情与故事",
    "edit": "剪辑与配音", "design": "平面设计", "visual": "影像风格与美学",
    "": "未分类",
}

# one of these was checked against the live market; the misspellings matter ("长镜头" finds
# 电影级长镜头, "名导" finds 名导十五秒视频风格资产引擎).
KEYWORDS = (
    "电影级长镜头", "运镜", "分镜", "名导", "微表情",
    "广告导演", "电影级", "镜头语言", "预告片", "TVC",
)


# ------------------------------------------------------------------ the two sources
def _post(client: httpx.Client, url: str, body: dict) -> dict:
    r = client.post(url, json=body, headers={"user-agent": UA,
                                             "content-type": "application/json"})
    r.raise_for_status()
    data = r.json()
    if str(data.get("ret")) != "0":
        # `ret` is a **string** on this site — comparing it to 0 reads every valid reply as a failure.
        raise RuntimeError("即梦回绝了：ret=%s %s" % (data.get("ret"), data.get("errmsg")))
    return data.get("data") or {}


def official_skills(client: httpx.Client) -> list[dict]:
    """The platform's own skills — the ones the home page shows and the market cannot find."""
    data = _post(client, SKILL_LIST, {"source": 1, "tagList": [], "isActive": True,
                                      "isTest": False, "offset": 0, "limit": 50})
    out = []
    for s in (data.get("official_skills") or []):
        if not s.get("id") or not s.get("name"):
            continue
        out.append({
            "skill_id": str(s["id"]),
            "skill_name": str(s["name"]),
            "description": str(s.get("default_desc") or ""),
            "instruction": "",              # not published: the logic lives on their side
            "guide": str(s.get("default_guide_text") or ""),
            "author": "即梦AI（官方预置技能）",
            "usage": 0,
            "tags": [str(t) for t in (s.get("market_tag_list") or [])],
            "official": True,
            "has_showcase": bool(s.get("showcase_media")),
        })
    return out


def search_skills(client: httpx.Client, keyword: str, limit: int) -> list[dict]:
    """Market skills for one keyword. The only list endpoint that works anonymously."""
    data = _post(client, SKILL_SEARCH, {"keyword": keyword, "isTest": False,
                                        "offset": 0, "limit": max(1, limit)})
    out = []
    for s in (data.get("skills") or []):
        if not s.get("skill_id") or not s.get("skill_name"):
            continue
        who = (s.get("effective_user") or {})
        out.append({
            "skill_id": str(s["skill_id"]),
            "skill_name": str(s["skill_name"]),
            "description": str(s.get("description") or ""),
            "instruction": str(s.get("instruction") or ""),
            "guide": "",
            "author": str(who.get("name") or who.get("uid") or "(未署名)"),
            "usage": int(s.get("usage_count") or 0),
            "tags": list(s.get("tag_list") or []),
            "official": False,
            "has_showcase": bool(s.get("showcase_media")),
        })
    return out


def collect(client: httpx.Client, keywords: tuple[str, ...], per_keyword: int) -> tuple[list[dict], dict]:
    """Both sources, deduped by `skill_id`, official first then by how much it is actually used.

    Deduping here rather than at write time matters: ten keywords overlap heavily (「运镜」 and
    「电影级长镜头」 both return 格莱美运镜), and writing the same skill ten times would look like
    a knowledge base with ten of them.
    """
    seen: dict[str, dict] = {}
    stats = {"official": 0, "keywords": 0, "per_keyword": {}}
    for s in official_skills(client):
        seen.setdefault(s["skill_id"], s)
        stats["official"] += 1
    for kw in keywords:
        try:
            found = search_skills(client, kw, per_keyword)
        except (httpx.HTTPError, RuntimeError) as e:
            print("   ！「%s」搜不到：%s: %s" % (kw, type(e).__name__, e))
            continue
        stats["keywords"] += 1
        stats["per_keyword"][kw] = len(found)
        for s in found:
            seen.setdefault(s["skill_id"], s)
        time.sleep(PAUSE)
    got = list(seen.values())
    # Official first (they are the ones named on the home page), then by usage — a skill used
    # twenty thousand times is a better model of "how this is written" than one used twice.
    got.sort(key=lambda s: (not s["official"], -s["usage"], s["skill_name"]))
    return got, stats


# ------------------------------------------------------------------ the note
def _fence(text: str) -> str:
    """A backtick fence longer than anything inside, so a skill that contains ``` survives.

    A skill's body is itself Markdown and several of them do contain code fences; wrapping those
    in a three-backtick fence truncates the note at the first inner fence — and the note still
    renders, so the loss is invisible until somebody reads it.
    """
    run = best = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        best = max(best, run)
    return "`" * max(3, best + 1)


def note_of(s: dict) -> tuple[str, str]:
    title = s["skill_name"]
    line = "─" * 3
    head = [
        line,
        "source: jimeng skill · skill_id=%s · author=%s" % (s["skill_id"], s["author"]),
        "author: %s" % s["author"],
        "skill_id: %s" % s["skill_id"],
        "kind: official_skill" if s["official"] else "kind: market_skill",
        "usage: %d" % s["usage"],
    ]
    if s["tags"]:
        head.append("tags: [%s]" % ", ".join(s["tags"]))
    head += [line, "", "# %s" % title, ""]
    if s["description"]:
        head += ["**即梦上是这么介绍它的**：%s" % s["description"], ""]
    origin = ("即梦的**官方预置技能**" if s["official"]
              else "即梦**技能市场**里的技能，作者 %s" % s["author"])
    head += ["**出处**：%s（在即梦 %s 的「技能」里搜「%s」可见；市场接口不提供匿名详情页，"
             "所以这里只留 skill_id 与作者）" % (origin, HOME, title), ""]
    if s["usage"]:
        head += ["**被用过**：%d 次" % s["usage"], ""]
    if s["guide"]:
        head += ["**它给人的引导语**", "", "> " + s["guide"].replace("\n", "\n> "), ""]

    if s["instruction"]:
        fence = _fence(s["instruction"])
        head += ["**技能正文**（这是它的 `instruction`，原样收下）", "",
                 fence + "markdown", s["instruction"], fence, ""]
    else:
        head += ["**技能正文**：**不在市场接口里公开**（这类技能的逻辑在即梦服务端，只给描述）。"
                 "所以这份笔记能提供的只有定位与描述，不是一套可照做的写法 —— 别把它当成方法论用。",
                 ""]

    if s["has_showcase"]:
        head += ["**示范片**：这个技能自带示范作品。链接带签名会过期，所以没有留存 —— "
                 "要看就到即梦上按名字搜。", ""]

    head += [
        "**怎么用**：这是**别人写的技能正文**，学的是它怎么把镜头运动、分镜表、导演调度写成"
        "可执行的指令 —— 段落怎么排、约束怎么写死、哪些字段一定要填。要照着写，就把主体和内容"
        "换成本次要讲的那件事。**不要整段照抄对外发布**；用到了它的结构，就说明出自谁。",
        "",
    ]
    return title, "\n".join(head)


# ------------------------------------------------------------------ running it
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", default="", help="逗号分隔；默认是 %s" % "、".join(KEYWORDS))
    ap.add_argument("--per-keyword", type=int, default=20, help="每个关键词最多收几条（默认 20）")
    ap.add_argument("--limit", type=int, default=0, help="总共最多收几条（调试用）")
    ap.add_argument("--out", default="", help="笔记目录，默认 <数据目录>/jimeng-skills")
    ap.add_argument("--kb", default="", help="灌进哪个知识库（默认共享知识库）")
    ap.add_argument("--no-embed", action="store_true", help="只灌文字，不补向量")
    ap.add_argument("--report", default="", help="把结果写成 JSON")
    args = ap.parse_args()

    keywords = tuple(k.strip() for k in (args.keywords.split(",") if args.keywords else KEYWORDS)
                     if k.strip())
    data_dir = os.environ.get("TEAM_AGENT_DATA") or str(Path.home() / ".team-agent")
    out = Path(args.out).expanduser() if args.out else Path(data_dir) / "jimeng-skills"
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
    print("笔记目录 : %s" % out)
    print("关键词   : %s（每词最多 %d 条）\n" % ("、".join(keywords), args.per_keyword))

    existing = {str(d.get("filename") or ""): d["id"] for d in store.list_docs(kb["id"])}

    t0 = time.time()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            skills, stats = collect(client, keywords, args.per_keyword)
        except (httpx.HTTPError, RuntimeError) as e:
            print("！取不到技能列表：%s: %s" % (type(e).__name__, e), file=sys.stderr)
            return 1
    print("官方预置 %d 个；%d 个关键词分别命中 %s；去重后 %d 个技能"
          % (stats["official"], stats["keywords"],
             "/".join(str(v) for v in stats["per_keyword"].values()), len(skills)))
    if args.limit:
        skills = skills[:args.limit]

    added = replaced = empty_body = failed = 0
    names: list[str] = []
    # 每个技能被归到哪一类(`library.category_of`,由 `add_text` 自己算),
    # 这里只统计出来给人看 —— 抓回来的一堆技能能不能读成一张清单,一眼就知道。
    by_category: dict[str, int] = {}
    for s in skills:
        path = out / ("skill-%s.md" % s["skill_id"])
        title, body = note_of(s)
        path.write_text(body, encoding="utf-8")
        did = existing.get(str(path))
        try:
            # 归类是**我们说**的,不是从路径猜的:同一批技能即便写到数据目录之外(`--out /tmp/...`),
            # 那也是抓回来的素材。规则只有一份(`library.category_of`),拿它自己的名字、平台标签与正文来判。
            doc = lib.add_text(title, body, filename=str(path), kind="note", did=did, kb_id=kb["id"],
                               category=category_of(title, body, s.get("tags") or ()))
        except Exception as e:  # noqa: BLE001 — one bad skill must not stop the rest
            print("   [灌不进去] %s: %s: %s" % (s["skill_id"], type(e).__name__, e))
            failed += 1
            continue
        cat = str(doc.get("category") or "")
        by_category[cat] = by_category.get(cat, 0) + 1
        existing[str(path)] = did or ""
        names.append(title)
        if not s["instruction"]:
            empty_body += 1
        if did:
            replaced += 1
        else:
            added += 1

    vectors: dict = {}
    if not args.no_embed and (added or replaced):
        try:
            vectors = asyncio.run(_embed(lib, store))
        except Exception as e:  # noqa: BLE001
            print("！补向量失败（文字已经在库里了）：%s: %s" % (type(e).__name__, e))

    took = time.time() - t0
    print("\n共收 %d 个技能：新增 %d，替换 %d；其中正文不公开的 %d 个，灌不进去 %d 个；用时 %.1f 秒"
          % (added + replaced, added, replaced, empty_body, failed, took))
    print("  前几个：%s" % "、".join(names[:6]))
    if by_category:
        # 按数量从多到少;未分类(自己的话里没说清)排在最后 —— 它不是一类,是「没有类别」。
        parts = ["%s %d" % (CATEGORY_LABEL.get(k, k) or "未分类", v)
                 for k, v in sorted(by_category.items(), key=lambda kv: (kv[0] == "", -kv[1]))]
        print("  用处分类：%s" % "、".join(parts))
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"added": added, "replaced": replaced, "empty_body": empty_body, "failed": failed,
             "seconds": round(took, 1), "out": str(out), "kb": kb.get("id"), "vectors": vectors,
             "keywords": list(keywords), "per_keyword": stats["per_keyword"],
             "categories": {k: v for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
             "names": names}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n提示：这些是**别人写的技能正文**，学结构与写法；对外用要说明出自谁、别整段照抄。")
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
