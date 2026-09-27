"""一句话 → 一个短标签。项目名就是这种东西。

用户 2026-09-25:「这个项目名称都太长,限制 8 个字以内,系统自动使用最核心的关键词命名」。

⚠️⚠️ **这条规则只有这一份,而且它住在后端。** 两个调用点共用它:
① `POST /api/groups` 不带名字时,后端自己从任务原文里取关键词;
② 启动时把机器生成的旧长名字改成关键词(`shorten_stored`)。
前端只剩下「列表里显示前 8 个字 + 省略号」那种纯显示层的截断(`lib.ts::shortName`) ——
**判断留一处**,两边不会各说各话。这个项目在「同一件事两处判」上吃过不止一次亏
(谁能看图、什么算密钥、技能排序),名字这种一眼能看见的东西尤其经不起两套规则。
"""

from __future__ import annotations

import re

#: 项目名的长度上限。用户先要 10 个字,第二天收紧到 8 —— 这个数字只在这里一处。
NAME_MAX = 8

#: 点名(`@某人`)是称呼,不是「这件事是什么」。
_MENTION = re.compile(r"@\S+")
#: 第一个短句之后通常是补充说明,关键词几乎总在第一句里。
_CLAUSE = re.compile(r"[，。;；!?!？、,:：]")
#: 开头的客套与空动词。去掉之后剩下的才是「要什么」。
#: ⚠️ 多字的写法排在前面(`制作一个` 在 `制作` 之前):正则的择一是**从前往后**试的,反过来会先匹配
#: 到短的,于是「制作一个中秋节的图片」留下一个「一个」。
#: ⚠️ 只收**三字以上**的写法(画一个/写一个/生成一个…):单字的「画」「写」「生成」同时也是正经词头
#: (画布、写作计划、生成器),去掉它们会把好名字改坏 —— 而这个名字是用户每天要看的东西。
#: 单字的「做」「来」是上一版就有的,保留:它们去掉之后那个字确实什么也没说(「做个视频」→「视频」)。
_FILLER = re.compile(
    r"^(?:帮我|请|麻烦|给我|我要|我需要|我想|想要|看一下|看看|制作一个|制作|生成一个|弄一个|"
    r"起草一个|画一个|写一个|写一篇|写份|写个|做个|做一个|做|来)+")
#: 用来决定「英文能不能按词边界回退」。
_ASCII_TAIL = re.compile(r"[A-Za-z0-9]$")
#: 裁完之后顺手去掉尾巴上的标点与空白(裁到一半停在逗号上很难看)。
_TRAIL = re.compile(r"[，。、,;；:：!?！？\s]+$")
_WS = re.compile(r"\s+")


def short_name(text: str, fallback: str = "") -> str:
    """`text` → 「最核心的关键词」,最多 `NAME_MAX` 个字;取不出来就回 `fallback`。

    四步,顺序有理由:

    1. **去掉点名**:「@小助 帮我看看」这件事的名字是「帮我看看」,不是那个称呼;
    2. **只取第一个短句**:标点之后通常是补充说明(「视频制作,需要 3 段旁白,每段 15 秒」→「视频制作」);
    3. **去掉开头的客套与空动词**:剩下的才是「要什么」——
       「我需要做一个未破裂颅内动脉瘤」→「未破裂颅内动脉瘤」(而这正好是 8 个字);
    4. **裁到上限**,英文按**词边界**回退 —— `video` 被切成 `vid` 是坏名字,而中文没有词边界,只能按字数。

    ⚠️ 第 3 步之后**太短就别去**:「做个视频」的答案是「视频」,但如果去掉客套只剩一个字,
    那一个字就是这件事的全部信息(「做」)。
    """
    clean = _WS.sub(" ", _MENTION.sub("", text or "")).strip()
    if not clean:
        return fallback
    clause = (_CLAUSE.split(clean, 1)[0] or clean).strip() or clean
    body = _FILLER.sub("", clause).strip()
    if len(body) < 2:
        body = clause
    out = body[:NAME_MAX]
    if len(body) > NAME_MAX and _ASCII_TAIL.search(out):
        cut = out.rfind(" ")
        if cut >= 3:
            out = out[:cut]
    return _TRAIL.sub("", out).strip() or fallback


def shorten_stored(store) -> list[tuple[str, str]]:
    """把机器生成的老长名字改成关键词。启动时跑一次,**幂等**。

    ⚠️ 只对**超过 `NAME_MAX` 的名字**动手,而且只用 `short_name` 那一套规则。用户自己写的短名字
    (「视频制作」「创作写作」「ok」)一个都不碰 —— 它们本来就合格,而一个会改写用户亲手取的名字的
    自动规则,比长名字糟糕得多。取不出更好的名字时保持原样:宁可不改,也不要改成一个更糟的。

    返回改掉的那些 `(旧名, 新名)`,调用方拿去打印 —— 改用户数据这件事必须留下痕迹。
    """
    changed: list[tuple[str, str]] = []
    for g in store.list_groups():
        old = str(g.get("name") or "")
        if len(old) <= NAME_MAX:
            continue
        new = short_name(old)
        if not new or new == old:
            continue
        store.update_group(g["id"], {"name": new})
        changed.append((old, new))
    return changed
