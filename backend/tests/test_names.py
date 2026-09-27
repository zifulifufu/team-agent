"""项目名:一句话里最核心的那几个字。

用户 2026-09-25:「这个项目名称都太长,限制 8 个字以内,系统自动使用最核心的关键词命名」。
规则只有一份(`app/names.py`),这里的测试钉住两件事:**它取出来的名字是什么**,以及
**它不该动什么** —— 一个自动改名规则真正的风险不在取不出来,而在把好名字改坏。
"""

import pytest
from fastapi.testclient import TestClient

from app import names
from app.main import create_app
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


# ------------------------------------------------------------------ 规则本身
def test_the_core_keyword_is_what_survives():
    """四步各自都有它存在的理由,合起来才是那个「关键词」。

    这里逐个钉住的是**用户库里真实出现过的句子** —— 前两条就是他们那五个项目的名字。
    """
    assert names.short_name("我需要做一个未破裂颅内动脉瘤") == "未破裂颅内动脉瘤"
    assert names.short_name("制图，需要metachat找") == "制图"
    # 只取第一个短句:标点之后是补充说明,关键词几乎总在第一句里
    assert names.short_name("制作一个中秋节的图片，带着女儿到古镇去玩") == "中秋节的图片"
    # 去点名(那是称呼,不是「这件事是什么」)、去客套(剩下的才是「要什么」)
    assert names.short_name("@小助 帮我看看这个 HTML 网页为什么打不开") == "这个 HTML"
    assert names.short_name("画一个三层楼房的简笔画") == "三层楼房的简笔画"
    # 英文按词边界回退:`video` 被切成 `vid` 是坏名字
    assert names.short_name("make a video about strokes") == "make a"
    # 空到取不出东西时,由调用方决定退到什么
    assert names.short_name("   ") == "" and names.short_name("   ", "新项目") == "新项目"


def test_it_never_comes_out_longer_than_the_cap():
    """上限是这条规则的**硬约束**:列表里那一行放不下更多字,而用户已经说过两次「太长了」。"""
    for text in ("我需要做一个未破裂颅内动脉瘤",
                 "帮我看看这个 HTML 网页为什么打不开",
                 "写一个 60 秒短视频的旁白脚本",
                 "make a video about strokes for a medical talk"):
        assert 0 < len(names.short_name(text)) <= names.NAME_MAX, text


def test_a_word_head_that_is_also_a_real_word_is_left_alone():
    """⚠️ 这条是上一版的教训:**去掉客套 ≠ 见动词就砍**。

    「画」「写」「生成」既是空动词也是正经词头 —— 砍掉它们会把「画布设计规范」改成「布设计规范」,
    而这个名字是用户每天在左栏看的东西。所以客套表里只收三字以上的写法。
    """
    assert names.short_name("画布设计规范") == "画布设计规范"
    assert names.short_name("生成器性能优化") == "生成器性能优化"
    assert names.short_name("写作计划模板") == "写作计划模板"


# ------------------------------------------------------------------ 建群时取名
def test_a_group_created_without_a_name_is_named_from_the_task(client):
    """`POST /api/groups` 不带名字时,后端自己取关键词。

    ⚠️ 这条守的是「规则住在后端」这个决定:前端要是自己也算一遍再传过来,就等于把这条规则抄成两份,
    而两份规则迟早会各说各话(用户先要 10 个字、第二天要 8 个 —— 已经改过一次口径了)。
    """
    g = client.post("/api/groups", json={
        "task": "我需要做一个未破裂颅内动脉瘤的科普视频，给患者家属看", "member_ids": []}).json()
    assert g["name"] == "未破裂颅内动脉瘤", g["name"]


def test_an_explicit_name_is_still_honoured(client):
    """模板、`/api/groups` 的老调用方、以及**用户自己改名**都走这条路,不能被自动命名覆盖。"""
    g = client.post("/api/groups", json={"name": "视频制作", "task": "随便什么", "member_ids": []}).json()
    assert g["name"] == "视频制作"

    renamed = client.patch(f"/api/groups/{g['id']}", json={"name": "中秋图片"}).json()
    assert renamed["name"] == "中秋图片"


# ------------------------------------------------------------------ 老名字
def test_only_names_over_the_cap_are_rewritten_and_the_result_is_stable(store):
    """启动时把机器生成的旧长名字改成关键词。

    ⚠️ 三件事分开验,因为它们会各自坏掉:
      ①**用户自己写的短名字一个字都不动**(「视频制作」被改掉比名字太长糟糕得多);
      ②长名字改成关键词(这就是用户要的那一句);
      ③**幂等** —— 再跑一次什么都不改。一个每次启动都改点东西的规则,等于名字会自己变。
    """
    old = store.create_group("我需要做一个未破裂颅内动脉瘤")
    keep = store.create_group("视频制作")
    short = store.create_group("中秋图片")

    changed = dict(names.shorten_stored(store))
    assert changed.get("我需要做一个未破裂颅内动脉瘤") == "未破裂颅内动脉瘤", changed
    # ⚠️ 不断言「只改了这一个」:夹具里本来就有一个演示群(`Product launch group`,20 个字符),
    # 它也**应该**被改短 —— 按文本断言才守得住这条规则,按条数断言只会在夹具变化时变红。
    assert "Product launch group" not in changed.values()

    assert store.get_group(old["id"])["name"] == "未破裂颅内动脉瘤"
    assert store.get_group(keep["id"])["name"] == "视频制作", "用户自己写的短名字被改掉了"
    assert store.get_group(short["id"])["name"] == "中秋图片"

    assert names.shorten_stored(store) == [], "第二次跑还改了东西 —— 这条规则不是幂等的"
