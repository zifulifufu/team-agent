"""Model "strength" tags.

A strength tag is not a benchmark result: it is inferred from three kinds of
evidence, and the user can override it per model in the UI:

  1. what the catalog states: reasoning / vision / coding support, context length,
     tier (flagship / balanced / fast);
  2. hints in the model name: coder, vl, r1, flash, mini ...
  3. the general reputation of the vendor family (family priors), e.g. Kimi leans
     long-context, DeepSeek leans reasoning and low cost.

"Pick a model by strength" in a group chat and the host's task assignment both
read these tags.

Tag ids are stable ASCII identifiers stored in the database, so they must not be
translated. Display labels and descriptions are localized: English is the
default, Chinese is used when the UI language is Chinese.
"""

from __future__ import annotations

import re
from typing import Any

# (id, English description). The order is the display order.
TAGS: list[tuple[str, str]] = [
    ("writing", "Long-form writing, copy, polishing, tone control"),
    ("coding", "Programming, debugging, code review"),
    ("reasoning", "Complex reasoning, maths, multi-step planning"),
    ("long-context", "Very long context; good at reading big documents and merging material"),
    ("multimodal", "Can look at images (or audio/video) and understand them"),
    ("speed", "Fast responses; good for frequent, lightweight tasks"),
    ("low-cost", "Cheap; good for bulk work and drafts"),
    ("chinese", "Especially good at understanding and writing Chinese"),
    ("tool-use", "Reliably calls tools in the required format and runs agent tasks"),
    ("local", "Runs on this machine, so data never leaves it"),
]
TAG_IDS = [t for t, _ in TAGS]
TAG_DESC = dict(TAGS)

# id -> (English label, Chinese label)
LABELS: dict[str, tuple[str, str]] = {
    "writing": ("Writing", "写作"),
    "coding": ("Coding", "代码"),
    "reasoning": ("Reasoning", "推理"),
    "long-context": ("Long context", "长文本"),
    "multimodal": ("Multimodal", "多模态"),
    "speed": ("Speed", "速度"),
    "low-cost": ("Low cost", "低成本"),
    "chinese": ("Chinese", "中文"),
    "tool-use": ("Tool use", "工具调用"),
    "local": ("Local", "本地"),
}

DESCS_ZH: dict[str, str] = {
    "writing": "长文、文案、润色、风格把控",
    "coding": "编程、调试、代码审查",
    "reasoning": "复杂推理、数学、多步规划",
    "long-context": "超长上下文,适合读大文档、整合资料",
    "multimodal": "能看图(或音视频),做图文理解",
    "speed": "响应快,适合高频、轻量任务",
    "low-cost": "价格低,适合批量与草稿",
    "chinese": "中文理解与表达尤其好",
    "tool-use": "擅长按格式调用工具、执行智能体任务",
    "local": "在本机运行,数据不出门",
}

# Older builds stored the Chinese label itself as the tag id. Keep accepting those
# so existing databases and plugins keep working; `normalize_tag()` maps them over.
ALIASES: dict[str, str] = {zh: en for en, (_, zh) in LABELS.items()}

# Vendor family traits (matched against the model id). At most 1-3 confident tags.
FAMILY_PRIORS: list[tuple[str, list[str]]] = [
    (r"claude", ["writing", "coding", "tool-use"]),
    (r"(^|/)(gpt|o[134])(-|\d|$)|chatgpt", ["reasoning", "tool-use"]),
    (r"gemini", ["multimodal", "long-context"]),
    (r"deepseek", ["reasoning", "chinese", "low-cost", "coding"]),
    (r"kimi|moonshot", ["long-context", "writing", "chinese"]),
    (r"qwen|qwq|tongyi", ["chinese"]),
    (r"glm|chatglm|zai-org|z-ai", ["chinese", "tool-use"]),
    (r"doubao|seed", ["chinese", "multimodal"]),
    (r"minimax|abab", ["long-context", "tool-use"]),
    (r"hunyuan|ernie", ["chinese"]),
    (r"grok", ["reasoning"]),
    (r"mistral|codestral|mixtral", ["coding"]),
]

NAME_HINTS: list[tuple[str, list[str]]] = [
    (r"coder|codex|code|devstral|codestral", ["coding"]),
    (r"(^|[-_/.])(vl|omni|4v|5v)($|[-_/.:\d])|vision|multimodal", ["multimodal"]),
    (r"(^|[-_/:.])r1($|[-_:.])|reason|thinking|(^|[-/])o[134](-|$)|qwq", ["reasoning"]),
    (r"flash|mini|nano|lite|haiku|turbo|luna|air|small|speed|highspeed", ["speed", "low-cost"]),
]

LONG_CONTEXT = 2_000_000  # mainstream models all have 1M context now, so only longer counts
MAX_TAGS = 6


def normalize_tag(tag: object) -> str | None:
    """Accept a current id or a legacy Chinese label; return the canonical id."""
    if not isinstance(tag, str):
        return None
    t = tag.strip()
    if t in TAG_DESC:
        return t
    if t in ALIASES:
        return ALIASES[t]
    low = t.lower()
    return low if low in TAG_DESC else None


def label(tag: str, lang: str = "en") -> str:
    """Display label for a tag id, in the requested language."""
    en, zh = LABELS.get(tag, (tag, tag))
    return zh if lang == "zh" else en


def description(tag: str, lang: str = "en") -> str:
    """Longer explanation for a tag id, in the requested language."""
    if lang == "zh":
        return DESCS_ZH.get(tag, TAG_DESC.get(tag, ""))
    return TAG_DESC.get(tag, "")


def _order(tags: list[str]) -> list[str]:
    seen = set(tags)
    return [t for t in TAG_IDS if t in seen]


def infer(model_id: str, entry: dict | None = None, *, is_local: bool = False) -> list[str]:
    """``model_id`` is the bare model name (no provider prefix). ``entry`` is the
    catalog row, if any. Each kind of evidence carries a weight: catalog 3 >
    name hints 2.5 > family reputation 2 > tier/context 1.5; the top MAX_TAGS win."""
    w: dict[str, float] = {}

    def add(tag: str, weight: float) -> None:
        w[tag] = w.get(tag, 0) + weight

    name = model_id.lower()
    e = entry or {}
    if e.get("reasoning") is True:
        add("reasoning", 3)
    if e.get("vision"):
        add("multimodal", 3)
    if e.get("coding"):
        add("coding", 3)
    if e.get("tools"):
        add("tool-use", 3)
    ctx = e.get("context")
    if isinstance(ctx, int) and ctx >= LONG_CONTEXT and not is_local:
        add("long-context", 2)
    tier = e.get("tier")
    if tier == "fast":
        add("speed", 1.5)
        add("low-cost", 1.5)
    elif tier == "flagship":
        add("reasoning", 1.5)
        if not re.search(r"coder|code", name):
            add("writing", 1.5)

    for pat, ts in NAME_HINTS:
        if re.search(pat, name):
            for t in ts:
                add(t, 2.5)
    for pat, ts in FAMILY_PRIORS:
        if re.search(pat, name):
            for t in ts[:4]:
                add(t, 2)
            break
    if is_local:
        add("local", 10)
        if tier == "fast":  # "reasoning/long-context" claims on small models are not reliable
            w.pop("reasoning", None)
            w.pop("long-context", None)

    best = sorted(w, key=lambda t: (-w[t], TAG_IDS.index(t)))[:MAX_TAGS]
    return _order(best)


def clean_tags(tags: Any) -> list[str]:
    """Tags submitted by a client: keep the known ones, canonicalize and de-duplicate."""
    if not isinstance(tags, list):
        return []
    return _order([t for t in (normalize_tag(x) for x in tags) if t])


def score(model_tags: list[str], wanted: list[str]) -> float:
    """How well a model matches the wanted strengths = number of matching tags.
    Ties are broken by the router using chain order and cloud-before-local."""
    return float(sum(1 for t in wanted if t in model_tags))
