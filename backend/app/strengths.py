"""模型「强项」标签。

强项不是跑分结果,而是根据三类信息推断出来的参考值(用户可以在界面里逐个模型手动改):
  1. 目录里的官方标注:是否支持推理/图片/编码场景、上下文长度、型号档位(旗舰/均衡/快速);
  2. 型号名里的特征词:coder、vl、r1、flash、mini …;
  3. 厂商系列的一般口碑(family prior):例如 Kimi 偏长文本、DeepSeek 偏推理和低成本。
群聊里的「按强项选模型」和主持人分工都读这份标签。
"""

from __future__ import annotations

import re
from typing import Any

# (标签, 说明)。顺序即展示顺序。
TAGS: list[tuple[str, str]] = [
    ("写作", "长文、文案、润色、风格把控"),
    ("代码", "编程、调试、代码审查"),
    ("推理", "复杂推理、数学、多步规划"),
    ("长文本", "超长上下文,适合读大文档、整合资料"),
    ("多模态", "能看图(或音视频),做图文理解"),
    ("速度", "响应快,适合高频、轻量任务"),
    ("低成本", "价格低,适合批量与草稿"),
    ("中文", "中文理解与表达尤其好"),
    ("工具调用", "擅长按格式调用工具、执行智能体任务"),
    ("本地", "在本机运行,数据不出门"),
]
TAG_IDS = [t for t, _ in TAGS]
TAG_DESC = dict(TAGS)

# 厂商系列的一般特点(按型号 ID 匹配)。只给最有把握的 1~3 个标签。
FAMILY_PRIORS: list[tuple[str, list[str]]] = [
    (r"claude", ["写作", "代码", "工具调用"]),
    (r"(^|/)(gpt|o[134])(-|\d|$)|chatgpt", ["推理", "工具调用"]),
    (r"gemini", ["多模态", "长文本"]),
    (r"deepseek", ["推理", "中文", "低成本", "代码"]),
    (r"kimi|moonshot", ["长文本", "写作", "中文"]),
    (r"qwen|qwq|tongyi", ["中文"]),
    (r"glm|chatglm|zai-org|z-ai", ["中文", "工具调用"]),
    (r"doubao|seed", ["中文", "多模态"]),
    (r"minimax|abab", ["长文本", "工具调用"]),
    (r"hunyuan|ernie", ["中文"]),
    (r"grok", ["推理"]),
    (r"mistral|codestral|mixtral", ["代码"]),
]

NAME_HINTS: list[tuple[str, list[str]]] = [
    (r"coder|codex|code|devstral|codestral", ["代码"]),
    (r"(^|[-_/.])(vl|omni|4v|5v)($|[-_/.:\d])|vision|multimodal", ["多模态"]),
    (r"(^|[-_/:.])r1($|[-_:.])|reason|thinking|(^|[-/])o[134](-|$)|qwq", ["推理"]),
    (r"flash|mini|nano|lite|haiku|turbo|luna|air|small|speed|highspeed", ["速度", "低成本"]),
]

LONG_CONTEXT = 2_000_000  # 现在主流模型都有 1M 上下文,不足以区分;只有更长的才算「长文本」强项
MAX_TAGS = 6


def _order(tags: list[str]) -> list[str]:
    seen = set(tags)
    return [t for t in TAG_IDS if t in seen]


def infer(model_id: str, entry: dict | None = None, *, is_local: bool = False) -> list[str]:
    """model_id 用裸模型名(不带服务商前缀)。entry 是目录里的条目(可为空)。
    每条证据带权重:目录官方标注 3 > 型号名特征 2.5 > 厂商系列口碑 2 > 档位/上下文 1.5。取权重最高的 MAX_TAGS 个。"""
    w: dict[str, float] = {}

    def add(tag: str, weight: float) -> None:
        w[tag] = w.get(tag, 0) + weight

    name = model_id.lower()
    e = entry or {}
    if e.get("reasoning") is True:
        add("推理", 3)
    if e.get("vision"):
        add("多模态", 3)
    if e.get("coding"):
        add("代码", 3)
    if e.get("tools"):
        add("工具调用", 3)
    ctx = e.get("context")
    if isinstance(ctx, int) and ctx >= LONG_CONTEXT and not is_local:
        add("长文本", 2)
    tier = e.get("tier")
    if tier == "fast":
        add("速度", 1.5)
        add("低成本", 1.5)
    elif tier == "flagship":
        add("推理", 1.5)
        if not re.search(r"coder|code", name):
            add("写作", 1.5)

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
        add("本地", 10)
        if tier == "fast":  # 小模型的「推理/长文本」标注不可信
            w.pop("推理", None)
            w.pop("长文本", None)

    best = sorted(w, key=lambda t: (-w[t], TAG_IDS.index(t)))[:MAX_TAGS]
    return _order(best)


def clean_tags(tags: Any) -> list[str]:
    """用户提交的标签:只保留已知标签并去重。"""
    if not isinstance(tags, list):
        return []
    return _order([t for t in tags if t in TAG_DESC])


def score(model_tags: list[str], wanted: list[str]) -> float:
    """按需要的强项给模型打分 = 命中的标签数。同分时由路由层按「优先级链顺序 → 云端优先」决胜。"""
    return float(sum(1 for t in wanted if t in model_tags))
