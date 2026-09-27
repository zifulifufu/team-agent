"""The skill ingest's silent-failure surfaces, pinned.

The script is run by hand and its end-to-end behaviour was exercised against the live market
(114 skills, 7 official). What these tests protect is the part that would fail **without saying
anything** on some future run:

* **A body that contains its own ``` fence.** Most of these skills *are* Markdown and a dozen of
  them contain code blocks. Wrapping such a body in a three-backtick fence ends the note at the
  first inner fence — and the note still renders, so the truncation is invisible until somebody
  reads it and wonders why the method stops mid-sentence.
* **An empty `instruction`.** A skill whose logic lives on the server returns `instruction: ""`.
  Written up as a normal entry it looks like a methodology we have, when what we have is a name
  and a description — the failure mode is a member confidently following nothing.
* **`ret` is a string on this site** (`"0"`), so `if data["ret"] != 0` reads every valid reply as a
  rejection, and the ingest reports "即梦回绝了" while holding a perfectly good payload.
* **Ten keywords overlap heavily.** Without dedup, 格莱美运镜 is written ten times and the library
  looks like it has ten of it.

No network: the two endpoints are driven through `httpx.MockTransport`, shaped like the real
replies (measured 2026-09-25).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script():
    spec = importlib.util.spec_from_file_location("ingest_jimeng_skills",
                                                  ROOT / "scripts" / "ingest-jimeng-skills.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ijs = load_script()


# ------------------------------------------------------------------ the fake site
def fake_client(official=None, market=None, fail=False):
    """A client that answers the two endpoints the way the real one does."""
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(200, json={"ret": "1000", "errmsg": "invalid parameter"})
        if str(request.url).endswith("/skill/list"):
            return httpx.Response(200, json={
                "ret": "0", "errmsg": "success",
                "data": {"skills": None, "total_count": 0, "has_more": False,
                         "official_skills": official if official is not None else OFFICIAL}})
        if str(request.url).endswith("/skill/market/search"):
            body = json.loads(request.content.decode())
            return httpx.Response(200, json={
                "ret": "0", "errmsg": "success",
                "data": {"skills": (market or {}).get(body.get("keyword"), []),
                         "total_count": 0, "has_more": False}})
        return httpx.Response(404, json={"ret": "404", "errmsg": "no such endpoint"})
    return httpx.Client(transport=httpx.MockTransport(handler))


OFFICIAL = [
    {"id": "web_agent_skill_seedance_shot_design", "name": "创作分镜",
     "default_desc": "面向视频模型的专业级虚拟导演与提示词工程师。",
     "default_guide_text": "说说你想拍什么", "market_tag_list": ["general_creation"],
     "showcase_media": [{"type": "video"}]},
    {"id": "", "name": "没有 id 的条目"},          # dropped
    {"id": "web_agent_skill_x", "name": ""},       # dropped
]

MARKET_运镜 = [
    {"skill_id": "21426643430412", "skill_name": "格莱美运镜", "description": "三段式镜头运动。",
     "instruction": "# 格莱美运镜\n\n俯冲 → 定格 → 横移慢跟。", "usage_count": 75,
     "effective_user": {"name": "即梦AI", "uid": "1"}, "showcase_media": [{"type": "video"}]},
    {"skill_id": "21228507900428", "skill_name": "电影级长镜头", "description": "一个不剪切的镜头。",
     "instruction": "# 电影级长镜头\n\n相机是主动的叙事者。", "usage_count": 24088,
     "effective_user": {"name": "即梦AI"}},
]

MARKET_长镜头 = [
    # the same skill again: ten keywords overlap, and this is what dedup is for
    {"skill_id": "21228507900428", "skill_name": "电影级长镜头", "description": "一个不剪切的镜头。",
     "instruction": "# 电影级长镜头\n", "usage_count": 24088, "effective_user": {"name": "即梦AI"}},
    {"skill_id": "999", "skill_name": "无正文的技能", "description": "逻辑在服务端。",
     "instruction": "", "usage_count": 5, "effective_user": {}},
]


# ------------------------------------------------------------------ the fence
def test_a_body_containing_a_fence_gets_a_longer_fence():
    body = "开场\n```python\ncamera = move()\n```\n收尾"
    fence = ijs._fence(body)
    assert len(fence) == 4
    # The point: the note must not be cut at the inner fence. Round-tripping it proves that.
    note = fence + "\n" + body + "\n" + fence
    inner = note.split("\n", 1)[1]
    assert body in inner


def test_a_plain_body_keeps_the_usual_three_backticks():
    assert ijs._fence("没有代码块的正文") == "```"
    assert ijs._fence("单反引号 `x` 不算") == "```"


def test_a_body_that_is_all_backticks_still_gets_a_longer_fence():
    assert len(ijs._fence("````````")) == 9


# ------------------------------------------------------------------ the note
def test_an_official_skill_says_its_body_is_not_public():
    s = {"skill_id": "web_agent_skill_a", "skill_name": "创作分镜", "description": "专业级虚拟导演。",
         "instruction": "", "guide": "说说你想拍什么", "author": "即梦AI（官方预置技能）",
         "usage": 0, "tags": ["general_creation"], "official": True, "has_showcase": True}
    title, body = ijs.note_of(s)
    assert title == "创作分镜"
    assert "kind: official_skill" in body
    assert "不在市场接口里公开" in body          # never dressed up as a methodology we have
    assert "说说你想拍什么" in body
    assert "web_agent_skill_a" in body


def test_a_market_skill_carries_its_body_author_and_id():
    s = {"skill_id": "21228507900428", "skill_name": "电影级长镜头", "description": "不剪切的镜头。",
         "instruction": "# 电影级长镜头\n\n相机是主动的叙事者。", "guide": "", "author": "即梦AI",
         "usage": 24088, "tags": [], "official": False, "has_showcase": False}
    title, body = ijs.note_of(s)
    assert title == "电影级长镜头"
    assert "kind: market_skill" in body
    assert "相机是主动的叙事者。" in body
    assert "即梦AI" in body and "21228507900428" in body
    assert "24088" in body
    assert "不在市场接口里公开" not in body
    assert "不要整段照抄" in body                # the standing it is collected under


def test_the_note_does_not_promise_a_link_that_does_not_resolve():
    s = {"skill_id": "x", "skill_name": "某技能", "description": "", "instruction": "",
         "guide": "", "author": "某人", "usage": 0, "tags": [], "official": False,
         "has_showcase": False}
    _, body = ijs.note_of(s)
    assert "/ai-tool/skill/" not in body          # there is no anonymously readable detail page


# ------------------------------------------------------------------ the two sources
def test_official_skills_are_read_and_the_broken_rows_dropped():
    with fake_client() as c:
        got = ijs.official_skills(c)
    assert [s["skill_name"] for s in got] == ["创作分镜"]
    assert got[0]["official"] is True and got[0]["instruction"] == ""
    assert got[0]["author"] == "即梦AI（官方预置技能）"


def test_a_nonzero_ret_is_an_error_even_though_it_is_a_string():
    with fake_client(fail=True) as c:
        with pytest.raises(RuntimeError):
            ijs.official_skills(c)


def test_a_market_skill_is_read_with_its_author_and_usage():
    with fake_client(market={"运镜": MARKET_运镜}) as c:
        got = ijs.search_skills(c, "运镜", 20)
    assert [s["skill_name"] for s in got] == ["格莱美运镜", "电影级长镜头"]
    assert got[0]["author"] == "即梦AI" and got[0]["usage"] == 75
    assert got[0]["instruction"].startswith("# 格莱美运镜")


def test_an_unsigned_market_skill_is_labelled_not_guessed():
    with fake_client(market={"长镜头": MARKET_长镜头}) as c:
        got = ijs.search_skills(c, "长镜头", 20)
    assert got[1]["author"] == "(未署名)"


# ------------------------------------------------------------------ collect
def test_overlapping_keywords_produce_one_note_per_skill():
    with fake_client(market={"运镜": MARKET_运镜, "长镜头": MARKET_长镜头}) as c:
        got, stats = ijs.collect(c, ("运镜", "长镜头"), 20)
    ids = [s["skill_id"] for s in got]
    assert len(ids) == len(set(ids)), "十来个关键词高度重叠，去重不能漏"
    assert "21228507900428" in ids
    assert stats["keywords"] == 2 and stats["official"] == 1


def test_official_comes_first_and_the_rest_by_how_much_they_are_used():
    with fake_client(market={"运镜": MARKET_运镜, "长镜头": MARKET_长镜头}) as c:
        got, _ = ijs.collect(c, ("运镜", "长镜头"), 20)
    assert got[0]["official"] is True                     # the ones named on the home page
    rest = [s for s in got if not s["official"]]
    assert [s["usage"] for s in rest] == sorted((s["usage"] for s in rest), reverse=True)


def test_a_keyword_that_fails_does_not_lose_the_others():
    """One dead keyword must not turn into an empty knowledge base."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/skill/list"):
            return httpx.Response(200, json={"ret": "0", "data": {"official_skills": OFFICIAL}})
        body = json.loads(request.content.decode())
        if body.get("keyword") == "坏的":
            return httpx.Response(200, json={"ret": "100008", "errmsg": "invalid skill parameter"})
        return httpx.Response(200, json={"ret": "0", "data": {"skills": MARKET_运镜}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got, stats = ijs.collect(c, ("坏的", "运镜"), 20)
    assert stats["keywords"] == 1
    # Both market skills survive, in usage order (24088 then 75) — the dead keyword cost nothing.
    assert [s["skill_name"] for s in got if not s["official"]] == ["电影级长镜头", "格莱美运镜"]
