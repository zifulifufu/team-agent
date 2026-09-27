"""语汇：把「写一句描述」变成「点几个词，拼出模型认的提示词」。

为什么需要这个模块，而不是让用户继续往一个文本框里写字：

ACE-Step 的 `tags` 字段**不是自然语言**，是一组逗号分隔的关键词，官方给的公式是
`[流派], [情绪], [2-3 个具体乐器], [人声], [制作风格], [BPM]`，而且有几条硬规矩——

* **流派永远放第一位**，它锚定其余全部；
* **5–12 个关键词最好**，超过 15 个开始互相稀释；
* **具体名词远胜形容词**：`felt piano` / `fingerpicked acoustic guitar` 会被真正渲染出来，
  而 `sophisticated, elegant, refined` 什么都没给模型（它没有对应的声音）；
* **BPM 要和流派对得上**：techno 活在 128–140，标 70 BPM 会让模型在两端之间摇摆；
* **互相矛盾的标签会毁掉输出**（`aggressive, serene`）。

一个自由文本框没法保证这些，用户也不知道有这些规矩 —— 实测第一版生成的曲子就是「描述性的
一句话」，出来的是那种谁也没要的通用钢琴。这里的每一栏都是一台推子，选完拼出来的字符串
已经合规；`compose_tags()` 还会在拼之前把不合规的地方**说出来**，而不是默默产出一首平庸的曲子。

⚠️ 词表是**通用音乐词汇**（流派 / 乐器 / 情绪 / 制作手法），不是从任何音乐平台抄来的分类表。
按维度点选这件事本身，才是「用分类提高生成能力」的实质：分类的价值在于它让人选得准，不在于
数据归谁。
"""
from __future__ import annotations

from . import i18n

__all__ = ["GENRES", "INSTRUMENTS", "PRODUCTION", "VOCALS", "PRESETS", "vocabulary",
           "compose_tags", "bpm_span", "MAX_TAGS"]

# 超过这个数，关键词开始互相稀释（官方指南：5–12 最好，>15 变糊）。
MAX_TAGS = 12

# 流派。`bpm` 是这个流派实际生活的区间 —— 用来在拼标签时**拦住**不合拍的组合，
# 而不是等模型产出怪东西再让用户去猜。`use` 是「什么片子适合用它」，这一栏才是分类的用处所在。
GENRES: list[dict] = [
    {"id": "cinematic-orchestral", "en": "cinematic orchestral score", "zh": "电影配乐",
     "bpm": (60, 100), "use": "讲解片的主底子，情绪推得动又不抢话"},
    {"id": "minimal-piano", "en": "minimal piano", "zh": "极简钢琴",
     "bpm": (55, 85), "use": "口播为主、需要留白的时候"},
    {"id": "ambient", "en": "ambient", "zh": "氛围",
     "bpm": (50, 80), "use": "长镜头、示意图停留、CTA 前的呼吸"},
    {"id": "lo-fi-hiphop", "en": "lo-fi hip-hop", "zh": "Lo-fi 嘻哈",
     "bpm": (70, 90), "use": "轻松科普、日常口吻"},
    {"id": "acoustic-folk", "en": "acoustic folk", "zh": "原声民谣",
     "bpm": (80, 110), "use": "病例故事、有人情味的部分"},
    {"id": "post-rock", "en": "post-rock", "zh": "后摇",
     "bpm": (70, 120), "use": "从平静推到高潮的长段"},
    {"id": "indie-pop", "en": "indie pop", "zh": "独立流行",
     "bpm": (100, 125), "use": "轻快的科普、面向公众的短片"},
    {"id": "synthwave", "en": "synthwave", "zh": "合成器浪潮",
     "bpm": (95, 115), "use": "科技感、器械与影像技术"},
    {"id": "tech-house", "en": "melodic house", "zh": "旋律浩室",
     "bpm": (118, 126), "use": "节奏明确的操作演示"},
    {"id": "trailer", "en": "trailer music", "zh": "预告片音乐",
     "bpm": (110, 140), "use": "开场钩子、结论的力量感"},
    {"id": "dark-ambient", "en": "dark ambient", "zh": "暗氛围",
     "bpm": (45, 75), "use": "风险、并发症、需要警觉的部分"},
    {"id": "documentary", "en": "documentary underscore", "zh": "纪录片配乐",
     "bpm": (65, 95), "use": "真实病例、访谈、资料画面"},
]

# 乐器：写成**能被渲染出来的具体名词**，不是形容词。
INSTRUMENTS: list[dict] = [
    {"id": "felt-piano", "en": "felt piano", "zh": "毡化钢琴", "for": "温暖、近、不亮"},
    {"id": "grand-piano", "en": "grand piano", "zh": "三角钢琴", "for": "干净、有存在感"},
    {"id": "solo-cello", "en": "solo cello", "zh": "独奏大提琴", "for": "沉、人性、悲伤"},
    {"id": "string-quartet", "en": "string quartet", "zh": "弦乐四重奏", "for": "古典、克制"},
    {"id": "warm-pad", "en": "warm analog pad", "zh": "温暖模拟铺底", "for": "不抢话的底"},
    {"id": "synth-pad", "en": "soft synth pad", "zh": "柔和合成铺底", "for": "科技、冷"},
    {"id": "picked-guitar", "en": "fingerpicked acoustic guitar", "zh": "指弹木吉他", "for": "朴素、叙事"},
    {"id": "electric-guitar", "en": "clean electric guitar", "zh": "清音电吉他", "for": "现代、city pop 味"},
    {"id": "upright-bass", "en": "upright bass", "zh": "立式贝斯", "for": "爵士味的走动"},
    {"id": "sub-bass", "en": "deep sub bass", "zh": "深沉低音", "for": "影院感、力量"},
    {"id": "brushed-drums", "en": "brushed drums", "zh": "刷鼓", "for": "轻、不打扰"},
    {"id": "soft-percussion", "en": "soft percussion", "zh": "轻打击", "for": "有节奏但不吵"},
    {"id": "low-brass", "en": "low brass", "zh": "低音铜管", "for": "结论、警示"},
    {"id": "marimba", "en": "marimba", "zh": "马林巴", "for": "轻快、有间隔感"},
    {"id": "drone", "en": "evolving drone", "zh": "渐变持续音", "for": "空间、不确定性"},
    {"id": "harp", "en": "harp", "zh": "竖琴", "for": "结论、收束、仪式感"},
]

# 制作手法：决定混音与质感。官方指南专门说这一栏「同样歌词能出完全不同的混音」。
PRODUCTION: list[dict] = [
    {"id": "clean", "en": "clean, hi-fi", "zh": "干净高保真"},
    {"id": "intimate", "en": "intimate room sound", "zh": "近距离空间感"},
    {"id": "wide", "en": "wide stereo mix", "zh": "宽立体声"},
    {"id": "cinematic-reverb", "en": "cinematic reverb", "zh": "影院混响"},
    {"id": "analog", "en": "analog warmth", "zh": "模拟温暖"},
    {"id": "tape", "en": "tape saturation", "zh": "磁带饱和"},
    {"id": "vinyl", "en": "vinyl crackle", "zh": "黑胶底噪"},
    {"id": "dusty", "en": "dusty, lo-fi", "zh": "Lo-fi 粉尘感"},
    {"id": "sparse", "en": "sparse arrangement", "zh": "稀疏编排"},
    {"id": "building", "en": "slow build", "zh": "缓慢推进"},
]

# 人声。留空即纯音乐 —— 但官方指南建议**连「no vocals」也写进 tags**（双保险）。
VOCALS: list[dict] = [
    {"id": "instrumental", "en": "no vocals", "zh": "纯音乐"},
    {"id": "humming", "en": "wordless humming", "zh": "无词哼唱"},
    {"id": "soft-female", "en": "soft female vocal", "zh": "柔和女声"},
    {"id": "warm-male", "en": "warm male vocal", "zh": "温暖男声"},
    {"id": "choir", "en": "choir", "zh": "合唱"},
]

# 场景预设：分类真正省事的地方 —— 把「这几个词一起用」这件经验固化下来。
PRESETS: list[dict] = [
    {"id": "explainer", "zh": "科普讲解底", "en": "explainer bed",
     "genre": "minimal-piano", "mood": "calm", "instruments": ["felt-piano", "warm-pad"],
     "production": ["clean", "intimate", "sparse"], "vocals": "instrumental", "bpm": 72,
     "note": "口播为主：留白多、不抢字、不快不慢"},
    {"id": "tension", "zh": "风险与悬念", "en": "risk and tension",
     "genre": "dark-ambient", "mood": "tense", "instruments": ["drone", "sub-bass"],
     "production": ["cinematic-reverb", "building"], "vocals": "instrumental", "bpm": 62,
     "note": "讲并发症、禁忌、需要警觉的地方"},
    {"id": "reveal", "zh": "结论与收束", "en": "conclusion",
     "genre": "cinematic-orchestral", "mood": "uplifting", "instruments": ["string-quartet", "harp", "low-brass"],
     "production": ["clean", "wide", "cinematic-reverb"], "vocals": "instrumental", "bpm": 84,
     "note": "把结论推起来，收尾不悬空"},
    {"id": "hook", "zh": "开场钩子", "en": "opening hook",
     "genre": "trailer", "mood": "epic", "instruments": ["low-brass", "sub-bass", "soft-percussion"],
     "production": ["cinematic-reverb", "building", "wide"], "vocals": "instrumental", "bpm": 120,
     "note": "前三秒抓住人，之后必须让位给讲解"},
    {"id": "story", "zh": "病例故事", "en": "case story",
     "genre": "documentary", "mood": "warm", "instruments": ["picked-guitar", "solo-cello"],
     "production": ["analog", "intimate"], "vocals": "instrumental", "bpm": 78,
     "note": "真实的病例、有人情味的部分"},
    {"id": "howto", "zh": "操作演示", "en": "how-to",
     "genre": "tech-house", "mood": "neutral", "instruments": ["synth-pad", "soft-percussion", "sub-bass"],
     "production": ["clean", "wide"], "vocals": "instrumental", "bpm": 120,
     "note": "步骤明确、节奏推进、不抒情"},
]


def vocabulary() -> dict:
    """Everything the page needs to draw the faders, already localised."""
    def rows(items: list[dict]) -> list[dict]:
        out = []
        for it in items:
            row = {"id": it["id"], "label": i18n.pick_now(it["en"], it["zh"])}
            for key in ("use", "for", "bpm"):
                if key in it:
                    row[key] = it[key]
            out.append(row)
        return out

    presets = []
    for p in PRESETS:
        presets.append({k: v for k, v in p.items() if k != "en"}
                       | {"label": i18n.pick_now(p["en"], p["zh"])})
    return {"genres": rows(GENRES), "instruments": rows(INSTRUMENTS),
            "production": rows(PRODUCTION), "vocals": rows(VOCALS), "presets": presets,
            "max_tags": MAX_TAGS}


def _find(items: list[dict], want: str) -> dict | None:
    w = str(want or "").strip()
    if not w:
        return None
    lw = w.lower()
    for it in items:
        if it["id"] == lw or it["en"].lower() == lw or it["zh"] == w:
            return it
    return None


def bpm_span(genre_id: str) -> tuple[int, int] | None:
    g = _find(GENRES, genre_id)
    return tuple(g["bpm"]) if g else None


def compose_tags(*, genre: str = "", mood: str = "", instruments: list[str] | tuple[str, ...] = (),
                 production: list[str] | tuple[str, ...] = (), vocals: str = "",
                 bpm: int = 0, extra: str = "") -> tuple[str, list[str]]:
    """`(tags, warnings)` — 按官方公式的顺序拼好，并**说出**不合规的地方。

    顺序是硬要求，不是审美：流派必须第一（它锚定其余），其后是情绪、乐器、人声、制作、BPM。
    `warnings` 不是"错误" —— 组合仍然会送去生成，因为用户可能就是要那个效果；它只是把
    「这样多半会糊」这件事**在花钱之前**讲出来。返回空 warnings 才说明这套词是干净的。
    """
    warn: list[str] = []
    parts: list[str] = []

    g = _find(GENRES, genre)
    if g:
        parts.append(g["en"])
    elif genre.strip():
        parts.append(genre.strip())          # 允许自由流派，但那要用户自己负责
        warn.append(i18n.pick_now(
            f"\"{genre}\" is not one of the listed genres — the model may not know it.",
            f"「{genre}」不在列出的流派里 —— 模型可能不认识它。"))

    if mood.strip():
        parts.append(mood.strip())

    for i in instruments:
        it = _find(INSTRUMENTS, i)
        parts.append(it["en"] if it else str(i))
    for p in production:
        it = _find(PRODUCTION, p)
        parts.append(it["en"] if it else str(p))
    v = _find(VOCALS, vocals)
    if v:
        parts.append(v["en"])
    if extra.strip():
        parts.append(extra.strip())

    # BPM 双写：官方指南明确说 tags 里写一次、参数里再写一次，两者一致会收紧节奏输出。
    if bpm:
        parts.append(f"{int(bpm)} bpm")
        span = bpm_span(genre) if g else None
        if span and not (span[0] <= int(bpm) <= span[1]):
            warn.append(i18n.pick_now(
                f"{int(bpm)} BPM sits outside what {g['en']} usually lives at "
                f"({span[0]}–{span[1]}); the model tends to swing between the two.",
                f"{int(bpm)} BPM 不在 {g['zh']} 惯常的区间（{span[0]}–{span[1]}）里，"
                "模型容易在这两端之间摇摆。"))

    if not g and not extra.strip():
        # 有自由文本就没关系（用户自己知道要什么）；两者都没有才是问题。
        warn.append(i18n.pick_now(
            "No genre chosen — that is the anchor the other keywords hang off.",
            "没有选流派 —— 它是其余关键词的锚点。"))
    if len(parts) > MAX_TAGS:
        warn.append(i18n.pick_now(
            f"{len(parts)} keywords — past about {MAX_TAGS} they start diluting each other. "
            "Drop the ones that matter least.",
            f"有 {len(parts)} 个关键词 —— 超过 {MAX_TAGS} 个左右就开始互相稀释，"
            "把最不重要的那几个去掉。"))
    # 互相矛盾的一对：官方指南点名这两种（激烈 vs 平静、lo-fi vs 高保真干净）会把模型撕开，
    # 让它来回摇摆而不是选一边。
    low = " ".join(parts).lower()
    if any(w in low for w in ("calm", "serene")) and any(w in low for w in ("aggressive", "epic")):
        warn.append(i18n.pick_now(
            "Calm and aggressive/epic together make the model oscillate instead of committing.",
            "「平静」和「激烈/史诗」放在一起，模型会在两端摇摆而不是选一边。"))
    if "lo-fi" in low and ("hi-fi" in low or "clean" in low):
        warn.append(i18n.pick_now(
            "Lo-fi and hi-fi/clean in the same prompt fight each other.",
            "同一句里既有 Lo-fi 又有高保真/干净，两者互相打架。"))
    return ", ".join(p for p in parts if p), warn
