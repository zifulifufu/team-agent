"""Zones: the four surfaces, and the three rules that keep a zone from being a lie.

A zone is a page of declarations, so the tests worth writing are not "does it parse" but the ones
about a declaration that has quietly stopped being true:

* every skill, tool, template and member a zone names **exists** — a zone promising a seat that
  cannot be created is a roster that fails at the moment somebody starts a group from it;
* `in template` agrees with the template it points at — the roles table and the roster are two
  statements about the same four seats, and this is what keeps them the same one;
* a `blocked` workflow **says what is blocking it**, and a `ready` zone has something that runs —
  otherwise the word on the badge is decoration;
* a zone file the app cannot use is **named with its reason**, never skipped.
"""
from __future__ import annotations

import json

import pytest

from app import i18n, presets, toolhub, tools, zones
from app import musicprompt


@pytest.fixture(autouse=True)
def _own_folder(tmp_path):
    """`use_folder` is module state, like the other shelves — put it back afterwards."""
    zones.use_folder(tmp_path / "zones")
    try:
        yield tmp_path / "zones"
    finally:
        zones.use_folder(None)


def _write(folder, name: str, body: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


GOOD = {
    "name": "Newsletter zone", "name_zh": "简报专区", "icon": "PenLine", "state": "planned",
    "blurb": "Write it and send it.", "blurb_zh": "写出来发出去。",
    "library": {"items": [{"label": "Issues", "label_zh": "期号", "state": "planned"}]},
    "templates": {"items": [{"label": "Weekly", "label_zh": "周报", "state": "planned"}]},
    "workflows": {"items": [{"label": "Draft", "label_zh": "起草", "state": "planned"}]},
    "roles": {"items": [{"label": "Editor", "label_zh": "编辑", "member": "Copywriter",
                         "skills": ["Office writing conventions"], "tools": ["write_document"],
                         "state": "planned"}]},
}


# --------------------------------------------------------- the declarations hold together

def test_every_shipped_zone_has_all_four_surfaces():
    for zid, z in zones.BUILTIN.items():
        for surface in zones.SURFACES:
            assert surface in z, f"{zid} 少了 {surface}"
        assert z["state"] in ("ready", "planned")
        assert z["blurb"] and z["blurb_zh"], zid
        assert z["name"] and z["name_zh"], zid


def test_every_skill_a_zone_names_is_a_skill_this_app_ships():
    """A zone promising `Short video storyboards` to a seat only works if that skill exists."""
    have = {v.get("name") for v in tools.EXAMPLE_SKILLS.values()}
    named = {s for z in zones.BUILTIN.values() for it in z["roles"]["items"]
             for s in (it.get("skills") or [])}
    assert named, "至少有一个座位要带技能，否则这条断言是空跑"
    assert not (named - have), f"这些技能不存在: {sorted(named - have)}"


def test_every_tool_a_zone_names_is_a_tool_that_exists():
    named = {t for z in zones.BUILTIN.values() for it in z["roles"]["items"]
             for t in (it.get("tools") or [])}
    assert named, "至少有一个座位要带工具，否则这条断言是空跑"
    assert not (named - set(toolhub.BUILTIN_SPECS)), f"这些工具不存在: {sorted(named - set(toolhub.BUILTIN_SPECS))}"


def test_every_group_template_a_zone_points_at_exists():
    tids = {t["id"] for t in presets.TEMPLATES}
    named = {z["template"] for z in zones.BUILTIN.values() if z["template"]}
    assert named, "至少有一个专区要点名群模板，否则这条断言是空跑"
    assert not (named - tids), f"这些群模板不存在: {sorted(named - tids)}"


def test_a_seat_marked_in_template_really_is_in_that_template():
    """⚠️ 这一条是「一个判断写了两份」的守卫。

    `roles` 说「这个位子群模板里就有」，`presets.TEMPLATES` 说模板带哪几个人 —— 两句话说的
    是同一件事。哪天有人给模板加了一个位子而没动这里，页面上就会显示「要自己加」，
    而实际上模板已经带了它 —— 于是用户会加出第二个。
    """
    templates = {t["id"]: t for t in presets.TEMPLATES}
    checked = 0
    for zid, z in zones.BUILTIN.items():
        if not z["template"]:
            continue
        members = set(templates[z["template"]]["members"])
        for it in z["roles"]["items"]:
            if it.get("in_template"):
                checked += 1
                assert it["member"] in members, \
                    f"{zid}: 「{it['label']}」标着模板里有，但 {z['template']} 里没有 {it['member']}"
    assert checked, "没有一条座位标了 in_template，这条守卫没验到东西"


def test_every_seat_names_a_member_that_exists():
    names = {a.get("name") for a in presets.BUILTIN_AGENTS}
    named = {it["member"] for z in zones.BUILTIN.values() for it in z["roles"]["items"]
             if it.get("member")}
    assert named
    assert not (named - names), f"这些成员不存在: {sorted(named - names)}"


def test_a_blocked_workflow_says_what_is_blocking_it():
    """`blocked` with no reason is a dead end: the person reading it has nothing to act on."""
    blocked = [it for z in zones.BUILTIN.values() for it in z["workflows"]["items"]
               if it["state"] == "blocked"]
    assert blocked, "没有 blocked 的条目，这条断言是空跑"
    for it in blocked:
        assert it["note"].strip() and it["note_zh"].strip(), it["id"]


def test_a_ready_zone_has_something_that_actually_runs():
    """⚠️ 一个 `ready` 的专区，里面至少得有一件 `ready` 的事 —— 否则那个徽标是装饰。"""
    for zid, z in zones.BUILTIN.items():
        if z["state"] != "ready":
            continue
        runnable = [it for surface in ("workflows", "library", "templates")
                    for it in z[surface]["items"] if it["state"] in ("ready", "partial")]
        assert runnable, f"{zid} 标着 ready，但四块里没有一件是能跑的"


# ------------------------------------------------------------------ the user's own folder

def test_a_zone_of_ones_own_loads_and_appears_in_the_list(_own_folder):
    _write(_own_folder, "newsletter.json", GOOD)
    ids = [z["id"] for z in zones.catalog()]
    assert "newsletter" in ids
    # 自带的专区一个都不能少
    assert set(zones.BUILTIN) <= set(ids)


def test_a_file_that_shadows_a_shipped_zone_is_refused_by_name(_own_folder):
    """⚠️ 静默覆盖一个自带专区，等于改了一个用户没碰过的页面。所以是拒绝，而且要报名字。"""
    _write(_own_folder, "video.json", GOOD)
    errs = zones.errors()
    assert any("video.json" in e["file"] for e in errs), errs
    assert any("video" in e["why"] for e in errs), errs
    # 而且自带的那份没被换掉
    assert zones.row("video")["icon"] == "Clapperboard"


def test_an_icon_the_app_cannot_draw_is_refused_and_lists_what_it_can(_own_folder):
    _write(_own_folder, "newsletter.json", GOOD | {"icon": "Rocket"})
    errs = [e for e in zones.errors() if e["file"] == "newsletter.json"]
    assert errs, "坏图标必须报出来，不能悄悄换个默认图标"
    assert "Clapperboard" in errs[0]["why"], errs[0]["why"]   # 报出「能画的有哪些」


def test_a_state_that_is_not_a_state_is_refused(_own_folder):
    _write(_own_folder, "newsletter.json", GOOD | {"state": "almost"})
    assert any("state" in e["why"] for e in zones.errors()), zones.errors()


def test_a_broken_file_does_not_take_the_shipped_zones_with_it(_own_folder):
    ( _own_folder / "junk.json").write_text("{ not json", encoding="utf-8")
    _write(_own_folder, "newsletter.json", GOOD)
    assert any(e["file"] == "junk.json" for e in zones.errors())
    assert zones.row("newsletter") is not None
    assert zones.row("video") is not None


def test_a_missing_zone_is_none_rather_than_a_default():
    """⚠️ 取一个不存在的专区**不能**退回第一个 —— 那就把「你要的那个」换成了「随便一个」。"""
    assert zones.detail("nope") is None
    assert zones.row("nope") is None


# -------------------------------------------------------------------------------- the page

def test_the_detail_is_localised_and_drops_the_second_language():
    was = i18n.current()
    try:
        i18n.set_current("zh")
        got = zones.detail("video")
        assert got["name"] == "视频专区"
        top = got["workflows"]["items"][0]
        assert top["label"] == "机制动画（推荐）"
        assert "_zh" not in json.dumps(got, ensure_ascii=False), \
            "两套语言不能在出口处同时露出去 —— 页面只该拿到一份"
    finally:
        i18n.set_current(was)


def test_ready_zones_come_first_in_the_sidebar_list():
    states = [z["state"] for z in zones.catalog()]
    assert states == sorted(states, key=lambda s: 0 if s == "ready" else 1), states


def test_the_video_zone_lists_the_composer_own_presets():
    """⚠️ 这份清单来自 `musicprompt`，不是重打一遍 —— 重打的那份迟早和作曲台对不上。"""
    got = zones.detail("video")
    ids = [it["id"] for it in got["templates"]["items"] if it["state"] == "ready"]
    assert ids == [p["id"] for p in musicprompt.PRESETS]


def test_the_video_zone_counts_what_is_on_its_shelves():
    """计数是「现在有几件」，所以它必须来自那两个货架本身，而不是写死的数字。"""
    got = zones.detail("video")
    counts = {it["id"]: it.get("count") for it in got["library"]["items"]}
    assert set(counts) == {"music-shelf", "my-material"}
    assert all(isinstance(v, int) for v in counts.values()), counts


def test_a_zone_with_nothing_in_a_surface_says_nothing_rather_than_inventing_it(_own_folder):
    """空列表就空着。为「这一块看起来太空」而编几条内容，正是「点了没反应」的来源。"""
    _write(_own_folder, "newsletter.json", GOOD | {"workflows": {"items": []}})
    got = zones.detail("newsletter")
    assert got["workflows"]["items"] == []


# ------------------------------------------- the page draws three of the declared four

def test_the_page_gets_only_the_surfaces_it_draws():
    """⚠️ 声明四块、只画三块，而且**第四块连响应都不带**。

    用户 2026-09-27：「在视频专区里面的各个面板里不需要放分工、工具与技能。」块撤了，但两条
    都不能跟着丢：① 注册表里 `roles` 仍在（那是这个专区将来起群时的那份名单，上面四条守卫
    读的就是它）；② 响应里**不能**再有它 —— 送过去却没有页面会渲染的东西，正是这个项目反复
    在治的那种「等于不存在」。
    """
    assert set(zones.DRAWN) < set(zones.SURFACES), "要么四块全画、要么这条断言没验到东西"
    got = zones.detail("video")
    for name in zones.DRAWN:
        assert name in got, name
    for name in set(zones.SURFACES) - set(zones.DRAWN):
        assert name not in got, f"{name} 被送出去了，但没有页面会画它"
        assert name in zones.BUILTIN["video"], f"{name} 在注册表里也不能丢"


def test_the_undrawn_surface_is_still_held_to_its_guards():
    """撤掉的只是「画出来」，不是「对得上」—— `in_template` 那条守卫仍然要守着。"""
    templates = {t["id"]: t for t in presets.TEMPLATES}
    zone = zones.BUILTIN["video"]
    members = set(templates[zone["template"]]["members"])
    marked = [it for it in zone["roles"]["items"] if it.get("in_template")]
    assert marked, "视频专区的分工表里一条 in_template 都没有，守卫成了空跑"
    for it in marked:
        assert it["member"] in members, it
