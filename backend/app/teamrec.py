"""Who should be in a group chat, given what the user says they want done.

The home screen's box asks for a *task*, and until now "who joins" was answered two other ways:
`SCENES` offers a fixed lineup per scenario tab, and a template brings a whole team. This answers
the same question from the sentence itself — read what the user wrote, pick the people who fit, and
say **why** for each, because a lineup nobody can argue with is a lineup nobody trusts.

The signal is a **concept overlap**, not a keyword match, and that choice is the whole design. A
member's strengths live in a mixture of languages and places — the person who writes role text is
not the person who names the model — so "视频" in the task has to find the member whose role says
"Video generation", and 「入排标准」 has to find the expert named 「临床研究设计专家」. Measured on
this machine with a plain keyword match: 34 tickable members, **20 of them carrying no strength tags
at all**, and a video model scoring 0.75 for a video task on the meaningless bigram "60".

⚠️ Nothing is written to the database here. The group does not exist yet, and creating members for
a lineup the user then edits away would litter the member list. A hit expert comes back as a `key`;
the caller creates it (via `templates.ensure_agent_from_key`) when the lineup is accepted.
"""

from __future__ import annotations

import re

from . import i18n, strengths

# What a task can be about, and the words that express it in either language. One row per concept,
# both languages on the same row — that pairing is what makes 「视频」 and "Video generation" land on
# the same concept instead of on nothing.
#
# ⚠️ A concept is not a strength: `video` says *what is being made*, `writing` says *what the work
# is like*. They are used for two different jobs (this table picks who, `WANT_WORDS` says how), so
# they are deliberately kept apart rather than merged into one vocabulary.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "video": ("视频", "片子", "短片", "影片", "剪辑", "分镜", "镜头", "成片", "短片", "电影",
              "video", "clip", "footage", "storyboard", "shot list", "render", "movie", "film",
              "seedance", "seedream-video"),
    "image": ("图片", "图像", "画图", "制图", "绘制", "图谱", "模拟图", "画面", "海报", "配图", "插画", "绘图", "照片", "封面", "静帧",
              "image", "picture", "photo", "poster", "illustration", "drawing", "still frame",
              "kontext", "seedream"),
    "audio": ("配乐", "音乐", "配音", "旁白", "声音", "音效", "朗读",
              "music", "soundtrack", "voice over", "narration", "audio", "sound"),
    "document": ("文档", "报告", "周报", "汇报", "纪要", "方案", "论文", "讲稿", "总结", "文稿", "合同", "通知",
                 "document", "report", "minutes", "proposal", "paper", "draft", "memo", "contract"),
    "data": ("数据", "统计", "表格", "计算", "指标", "样本", "数据库", "分析表",
             "data", "statistic", "spreadsheet", "excel", "csv", "table", "compute", "metric",
             "analytics"),
    "code": ("代码", "脚本", "程序", "自动化", "开发", "调试", "爬虫", "接口", "部署", "仓库",
             "code", "script", "program", "debug", "automate", "scrape", "api", "deploy", "repo"),
    "research": ("检索", "搜索", "查资料", "找资料", "文献", "综述", "资料", "事实核查",
                 "search", "retrieve", "lookup", "literature", "review of", "research",
                 "fact check"),
    "medical": ("临床", "医学", "患者", "病例", "诊疗", "循证", "指南", "影像", "卒中", "用药",
                "medical", "clinical", "patient", "diagnosis", "guideline", "imaging", "trial"),
    "writing": ("写作", "文案", "润色", "翻译", "摘要", "故事", "剧本", "科普", "宣传", "公众号",
                "writing", "copy", "polish", "translate", "summary", "story", "article",
                "newsletter"),
    "review": ("校对", "审核", "审查", "检查", "复核", "验收", "把关",
               "proofread", "review", "audit", "check", "verify", "qc"),
    "plan": ("规划", "计划", "分工", "排期", "拆解", "项目管理", "流程",
             "plan", "schedule", "breakdown", "project management", "workflow"),
}

# What a task asks of the *work*, which is a different question from what it is about (see CONCEPTS
# above). A word may light up more than one strength on purpose: 「写个脚本」 is writing *and*
# coding, and a lineup with a writer and a coder is the right answer to it.
WANT_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("multimodal", ("看图", "图片", "图像", "画面", "视频", "片子", "短片", "剪辑", "分镜", "镜头",
                    "配乐", "动画", "字幕",
                    "image", "picture", "video", "clip", "footage", "storyboard", "render",
                    "music", "animation", "subtitle")),
    ("coding", ("代码", "脚本", "程序", "自动化", "开发", "调试", "爬虫", "接口", "部署",
                "code", "script", "program", "debug", "automate", "api", "deploy")),
    ("tool-use", ("检索", "搜索", "查一下", "查资料", "找资料", "抓取", "爬", "表格", "计算",
                  "统计一下", "excel", "csv", "search", "retrieve", "look up", "scrape",
                  "spreadsheet", "compute", "calculate")),
    ("writing", ("写", "文案", "稿", "报告", "纪要", "润色", "摘要", "翻译", "论文", "公众号",
                 "宣传", "故事", "剧本", "讲稿", "汇报", "总结", "科普",
                 "write", "draft", "copy", "report", "minutes", "polish", "summary", "translate",
                 "essay", "paper", "story", "article", "newsletter")),
    ("long-context", ("综述", "文献", "多篇", "通读", "整理资料", "长文", "全篇", "大量材料",
                      "literature", "long document", "many documents", "merge")),
    ("reasoning", ("分析", "评估", "核对", "审", "设计", "方案", "论证", "判断", "拆解", "规划",
                   "复盘", "可行性", "风险",
                   "analys", "evaluat", "check", "review", "design", "plan", "reason", "assess")),
]

# Domain words that should count even though they do not appear in the expert's own name — the
# bridge between how a user speaks and what a preset is called. What is *not* here is covered by the
# concept table and by the name overlap below, so a new preset works without touching this: these
# entries only make the common phrasings hit harder.
EXPERT_WORDS: dict[str, tuple[str, ...]] = {
    "trial-design": ("临床试验", "随机对照", "rct", "入排", "终点", "研究设计", "试验方案", "样本量"),
    "biostat": ("统计", "p值", "显著性", "置信区间", "回归", "spss", "sas"),
    "evidence": ("循证", "证据", "meta", "荟萃", "系统评价", "证据等级"),
    "paper": ("论文", "投稿", "审稿", "sci", "返修", "参考文献"),
    "ethics": ("伦理", "知情同意", "irb", "受试者"),
    "crf": ("crf", "病例报告表", "数据录入", "录库", "edc"),
    "guideline": ("指南", "共识", "推荐意见"),
    "stroke": ("卒中", "中风", "脑梗", "脑出血", "静脉溶栓", "取栓"),
    "cerebrovascular": ("脑血管", "动脉瘤", "介入", "支架", "血管内治疗"),
    "cerebrovascular-public": ("科普", "患者教育", "宣教", "患教"),
    "imaging": ("影像", "ct", "mri", "核磁", "读片", "灌注"),
    "patient": ("患者沟通", "医患", "告知", "谈话", "随访"),
    "med-english": ("医学英语", "英译", "润色英文", "投稿信"),
    "pharm": ("用药", "药物", "剂量", "相互作用", "不良反应", "抗生素"),
    "creative": ("创意", "广告词", "slogan", "点子"),
}

# A Chinese fragment shorter than this carries too little meaning («60», «一个», «标准»), and a
# Latin one shorter than this is a fragment of a word («vi», «gen»). Measured: without both limits a
# video model scored for a video task purely on the digits "60".
_MIN_CJK_GRAM = 2
_MIN_LATIN_WORD = 4
_LATIN = re.compile(r"^[a-z][a-z0-9\-]*$")


def _features(text: str) -> set[str]:
    """The comparable pieces of a phrase: Chinese character n-grams, plus whole Latin words.

    Deliberately not a tokeniser — the project has no Chinese segmenter and does not want one for
    this. Bigrams and trigrams are enough to tell 「临床研究设计」 from 「影像解读」, need no dictionary,
    and are symmetric on both sides of the comparison. Latin goes through as **whole words** rather
    than character n-grams, because "video" vs "vido" sharing "vid" means nothing, while the word
    "video" is exactly the signal.
    """
    t = (text or "").lower()
    out: set[str] = set()
    cjk = "".join(ch for ch in t if "\u4e00" <= ch <= "\u9fff")
    for n in (2, 3):
        out |= {cjk[i:i + n] for i in range(max(0, len(cjk) - n + 1))}
    for w in re.split(r"[^a-z0-9\-]+", t):
        if len(w) >= _MIN_LATIN_WORD and _LATIN.match(w):
            out.add(w)
    return out


def _concepts(text: str) -> set[str]:
    """Which concepts a phrase is about, in either language."""
    low = (text or "").lower()
    return {name for name, words in CONCEPTS.items() if any(w.lower() in low for w in words)}


def _needs(text: str) -> dict[str, list[str]]:
    """Which strengths the task asks for, each with the words that asked for it."""
    low = (text or "").lower()
    out: dict[str, list[str]] = {}
    for tag, words in WANT_WORDS:
        for w in words:
            if w.lower() in low:
                out.setdefault(tag, []).append(w)
    return out


def task_tags(text: str) -> list[str]:
    """Use the same task vocabulary for recruiting and fallback routing."""
    return strengths.clean_tags(list(_needs(text)))


def _label_concepts(concepts: set[str]) -> list[str]:
    lang = i18n.current()
    names = {
        "video": ("video", "视频"), "image": ("images", "图片"), "audio": ("audio", "音频"),
        "document": ("documents", "文档"), "data": ("data", "数据"), "code": ("code", "代码"),
        "research": ("research", "资料检索"), "medical": ("medical", "医学"),
        "writing": ("writing", "写作"), "review": ("review", "审校"), "plan": ("planning", "规划"),
    }
    return [names[c][1 if lang == "zh" else 0] for c in sorted(concepts) if c in names]


def _why_member(agent: dict, want: dict[str, list[str]], shared: set[str]) -> str:
    """One sentence the user can argue with: what this member was picked *for*."""
    lang = i18n.current()
    bits: list[str] = []
    if shared:
        bits.append(i18n.pick_now(f"made for {', '.join(_label_concepts(shared)[:3])}",
                                  f"正是做{_label_concepts(shared)[0]}的" if len(shared) == 1
                                  else f"对口:{'、'.join(_label_concepts(shared)[:3])}"))
    hits = [t for t in strengths.TAG_IDS if t in (agent.get("tags") or []) and t in want]
    if hits:
        names = "、".join(strengths.label(t, lang) for t in hits[:2])
        bits.append(i18n.pick_now(f"strong at {names}", f"强项:{names}"))
    if not bits and (agent.get("tags") or []):
        names = "、".join(strengths.label(t, lang) for t in agent["tags"][:2])
        bits.append(i18n.pick_now(f"carries {names}", f"带着{names}"))
    if not bits:
        bits.append(i18n.pick_now("a member of this group", "本群成员"))
    return " · ".join(bits)


def suggest(store, text: str, limit: int = 6) -> dict:
    """The lineup for `text`: existing members first, then experts worth bringing in.

    Returns `{"members": [...], "experts": [...], "reads": {...}}`. Members carry `why` (shown next
    to each name) and experts carry `key` (the caller creates them on acceptance) plus `why`.

    ⚠️ "Can this member hold the chair" is **not** part of the score. Relevance and eligibility are
    different questions, and folding them together made a video task rank writers above the video
    model — the chair bonus outweighed the actual match. It is applied afterwards instead: if the
    lineup has nobody who can run the group, one is added from the best-scoring eligible members.
    """
    from . import localcmd, media, presets  # local: keeps this module free of import cycles
    from .teamsetup import model_problem

    want = _needs(text)
    task_concepts = _concepts(text)
    task_feats = _features(text)

    cfg = store.get_settings()
    eligible = [a for a in store.list_agents() if not a.get("hidden")
                and (not a.get("engine") or cfg["external_agents_enabled"])
                and (not a.get("model_id") or not model_problem(store, store.get_model(a["model_id"])))]
    scored = []
    named_tools = set()
    task_lower = (text or "").lower()
    needs_voice = any(w in task_lower for w in ("配音", "旁白", "朗读", "克隆", "音色", "narration", "voice over", "tts", "speech"))
    for a in eligible:
        tool = localcmd.row(str(a.get("engine") or ""))
        explicit_tool = False
        if tool:
            aliases = [tool["name"].lower(), str(a["engine"]).lower()]
            aliases += {"openchatcut": ["chatcut"], "shotcraft": ["shtocraft"]}.get(a["engine"], [])
            explicit_tool = any(word in task_lower for word in aliases)
            # A general video request is not a request to render every installed template.
            if not explicit_tool and not (tool.get("voice_engine") and needs_voice):
                continue
            if explicit_tool:
                named_tools.add(a["id"])
        own_text = " ".join(str(a.get(k) or "") for k in ("name", "role", "skills"))
        own_concepts = {"audio"} if tool and tool.get("voice_engine") else _concepts(own_text)
        # A generating member says what it makes in the one place that is authoritative — the `use`
        # its model was classified with — and its role text is not to be believed instead. Measured:
        # every MetaChat media member's role reads "MetaChat media (drawing and video)", because the
        # *provider* serves both, so a picture model matched the video concept and was offered as
        # "the one for video". `member_target` is the same answer `_media_turn` uses to decide which
        # tool to run.
        use = ""
        if (a.get("origin") or "") == media.MEDIA_ORIGIN:
            use = str(((media.member_target(store, a) or {}).get("use")) or "")
            own_concepts = {use} if use in CONCEPTS else set()
        shared = task_concepts & own_concepts
        feats = _features(own_text)
        feat_hits = feats & task_feats
        preset = presets.builtin_for(a.get("name")) or {}
        if preset.get("kind") == "expert" and not any(
            word.lower() in task_lower for word in EXPERT_WORDS.get(preset.get("key", ""), ())
        ):
            continue
        specialty = own_concepts & {"video", "image", "audio", "medical"}
        if specialty and not (specialty & task_concepts) and not explicit_tool:
            continue

        score = 10.0 if explicit_tool else (0.1 if a.get("engine") == "qwen3tts" else 0.0)
        score += len(shared) * 2.5                       # what it is about — the strongest signal
        score += len(feat_hits) * 0.6                    # name/role wording overlap
        for tag, words in want.items():
            if tag in (a.get("tags") or []):
                score += 1.0 + min(len(words), 3) * 0.25  # asked for more than once = stronger
        if score > 0:
            scored.append((score, a, shared))

    scored.sort(key=lambda r: (-r[0], str(r[1].get("name") or "")))
    # One generator per requested output type, and no duplicate handles for the same model.
    picked = []
    seen_models, seen_uses, seen_engines = set(), set(), set()
    voice_picked = False
    for row in scored:
        a = row[1]
        mid = a.get("model_id")
        use = (media.member_target(store, a) or {}).get("use")
        engine = a.get("engine")
        is_voice = bool((localcmd.row(str(engine or "")) or {}).get("voice_engine"))
        if is_voice and voice_picked and a["id"] not in named_tools:
            continue
        if (mid and mid in seen_models) or (engine and engine in seen_engines) or (use and (use not in task_concepts or use in seen_uses)):
            continue
        picked.append(row)
        voice_picked = voice_picked or is_voice
        if engine:
            seen_engines.add(engine)
        if mid:
            seen_models.add(mid)
        if use:
            seen_uses.add(use)
        if len(picked) >= max(1, limit):
            break
    # Reserve the requested voice capability even when a long clinical brief makes
    # general medical roles score higher. More discussion cannot synthesize audio.
    if needs_voice and not voice_picked:
        voice = next((r for r in scored if (localcmd.row(str(r[1].get("engine") or "")) or {}).get("voice_engine")), None)
        if voice:
            picked.append(voice)
    chair_only = ""          # id of a member added solely to hold the chair (see below)

    def coordinator_rank(a: dict) -> int:
        preset = presets.builtin_for(a.get("name")) or {}
        return (0 if preset.get("key") == "pm" else
                1 if preset.get("role") == "Coordinator" else
                2 if preset.get("key") == "host" else 3)

    # A specialist matching many domain words should not displace coordination.
    # Keep the decision in the recommender so the preview and actual group agree.
    if not any(coordinator_rank(a) < 3 for _, a, _ in picked):
        coordinators = sorted((a for a in eligible if media.may_host(a) and coordinator_rank(a) < 3),
                              key=coordinator_rank)
        if coordinators:
            coordinator = coordinators[0]
            picked.append((0.0, coordinator, set()))
            chair_only = coordinator["id"]

    # Nobody here can run the group (all generators/external agents): add somebody who can, rather
    # than returning a lineup the server would refuse to create. ⚠️ The last resort is *any* eligible
    # member, score or not — measured on 「做一个中秋节的图片」, which matches only picture generators,
    # where nothing in the scored list could hold the chair and the lineup came back with no host.
    if not any(media.may_host(a) for _, a, _ in picked):
        taken = {a["id"] for _, a, _ in picked}
        spare = next((r for r in scored if media.may_host(r[1]) and r[1]["id"] not in taken), None)
        if spare is None:
            fallback = next((a for a in eligible
                             if a["id"] not in taken and media.may_host(a)), None)
            spare = (0.0, fallback, set()) if fallback else None
        if spare:
            picked = picked + [spare]
            chair_only = spare[1]["id"]

    members = [{
        "id": a["id"], "name": a["name"], "avatar": a.get("avatar") or "",
        "role": a.get("role") or "", "may_host": media.may_host(a),
        "why": (i18n.pick_now("runs the group — somebody has to hold the chair",
                              "负责带队 —— 一个群总得有人当群主")
                if a["id"] == chair_only else _why_member(a, want, shared)),
        "score": round(sc, 2),
    } for sc, a, shared in picked]

    # Experts: a preset that fits and is not already a member. ⚠️ A preset is only offered when
    # something in the task actually points at it — offering "论文写作专家" for a meeting-minutes task
    # (because both carry `writing`) filled the suggestion with three near-identical experts, which
    # is worse than offering none. Shared strengths only add to the score, they never qualify alone.
    # `ensure_agent_from_key` is deliberately not called here (see the module docstring).
    have = {str(a.get("name") or "").lower() for a in store.list_agents()}
    experts = []
    for p in presets.EXPERT_PRESETS:
        name = p.get("name") or ""
        if name.lower() in have:
            continue
        hits = [w for w in EXPERT_WORDS.get(p.get("key") or "", ())
                if w.lower() in (text or "").lower()]
        own_text = " ".join([name, str(p.get("name_zh") or ""), str(p.get("role") or ""),
                             str(p.get("role_zh") or "")])
        feats = _features(own_text)
        shared = task_concepts & _concepts(own_text)
        feat_hits = [f for f in sorted(feats & task_feats)]
        # ⚠️ A shared *concept* alone must not qualify a preset: "paper" is a `document` word, so
        # 「论文写作专家」 matched a meeting-minutes task and got offered for it. Something in the task
        # has to point at this preset specifically — a domain word, or wording from its own name.
        if not hits:
            continue
        tag_hits = [t for t in (p.get("tags") or []) if t in want]
        score = len(hits) * 2.0 + len(feat_hits) * 1.2 + len(shared) * 1.5 + len(tag_hits) * 0.5
        # Deduplicated: `hits` and `feat_hits` can name the same word, and the sentence read
        # "matches 科普, 科普" when it did.
        shown = list(dict.fromkeys(hits + feat_hits))[:3] or _label_concepts(shared)[:3]
        experts.append({
            "key": p["key"], "name": name, "name_zh": p.get("name_zh") or name,
            "avatar": p.get("avatar") or "🧑‍🔬", "role": i18n.pick_now(p.get("role") or "", p.get("role_zh") or p.get("role") or ""),
            "why": i18n.pick_now(f"matches {', '.join(shown)}", f"与「{'、'.join(shown)}」相关"),
            "score": round(score, 2),
        })
    experts.sort(key=lambda e: (-e["score"], e["name"]))

    # A configured tool need not already have a member row to be proposed. This is still read-only.
    proposed_models = []
    warnings = []
    for use in ("image", "video"):
        if use not in task_concepts:
            continue
        candidates = [m for m in store.list_media_models() if m.get("use") == use and not model_problem(store, m)]
        candidates.sort(key=lambda m: (m.get("kind") != "comfyui", m["model_name"] != "wan2.2-ti2v-5b", m["id"]))
        already = [a for _, a, _ in picked if a.get("origin") == media.MEDIA_ORIGIN
                   and (media.member_target(store, a) or {}).get("use") == use
                   and not model_problem(store, store.get_model(a.get("model_id")))]
        if not already and candidates:
            m = candidates[0]
            proposed_models.append({"id": m["id"], "name": m["display_name"], "use": use,
                "why": i18n.pick_now(f"Produces the requested {use} file", f"负责生成任务所需的{'图片' if use == 'image' else '视频'}文件")})
        elif not already:
            warnings.append(i18n.pick_now(f"No configured {use} generator: add one, or let a member use an available tool.",
                f"暂无已配置的{'绘图' if use == 'image' else '视频'}生成模型:请补充工具,或确认成员能使用其他制作方式。"))
    return {
        "members": members,
        "host_ref": next((f"agent:{a['id']}" for _, a, _ in sorted(picked, key=lambda r: coordinator_rank(r[1]))
                          if media.may_host(a) and coordinator_rank(a) < 3),
                         next((f"agent:{m['id']}" for m in members if m["may_host"]), "")),
        "experts": experts[:3],
        "models": proposed_models,
        "warnings": warnings,
        "reads": {
            "concepts": _label_concepts(task_concepts),
            "needs": [strengths.label(t, i18n.current()) for t in
                      sorted(want, key=lambda t: (t not in strengths.TAG_IDS, t))],
        },
    }
