"""语汇 + 读描述：把**一句人话**变成符合同一格子里的**一串标签**。

ACE-Step 的 `tags` 字段**不是自然语言**，是一组逗号分隔的关键词，官方公式是
`[流派], [情绪], [2-3 个具体乐器], [人声], [制作风格], [BPM]`，而且有几条硬规矩 ——

* **流派永远放第一位**，它锚定其余全部；
* **5–12 个关键词最好**，超过 15 个开始互相稀释；
* **具体名词远胜形容词**：`felt piano` / `fingerpicked acoustic guitar` 会被真正渲染出来，
  而 `sophisticated, elegant, refined` 什么都没给模型（它没有对应的声音）；
* **BPM 要和流派对得上**：techno 活在 118–126，标 70 BPM 会让模型在两端之间摇摆；
* **互相矛盾的标签会毁掉输出**（`aggressive, serene`）。

第一阶段这里是一排**推子**（流派 / 情绪 / 乐器 / 制作 / 人声），用户在面板上逐个点。实测下来
那是错的：一个视频作者想的是「来一段安静的、别抢话的底」，不是「我该选哪个制作手法」。

所以现在是 `read()` —— **读那句话**。它按上面五条把描述翻成标签，用户在面板上只需要写字，
外加两个他确实有意见的旋钮（人声、时长）。算法决定的那些**不出现在面板上**（用户 2026-09-27：
「情绪也没必要放到面板上，这些都在算法里面……不用在面板中展示出来」）。

⚠️ `read()` 是**确定的**，不是模型调用：一张中英双语的线索表 + 每个流派自带的一套「标准配器」。
这么选有三个理由 —— 不花 token、离线可用、**可以写断言**（「一句安静钢琴不该读出鼓」这种话，
只有确定的实现才验得了）。将来若要用模型来读更自由的表述，换的应该是这一个函数，不是调用它的地方。

⚠️ 线索表是**通用音乐词汇**，不是从任何音乐平台抄来的分类表。按线索读这句话本身，才是
「用分类提高生成能力」的实质：分类的价值在于它让人说得准，不在于数据归谁。
"""
from __future__ import annotations

import re

from . import i18n

__all__ = ["GENRES", "INSTRUMENTS", "PRODUCTION", "VOCALS", "PRESETS", "vocabulary",
           "compose_tags", "read", "bpm_span", "MAX_TAGS", "DEFAULT_GENRE"]

# 超过这个数，关键词开始互相稀释（官方指南：5–12 最好，>15 变糊）。
MAX_TAGS = 12

# 一句话里什么都没读出来时用的底子。选 `documentary` 不是因为最好听，而是因为它的用途写着
# 「真实病例、访谈、资料画面」—— 这个程序里的曲子绝大多数是垫在旁白底下的，而它的区间（65–95）
# 正好落在不快不慢的中段。**读不出来就说读不出来**（见 `read()` 的 notes），不假装读懂了。
DEFAULT_GENRE = "documentary"

# 流派。`bpm` 是这个流派实际生活的区间 —— 用来在拼标签时**拦住**不合拍的组合，
# 而不是等模型产出怪东西再让用户去猜。`use` 是「什么片子适合用它」，这一栏才是分类的用处所在。
# `kit` / `prod` 是**这个流派自带的配器与质感**：读描述时，没被提到的那几栏从这里取，
# 于是一句很含糊的话也能得到一套自洽的结果，而不是一堆随便挑的词。
GENRES: list[dict] = [
    {"id": "cinematic-orchestral", "en": "cinematic orchestral score", "zh": "电影配乐",
     "bpm": (60, 100), "use": "讲解片的主底子，情绪推得动又不抢话",
     "kit": ["string-quartet", "warm-pad", "low-brass"], "prod": ["clean", "wide", "cinematic-reverb"]},
    {"id": "minimal-piano", "en": "minimal piano", "zh": "极简钢琴",
     "bpm": (55, 85), "use": "口播为主、需要留白的时候",
     "kit": ["felt-piano", "warm-pad"], "prod": ["clean", "intimate", "sparse"]},
    {"id": "ambient", "en": "ambient", "zh": "氛围",
     "bpm": (50, 80), "use": "长镜头、示意图停留、CTA 前的呼吸",
     "kit": ["drone", "warm-pad"], "prod": ["sparse", "cinematic-reverb"]},
    {"id": "lo-fi-hiphop", "en": "lo-fi hip-hop", "zh": "Lo-fi 嘻哈",
     "bpm": (70, 90), "use": "轻松科普、日常口吻",
     "kit": ["felt-piano", "brushed-drums", "upright-bass"], "prod": ["dusty", "vinyl", "tape"]},
    {"id": "acoustic-folk", "en": "acoustic folk", "zh": "原声民谣",
     "bpm": (80, 110), "use": "病例故事、有人情味的部分",
     "kit": ["picked-guitar", "solo-cello"], "prod": ["analog", "intimate"]},
    {"id": "post-rock", "en": "post-rock", "zh": "后摇",
     "bpm": (70, 120), "use": "从平静推到高潮的长段",
     "kit": ["electric-guitar", "sub-bass", "soft-percussion"], "prod": ["building", "wide"]},
    {"id": "indie-pop", "en": "indie pop", "zh": "独立流行",
     "bpm": (100, 125), "use": "轻快的科普、面向公众的短片",
     "kit": ["grand-piano", "picked-guitar", "brushed-drums"], "prod": ["clean", "wide"]},
    {"id": "synthwave", "en": "synthwave", "zh": "合成器浪潮",
     "bpm": (95, 115), "use": "科技感、器械与影像技术",
     "kit": ["synth-pad", "sub-bass"], "prod": ["clean", "wide"]},
    {"id": "tech-house", "en": "melodic house", "zh": "旋律浩室",
     "bpm": (118, 126), "use": "节奏明确的操作演示",
     "kit": ["synth-pad", "soft-percussion", "sub-bass"], "prod": ["clean", "wide"]},
    {"id": "trailer", "en": "trailer music", "zh": "预告片音乐",
     "bpm": (110, 140), "use": "开场钩子、结论的力量感",
     "kit": ["low-brass", "sub-bass", "soft-percussion"], "prod": ["cinematic-reverb", "building", "wide"]},
    {"id": "dark-ambient", "en": "dark ambient", "zh": "暗氛围",
     "bpm": (45, 75), "use": "风险、并发症、需要警觉的部分",
     "kit": ["drone", "sub-bass"], "prod": ["cinematic-reverb", "sparse"]},
    {"id": "documentary", "en": "documentary underscore", "zh": "纪录片配乐",
     "bpm": (65, 95), "use": "真实病例、访谈、资料画面",
     "kit": ["felt-piano", "solo-cello"], "prod": ["analog", "sparse"]},
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
    {"id": "clean", "en": "clean, hi-fi", "zh": "干净高保真", "for": "不想要底色的时候"},
    {"id": "intimate", "en": "intimate room sound", "zh": "近距离空间感", "for": "像在耳边"},
    {"id": "wide", "en": "wide stereo mix", "zh": "宽立体声", "for": "有画面感"},
    {"id": "cinematic-reverb", "en": "cinematic reverb", "zh": "影院混响", "for": "大片感"},
    {"id": "analog", "en": "analog warmth", "zh": "模拟温暖", "for": "不刺耳"},
    {"id": "tape", "en": "tape saturation", "zh": "磁带饱和", "for": "旧、有年份"},
    {"id": "vinyl", "en": "vinyl crackle", "zh": "黑胶底噪", "for": "生活气息"},
    {"id": "dusty", "en": "dusty, lo-fi", "zh": "Lo-fi 粉尘感", "for": "放松、不精致"},
    {"id": "sparse", "en": "sparse arrangement", "zh": "稀疏编排", "for": "留白、给旁白让路"},
    {"id": "building", "en": "slow build", "zh": "缓慢推进", "for": "往上推、不收住"},
]

# 人声。`auto` 不是一种声音，是「让算法读描述决定」—— 面板上它排第一，因为多数时候用户没意见。
VOCALS: list[dict] = [
    {"id": "instrumental", "en": "no vocals", "zh": "纯音乐"},
    {"id": "humming", "en": "wordless humming", "zh": "无词哼唱"},
    {"id": "soft-female", "en": "soft female vocal", "zh": "柔和女声"},
    {"id": "warm-male", "en": "warm male vocal", "zh": "温暖男声"},
    {"id": "choir", "en": "choir", "zh": "合唱"},
]

# 场景预设：分类真正省事的地方 —— 把「这几个词一起用」这件经验固化下来。
#
# ⚠️ 面板上**不再画它们**（2026-09-27 起描述框代替了推子），它们现在的用处是专区的「模板」那一块：
# 一份可以直接读的起手件清单。`read()` 里的流派自带配器（`kit`/`prod`）就是从这里提炼的。
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


# --------------------------------------------------------------------------- 线索表
#
# 每一条都是「出现这些词就说明是这个」。中英并列，因为用户会用中文写、而词表是英文的
# （模型认英文标签，这一点在 `compose_tags` 里）。
# ⚠️ 线索是**小写匹配**，所以这里全写小写；中文没有大小写，原样写。

_GENRE_CUES: dict[str, tuple[str, ...]] = {
    "cinematic-orchestral": ("管弦", "交响", "弦乐", "电影", "大片", "配乐", "庄重",
                             "orchestra", "orchestral", "cinematic", "score", "symphonic"),
    "minimal-piano": ("钢琴", "极简", "留白", "简单", "单纯", "克制",
                      "piano", "minimal", "sparse keys", "solo piano"),
    "ambient": ("氛围", "空间", "空旷", "环境", "漂浮", "ambient", "atmospheric", "soundscape", "drone"),
    "lo-fi-hiphop": ("lo-fi", "lofi", "嘻哈", "放松", "学习", "咖啡", "日常", "随意",
                     "hip hop", "hip-hop", "chill", "chillhop"),
    "acoustic-folk": ("民谣", "木吉他", "民乐", "朴实", "乡", "folk", "acoustic", "guitar", "country"),
    "post-rock": ("后摇", "推向高潮", "渐强", "层层", "往上推", "post-rock", "post rock", "crescendo"),
    "indie-pop": ("流行", "轻快", "明亮", "欢快", "活泼", "pop", "upbeat", "cheerful", "bright"),
    "synthwave": ("合成器", "电子", "赛博", "科技", "未来", "机器", "器械",
                  "synth", "synthwave", "retrowave", "80s", "neon"),
    "tech-house": ("浩室", "舞曲", "节奏感", "律动", "电子舞曲", "强劲节奏",
                   "house", "techno", "edm", "dance", "club"),
    "trailer": ("预告", "开场", "钩子", "震撼", "有力", "燃", "气势",
                "trailer", "epic", "powerful", "blockbuster"),
    "dark-ambient": ("暗", "阴", "紧张", "危险", "悬念", "不安", "并发症", "并发症", "警报", "忌讳",
                     "dark", "tense", "ominous", "eerie", "dread", "suspense", "thriller"),
    "documentary": ("纪录片", "纪实", "病例", "访谈", "真实", "叙述", "资料",
                    "documentary", "underscore", "narrative", "true story"),
}

_INSTRUMENT_CUES: dict[str, tuple[str, ...]] = {
    "felt-piano": ("毡", "温柔钢琴", "柔和钢琴", "温钢琴", "felt piano"),
    "grand-piano": ("钢琴", "三角钢琴", "piano", "keys"),
    "solo-cello": ("大提琴", "cello"),
    "string-quartet": ("弦乐", "四重奏", "小提琴", "strings", "quartet", "violin"),
    "warm-pad": ("铺底", "暖垫", "垫子", "pad"),
    "synth-pad": ("合成铺底", "合成器", "synth", "synth pad"),
    "picked-guitar": ("木吉他", "吉他", "指弹", "acoustic guitar", "guitar", "fingerpick"),
    "electric-guitar": ("电吉他", "electric guitar"),
    "upright-bass": ("贝斯", "低音提琴", "bass", "double bass"),
    "sub-bass": ("超低音", "重低音", "sub", "低频"),
    "brushed-drums": ("刷鼓", "轻鼓", "brushed", "soft drums"),
    "soft-percussion": ("打击", "鼓点", "节奏组", "percussion", "drums", "beat"),
    "low-brass": ("铜管", "号", "brass", "horn", "trumpet"),
    "marimba": ("马林巴", "木琴", "marimba", "kalimba"),
    "drone": ("持续音", "嗡", "drone", "sustain"),
    "harp": ("竖琴", "harp"),
}

_PRODUCTION_CUES: dict[str, tuple[str, ...]] = {
    "clean": ("干净", "清晰", "高保真", "不脏", "clean", "hi-fi", "hifi"),
    "intimate": ("近", "耳边", "私密", "亲密", "intimate", "close"),
    "wide": ("宽", "空间感", "立体", "wide", "stereo"),
    "cinematic-reverb": ("混响", "影院", "空旷回响", "reverb", "cathedral"),
    "analog": ("模拟", "温暖质感", "不刺耳", "analog", "warmth"),
    "tape": ("磁带", "旧", "年份", "tape", "vintage"),
    "vinyl": ("黑胶", "底噪", "vinyl", "crackle"),
    "dusty": ("粉尘", "lo-fi", "lofi", "dusty", "gritty"),
    "sparse": ("留白", "稀疏", "不铺满", "简单", "sparse", "minimal arrangement", "no drums", "不要鼓"),
    "building": ("渐强", "推进", "越来越", "推到高潮", "build", "building", "rising"),
}

# ⚠️ 键必须与 `music.MOODS` 一致 —— 曲库按那份闭词表校验，多一个少一个都会让上架被拒。
# 这里不 import `music`（这个模块要当叶子，读描述的代价不该带上整条存储链），改用一条断言守着
# （`test_music_prompt.py`）。**两处说的是同一件事，就必须有一条机械守卫。**
_MOOD_CUES: dict[str, tuple[str, ...]] = {
    "calm": ("安静", "平静", "舒缓", "冷静", "轻柔", "放松", "不抢", "平稳的",
             "calm", "quiet", "peaceful", "gentle", "soft", "mellow", "relaxing"),
    "warm": ("温暖", "温情", "人情", "亲切", "怀旧", "朴素", "治愈感",
             "warm", "tender", "heartfelt", "cosy", "cozy", "nostalgic"),
    "tense": ("紧张", "悬疑", "压迫", "不安", "警觉", "危险", "忌讳",
              "tense", "suspense", "uneasy", "ominous", "dread", "tension"),
    "sad": ("悲伤", "难过", "遗憾", "失落", "沉重", "哀", "告别",
            "sad", "melancholy", "mournful", "somber", "sombre", "grief"),
    "uplifting": ("希望", "治愈", "向上", "感动", "积极", "明亮", "温暖人心",
                  "uplifting", "hopeful", "healing", "inspiring", "positive"),
    "epic": ("史诗", "震撼", "宏大", "力量", "燃", "壮阔", "大片感",
             "epic", "powerful", "grand", "heroic", "punch", "cinematic impact"),
    "neutral": ("中性", "客观", "平稳", "不煽情", "克制",
                "neutral", "steady", "matter-of-fact", "understated"),
}

_VOCAL_CUES: dict[str, tuple[str, ...]] = {
    "instrumental": ("纯音乐", "纯器乐", "器乐", "无人声", "不要人声", "不唱", "没人唱", "不要唱",
                     "instrumental", "no vocals", "without vocals", "no singing"),
    "humming": ("哼唱", "哼", "无词", "吟", "hum", "humming", "wordless"),
    "soft-female": ("女声", "女生", "女主唱", "female", "female vocal", "woman"),
    "warm-male": ("男声", "男生", "男声唱", "male", "male vocal", "man"),
    "choir": ("合唱", "合声", "人声铺底", "choir", "chorus", "vocal ensemble"),
}

# 同一家族的成员不并列。点名「钢琴」之后流派自带的「毡化钢琴」再补进来，不是更丰富，
# 而是两条几乎一样的线索互相稀释 —— 实测第一版就出了 `grand piano, felt piano` 两个钢琴。
# 家族表放在这里而不是每个乐器上加一个字段：只有真会同场打架的那几个需要它。
_FAMILY: dict[str, str] = {
    "grand-piano": "keys", "felt-piano": "keys", "marimba": "keys",
    "warm-pad": "pad", "synth-pad": "pad", "drone": "pad",
    "picked-guitar": "guitar", "electric-guitar": "guitar",
    "upright-bass": "bass", "sub-bass": "bass",
    "brushed-drums": "drums", "soft-percussion": "drums",
}

# 速度词。读到时把 BPM 挪到该流派区间的下三分之一 / 上三分之一，而不是写死一个数 ——
# 写死的数会让「慢节奏的浩室」变成 70 BPM，而那就是模型来回摇摆的那种输入。
_SLOW_CUES = ("慢", "缓", "悠", "沉", "安静", "舒缓", "别太急", "slow", "gentle", "laid-back",
              "downtempo", "calm")
_FAST_CUES = ("快", "迅速", "紧凑", "激烈", "强劲", "急促", "upbeat", "fast", "driving",
              "energetic", "uptempo", "punchy")


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


def _hits(text: str, cues: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """每个候选**命中了几个线索**。命中数而不是布尔，是为了让说得更细的那句话赢：
    「暗、紧张、悬念」把 `dark-ambient` 顶到前面，而不是和「听起来暗一点」并列。"""
    out: dict[str, int] = {}
    for key, words in cues.items():
        n = sum(1 for w in words if w in text)
        if n:
            out[key] = n
    return out


def _best(hit: dict[str, int], prefer: "list[str] | tuple[str, ...]" = ()) -> str:
    """命中数最高的那个；**同分时按给定顺序取前一个**（顺序即优先级，不靠字典序 —— 字典序会让
    「按什么排」变成一个没人打算过的决定）。"""
    if not hit:
        return ""
    top = max(hit.values())
    for key in prefer:
        if hit.get(key) == top:
            return key
    for key in hit:
        if hit[key] == top:
            return key
    return ""


def _explicit_bpm(text: str) -> int:
    m = re.search(r"(\d{2,3})\s*(?:bpm|beat|拍)", text)
    if not m:
        return 0
    return max(40, min(200, int(m.group(1))))


def read(text: str, *, vocals: str = "", lyrics: str = "") -> dict:
    """读一句描述 → 一套生成参数。**唯一出口**，调用方（接口、工具、测试）都走它。

    返回 `{genre, mood, instruments, production, vocals, bpm, notes, matched}`：

    * 五个参数直接喂给 `compose_tags()`，所以「怎么读」和「怎么拼」各有各的一处；
    * `matched` 是**它到底从哪几个词读出来的**；面板上那行回执由前端用 `read` 的结果 +
      它已经加载的词表拼出来（**不在后端拼那一行**：流派名要中文、情绪是曲库的英文闭词，
      后端拼出来必然是半中半英的）;
    * `notes` 是「读不出来」这类要说的话（本地化）。空 notes 才说明这句话被完整读懂了。

    ⚠️ `vocals` 传进来的是**用户在面板上的选择**；传空或 `auto` 才由描述决定。这是唯一一个
    「人的手压过算法」的地方，因为人声是唯一一件作者真正有意见的事（要不要唱、谁来唱）。
    """
    raw = str(text or "")
    low = raw.lower()
    notes: list[str] = []

    gid = _best(_hits(low, _GENRE_CUES), [g["id"] for g in GENRES])
    if not gid:
        gid = DEFAULT_GENRE
        notes.append(i18n.pick_now(
            "No genre came through in that description, so it starts from the documentary bed "
            "— say the style if you want a different one.",
            "这句话里没读出流派，所以用纪录片底的配器起手 —— 想要别的风格，在描述里说出来。"))
    genre = next(g for g in GENRES if g["id"] == gid)

    # 配器：描述里点到的按顺序排前面，剩下用这个流派自带的补齐（补到三个为止）。
    # ⚠️ 不把整张 `kit` 都塞进去：关键词超过 12 个就开始互相稀释（见 MAX_TAGS）。
    named = [i for i in INSTRUMENTS if any(w in low for w in _INSTRUMENT_CUES.get(i["id"], ()))]
    picked = [i["id"] for i in named]
    for i in genre["kit"]:
        if len(picked) >= 3:
            break
        if i in picked:
            continue
        fam = _FAMILY.get(i)
        if fam and any(_FAMILY.get(x) == fam for x in picked):
            continue          # 家族里已经有一个了（而且那是**用户点名**的那个，优先）
        picked.append(i)
    # 「不要鼓」这种**否定**要真的算数：`sparse` 的线索里有它，但鼓的线索里也有「鼓」。
    if any(w in low for w in ("不要鼓", "别打鼓", "no drums", "without drums")):
        picked = [i for i in picked if i not in ("brushed-drums", "soft-percussion")] or ["felt-piano"]

    # 质感：描述点到什么算什么，点到两个以上就收住（再多的词是互相稀释）。
    prod = [i["id"] for i in PRODUCTION if any(w in low for w in _PRODUCTION_CUES.get(i["id"], ()))]
    prod = prod[:2] or list(genre["prod"])[:2]

    mood = _best(_hits(low, _MOOD_CUES), list(_MOOD_CUES))
    if not mood:
        # 没读出来时**跟流派走**，而不是一律 neutral：预告片配乐配「中性」是两回事。
        mood = {"trailer": "epic", "dark-ambient": "tense", "post-rock": "uplifting",
                "lo-fi-hiphop": "warm", "indie-pop": "uplifting"}.get(gid, "neutral")

    bpm = _explicit_bpm(low)
    if not bpm:
        lo, hi = genre["bpm"]
        third = (hi - lo) / 3
        if any(w in low for w in _SLOW_CUES):
            bpm = round(lo + third)
        elif any(w in low for w in _FAST_CUES):
            bpm = round(hi - third)
        else:
            bpm = round((lo + hi) / 2)

    want = str(vocals or "").strip().lower()
    if want in ("", "auto"):
        want = _best(_hits(low, _VOCAL_CUES), list(_VOCAL_CUES)) or "instrumental"
    if str(lyrics or "").strip() and want == "instrumental":
        # 有歌词又要「纯音乐」是自相矛盾的输入；歌词更像用户的本意，所以按歌词走并说出来。
        want = "soft-female"
        notes.append(i18n.pick_now(
            "There are lyrics, so this cannot be instrumental — it will be sung (soft female). "
            "Clear the lyrics for an instrumental bed.",
            "填了歌词就唱不了「纯音乐」—— 这一首会唱出来（柔和女声）。要纯器乐就把歌词清空。"))
    v = _find(VOCALS, want)
    if v is None:
        want = "instrumental"

    return {"genre": gid, "mood": mood, "instruments": picked, "production": prod,
            "vocals": want, "bpm": int(bpm), "notes": notes,
            "matched": {"genre": {k: v for k, v in _hits(low, _GENRE_CUES).items()},
                        "mood": {k: v for k, v in _hits(low, _MOOD_CUES).items()}}}


def vocabulary() -> dict:
    """词表，给**两处**用：面板上剩下的人声下拉，以及 `make_music` 的说明。

    ⚠️ 流派 / 乐器 / 制作那三栏仍然发出去（成员用工具时要照着写标签），但**面板不再画它们** ——
    那正是这一轮改掉的东西。`presets` 同理：面板不画，专区的「模板」那一块读它。
    """
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


def compose_tags(*, genre: str = "", mood: str = "", instruments: list[str] | tuple[str, ...] = (),
                 production: list[str] | tuple[str, ...] = (), vocals: str = "",
                 bpm: int = 0, extra: str = "") -> tuple[str, list[str]]:
    """`(tags, warnings)` — 按官方公式的顺序拼好，并**说出**不合规的地方。

    顺序是硬要求，不是审美：流派必须第一（它锚定其余），其后是情绪、乐器、人声、制作、BPM。
    `warnings` 不是"错误" —— 组合仍然会送去生成，因为用户可能就是要那个效果；它只是把
    「这样多半会糊」这件事**在花钱之前**讲出来。返回空 warnings 才说明这套词是干净的。

    ⚠️ 上了描述框之后，这里的入参**全部来自 `read()`**，不再来自面板。留着这套参数是因为
    「怎么读」和「怎么拼」是两件事，而拼的规矩（顺序、上限、区间）属于后者。
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
