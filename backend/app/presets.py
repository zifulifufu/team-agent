"""内置的模型服务商预设(类似 Cherry Studio 的"添加服务商"列表)。

kind 决定如何映射到 LiteLLM 的 model 字符串:
  deepseek           -> deepseek/<model>
  openai_compatible  -> openai/<model> + api_base   (Kimi、通义千问、智谱、硅基流动、OpenRouter、llama.cpp、LM Studio ...)
  anthropic          -> anthropic/<model>
  gemini             -> gemini/<model>
  ollama             -> ollama_chat/<model> + api_base
"""

from __future__ import annotations

PRESETS: list[dict] = [
    {
        "preset": "deepseek",
        "name": "DeepSeek",
        "kind": "deepseek",
        "base_url": "",
        "is_local": False,
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "hint": "国内首选,路由层默认优先调用。",
    },
    {
        "preset": "moonshot",
        "name": "月之暗面 Kimi",
        "kind": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "is_local": False,
        "models": ["kimi-k3"],
        "hint": "长文本、资料整理见长。点「选择模型」可看到全部型号及强项。",
    },
    {
        "preset": "dashscope",
        "name": "阿里云百炼(通义千问)",
        "kind": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "is_local": False,
        "models": ["qwen3.8-max"],
        "hint": "中文写作、多模态。",
    },
    {
        "preset": "zhipu",
        "name": "智谱 GLM",
        "kind": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "is_local": False,
        "models": ["glm-5.3", "glm-5.3-flash"],
        "hint": "",
    },
    {
        "preset": "siliconflow",
        "name": "硅基流动",
        "kind": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "is_local": False,
        "models": [],
        "hint": "聚合多家开源模型,模型 ID 形如 Qwen/Qwen2.5-72B-Instruct。",
    },
    {
        "preset": "volcengine",
        "name": "火山方舟(豆包)",
        "kind": "openai_compatible",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "is_local": False,
        "models": [],
        "hint": "模型 ID 可以直接填方舟的 Model ID(如 doubao-seed-…),也可以填控制台创建的推理接入点 ID(ep-xxxx);以你账号里实际可用的为准。",
    },
    {
        "preset": "openai",
        "name": "OpenAI",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "is_local": False,
        "models": ["gpt-5.6-sol", "gpt-5.6-luna"],
        "hint": "",
    },
    {
        "preset": "anthropic",
        "name": "Anthropic Claude",
        "kind": "anthropic",
        "base_url": "",
        "is_local": False,
        "models": ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "hint": "",
    },
    {
        "preset": "gemini",
        "name": "Google Gemini",
        "kind": "gemini",
        "base_url": "",
        "is_local": False,
        "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"],
        "hint": "",
    },
    {
        "preset": "openrouter",
        "name": "OpenRouter",
        "kind": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "is_local": False,
        "models": [],
        "hint": "一个 Key 调用国内外大量模型,模型 ID 形如 anthropic/claude-sonnet-4.5。",
    },
    {
        "preset": "minimax",
        "name": "MiniMax",
        "kind": "openai_compatible",
        "base_url": "https://api.minimax.cn/v1",
        "is_local": False,
        "models": [],
        "hint": "长上下文、编程与智能体见长。",
    },
    {
        "preset": "hunyuan",
        "name": "腾讯混元",
        "kind": "openai_compatible",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "is_local": False,
        "models": [],
        "hint": "填入密钥后点「获取模型列表」即可看到当前可用的混元模型。",
    },
    {
        "preset": "xai",
        "name": "xAI Grok",
        "kind": "openai_compatible",
        "base_url": "https://api.x.ai/v1",
        "is_local": False,
        "models": [],
        "hint": "",
    },
    {
        "preset": "mistral",
        "name": "Mistral",
        "kind": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "is_local": False,
        "models": [],
        "hint": "",
    },
    {
        "preset": "ollama",
        "name": "Ollama(本地)",
        "kind": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "is_local": True,
        "models": ["qwen2.5:7b"],
        "hint": "本地兜底模型。外呼被禁用或云端失败时自动使用。",
    },
    {
        "preset": "deepseek-selfhost",
        "name": "DeepSeek(自建服务)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:30000/v1",
        "is_local": True,
        "models": ["deepseek-ai/DeepSeek-V4-Flash"],
        "hint": "接入你自己用 SGLang / vLLM / llama.cpp 部署的 DeepSeek-V4 或 V3;模型名以服务里的为准。",
    },
    {
        "preset": "llamacpp",
        "name": "llama.cpp / LM Studio(本地)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "is_local": True,
        "models": [],
        "hint": "任何本地 OpenAI 兼容服务都可以在这里接入。",
    },
]

PRESET_BY_ID = {p["preset"]: p for p in PRESETS}

DEFAULT_SYSTEM_PROMPT = (
    "你是「{{agent_name}}」({{agent_role}}),正在群聊「{{group_name}}」中与人类用户和其他 AI 成员协作。"
    "今天是 {{date}} {{weekday}}。\n\n"
    "协作规则:\n"
    "1. 先认清自己的强项和分工:只做属于你的部分,别人更擅长的交给别人,不要各说各话,不要重复别人已经给出的内容。\n"
    "2. 需要其他成员帮忙时,用 @成员名 点名并说清楚要他做什么;被点名的成员随后会收到并回复。"
    "不需要别人时不要 @ 任何人,也不要 @ 自己。\n"
    "3. 接着别人的成果往下做,不要复述;有分歧就指出并给出依据。\n"
    "4. 回复直接给结果,不要复述用户的话,不要客套。\n"
    "5. 聊天记录里形如 [名字] 的前缀只是标注发言人,你自己的回复不要加这个前缀。"
)

DEFAULT_SETTINGS: dict = {
    # 关闭后,所有非本地服务商都不会被调用(离线/涉密场景)
    "external_calls_enabled": True,
    # 外部智能体(如 WorkBuddy)总开关:默认关。它们是带工具的智能体,可能读写文件,所以必须由你主动打开
    "external_agents_enabled": False,
    # 路由优先级链:按顺序尝试,最后一个应为本地模型
    "route_chain": ["deepseek/deepseek-flash", "ollama/qwen2.5:7b"],
    # 一次用户消息最多触发多少轮 agent 发言(防止互相 @ 死循环)
    "max_hops": 8,
    # 每次调用带入的群聊历史条数
    "history_limit": 30,
    # 上下文管理:过去的每条消息最多带入多少字(超出的截掉中间),工具结果回填给模型时最多多少字
    "history_clip": 3000,
    "tool_output_limit": 6000,
    # 单次请求超时(秒)
    "request_timeout": 60,
    # 连续失败多少次后熔断,以及熔断时长(秒)
    "circuit_threshold": 2,
    "circuit_cooldown": 30,
    # ---- 提示词
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # ---- 协作:auto=由群主判断是否需要分工;on=总是先分工;off=不分工(只用 @ 接力)
    "plan_mode": "auto",
    "plan_max_tasks": 8,
    # ---- 工具调用:单次发言最多调用几轮工具(0=关闭工具调用)、单个工具超时(秒)
    "tool_rounds": 4,
    "tool_timeout": 60,
    # ---- 权限与操控:工具调用审批
    # ask_risky = 只有会执行代码/有副作用的工具(插件、非只读 MCP)才问;ask_all = 除 current_time 外每个都问;allow_all = 全部放行
    "perm_mode": "ask_risky",
    "perm_timeout": 120,       # 等你确认多少秒,超时按拒绝
    "perm_allow": [],          # 「总是允许」的工具名
    "perm_deny": [],           # 「永远禁止」的工具名
    # ---- 记忆 ⇄ Obsidian:同步到库里的一个文件夹(空 = 不启用)
    "obsidian_dir": "",
    "obsidian_auto": False,    # 开启后每 30 秒自动同步一次
    # ---- 记忆与资料库
    "memory_enabled": True,
    "memory_auto_extract": True,
    "memory_top_k": 6,
    "library_top_k": 5,
    # ---- 更新(全部只读检查;安装代码类扩展必须由你确认)
    "app_repo": "",            # 形如 owner/repo,程序本体的发布仓库
    "catalog_url": "",         # 模型目录 JSON 的地址(可留空,默认取 app_repo 里的 backend/app/data/catalog.json)
    "github_token": "",        # 可选,提高 GitHub API 频率上限
    # 默认关:打开后后端启动 20 秒就会自行联网(GitHub / ollama.com / HuggingFace)。
    # 企业或涉密环境不该出现未经授权的自动外联,要检查时手动点「检查更新」即可。
    "auto_check_updates": False,
    "update_interval_hours": 12,
    "auto_update_skills": False,  # 仅文本技能可以自动更新;插件/MCP/程序本体永远需要手动确认
}

SEED_AGENTS: list[dict] = [
    # Built-in members. The English text is the canonical value stored in the database;
    # `<field>_zh` carries the Chinese wording. `presets.localize_agent()` swaps in the
    # right one for the request language, and the aliases below let both spellings of a
    # name resolve to the same member, so an existing Chinese install and a fresh
    # English one behave identically without any data migration.
    {
        "name": "Aide",
        "name_zh": "小助",
        "avatar": "🧭",
        "role": "Coordinator",
        "role_zh": "协调员",
        "tags": ["reasoning", "tool-use"],
        "prompt": (
            "You are the group's project coordinator. Break the user's request into "
            "subtasks first, then @mention the members best suited to each one. When a "
            "member delivers, you consolidate it, check it, and give the user one clear "
            "final answer."
        ),
        "prompt_zh": (
            "你是群里的项目协调员。收到用户需求后先拆解任务,再用 @成员名 把子任务分配给最合适的成员;"
            "成员交付后由你汇总、把关,并给用户一个清晰的最终答复。"
        ),
    },
    {
        "name": "Copywriter",
        "name_zh": "文案",
        "avatar": "✍️",
        "role": "Copywriter",
        "role_zh": "文案写手",
        "tags": ["writing", "chinese"],
        "prompt": "You are at home in office documents, public-account posts, marketing copy, and creative writing. Your prose is tight, logical, and persuasive.",
        "prompt_zh": "你擅长办公文档、公众号、营销文案与创意写作。文字精炼、有逻辑、有感染力。",
        "skills": ["公文写作规范"],
    },
    {
        "name": "Storyboard",
        "name_zh": "分镜",
        "avatar": "🎬",
        "role": "Video storyboard artist",
        "role_zh": "视频分镜师",
        "tags": ["writing", "reasoning"],
        "prompt": (
            "You are at home in video scripts and storyboards. Always present a table "
            "with: shot number, picture, dialogue or voice-over, duration, camera "
            "movement, and music or sound effects."
        ),
        "prompt_zh": (
            "你擅长视频脚本与分镜设计。输出时用表格列出:镜号、画面、台词/旁白、时长、镜头运动、配乐/音效。"
        ),
        "skills": ["短视频分镜规范"],
    },
    {
        "name": "Proofreader",
        "name_zh": "校对",
        "avatar": "🔍",
        "role": "Proofreader",
        "role_zh": "审校",
        "tags": ["chinese", "reasoning"],
        "prompt": "You check facts, logic, typos, and style consistency. Point out the problems and hand back the corrected version.",
        "prompt_zh": "你负责检查事实、逻辑、错别字与风格一致性,指出问题并给出修改后的版本。",
    },
]


# ---------------------------------------------------------------- 成员预设
# 「随时添加 agent」里的预设库。tags 是这个岗位需要的强项:模型没手动指定时,按这些强项自动挑模型。
AGENT_PRESETS: list[dict] = [
    {
        "key": "host", "name": "Facilitator", "name_zh": "主持", "avatar": "🎙️",
        "role": "Meeting facilitator", "role_zh": "会议主持", "tags": ["reasoning", "tool-use"],
        "prompt": "You chair the discussion: open by framing the topic and the deliverable, keep the pace, call on the right people, close the gaps, and finish with conclusions and action items.",
        "prompt_zh": "你是讨论的主持人:开场界定议题与产出,控制节奏,点名让合适的人发言,收束分歧,最后给出结论和待办。",
    },
    {
        "key": "reviewer", "name": "Reviewer", "name_zh": "评审", "avatar": "🧐",
        "role": "Review officer", "role_zh": "评审官", "tags": ["reasoning", "chinese"],
        "prompt": "You are a strict but constructive reviewer: first acknowledge what is right, then list the problems by importance (facts, logic, risk, feasibility), each with a concrete suggestion.",
        "prompt_zh": "你是严格但建设性的评审官:先肯定做对的部分,再按重要性列出问题(事实、逻辑、风险、可执行性),每条给出改进建议。",
    },
    {
        "key": "scribe", "name": "Scribe", "name_zh": "记录", "avatar": "📝",
        "role": "Note taker", "role_zh": "记录员", "tags": ["speed", "chinese", "low-cost"],
        "prompt": "You keep the minutes: distil the conclusions, the disagreements, and the action items (owner + due date). Never add anything that was not said.",
        "prompt_zh": "你负责会议/讨论纪要:提炼结论、分歧、待办(负责人+时间),不添加没有出现过的内容。",
    },
    {
        "key": "librarian", "name": "Librarian", "name_zh": "资料员", "avatar": "📚",
        "role": "Research librarian", "role_zh": "资料检索员", "tags": ["long-context", "tool-use"],
        "prompt": "You handle research: when a fact is needed, search the library first, then quote the key points with their source (document title). If it is not in the material, say so plainly.",
        "prompt_zh": "你负责查资料:需要事实时先调用资料库检索,再摘录要点并注明出处(文档标题);资料里没有的,明确说没有。",
    },
    {
        "key": "coder", "name": "Programmer", "name_zh": "程序员", "avatar": "💻",
        "role": "Programmer", "role_zh": "程序员", "tags": ["coding", "reasoning", "tool-use"],
        "prompt": "You are a senior programmer: confirm the requirements and constraints first, then give runnable code with a short explanation, noting the edge cases and how to test it.",
        "prompt_zh": "你是资深程序员:先确认需求和约束,给出可运行的代码和简短说明,注明边界情况和测试方法。",
    },
    {
        "key": "translator", "name": "Translator", "name_zh": "翻译", "avatar": "🌐",
        "role": "Translator", "role_zh": "翻译", "tags": ["chinese", "writing", "speed"],
        "prompt": "You translate between Chinese and English: faithful, fluent, and idiomatic in the target language. Keep proper nouns consistent and give the original wording on first use.",
        "prompt_zh": "你负责中英互译:忠实、通顺、符合目标语言的表达习惯;专有名词保持一致并在首次出现时标注原文。",
    },
    {
        "key": "analyst", "name": "Analyst", "name_zh": "分析师", "avatar": "📊",
        "role": "Data analyst", "role_zh": "数据分析师", "tags": ["reasoning", "coding"],
        "prompt": "You are a data analyst: state your definitions and assumptions, show the calculation and the conclusion, and make every number checkable. Say plainly where you are unsure.",
        "prompt_zh": "你是数据分析师:说明口径与假设,给出计算过程和结论,数字要可复核;不确定的地方明说。",
    },
    {
        "key": "planner", "name": "Planner", "name_zh": "策划", "avatar": "💡",
        "role": "Creative planner", "role_zh": "创意策划", "tags": ["writing", "chinese"],
        "prompt": "You are a creative planner: offer three directions in one line each, then develop the chosen one. Every idea must be concrete enough to act on.",
        "prompt_zh": "你是创意策划:先给出 3 个方向各一句话,再展开被选中的方向;点子要具体到能落地。",
    },
    {
        "key": "editor", "name": "Editor", "name_zh": "编辑", "avatar": "🪄",
        "role": "Copy editor", "role_zh": "文字编辑", "tags": ["chinese", "writing"],
        "prompt": "You take someone else's draft to publishable: unify terminology and person, cut filler, fix the grammar, and reorder paragraphs. Give the revised text in full, then explain in as few words as possible what changed. Never change the meaning, and never quietly add content.",
        "prompt_zh": "你负责把别人写好的稿子改到能直接发的程度:统一术语与人称、删冗余、修正语病、理顺段落顺序。"
                      "给出修改后的全文,再用最少的话说明改了什么;不要改变原意,也不要顺手加内容。",
    },
    {
        "key": "qa", "name": "Fact-checker", "name_zh": "质检", "avatar": "✅",
        "role": "Fact and data checking", "role_zh": "事实与数据核查", "tags": ["reasoning", "chinese"],
        "prompt": "You verify: check every fact, number, date, name, and quotation for internal consistency and traceability. Output the problem, the evidence, and the suggested fix; mark anything you cannot trace as unconfirmed, and do not cover for the author.",
        "prompt_zh": "你负责核查:逐条检查事实、数字、日期、人名与引用是否自洽、能否追溯到来源。"
                      "输出「有问题的点 + 依据 + 建议改法」;查不到来源的标为待确认,不要替对方圆场。",
    },
    {
        "key": "researcher", "name": "Researcher", "name_zh": "研究员", "avatar": "🔬",
        "role": "Research and method", "role_zh": "研究与方法", "tags": ["reasoning", "long-context", "tool-use"],
        "prompt": "You turn a question into a verifiable research plan: the research question, the hypotheses, the data and sources needed, the analysis plan, and what result would falsify the hypothesis. Search the library first when facts are needed, and say plainly what is missing.",
        "prompt_zh": "你负责把问题变成可验证的研究安排:给出研究问题、假设、需要的数据与来源、分析口径,"
                      "以及「出现什么结果会推翻这个假设」。需要事实时先查资料库,资料里没有的明确说没有。",
    },
    {
        "key": "risk", "name": "Risk", "name_zh": "风控", "avatar": "🛡️",
        "role": "Risk and compliance", "role_zh": "风险与合规", "tags": ["reasoning", "long-context"],
        "prompt": "You hunt for risk: read the material through the lenses of compliance, security, data privacy, and public wording. Grade each item as high risk / worth noting / acceptable, and write down the trigger and the mitigation. Rather over-flag than miss.",
        "prompt_zh": "你负责挑风险:从合规、安全、数据隐私、对外表述四个角度看这份材料,"
                      "按「高风险 / 需注意 / 可接受」分级,每条写清触发条件与规避办法。宁可提示过度,不要漏。",
    },
    {
        "key": "pm", "name": "Project manager", "name_zh": "项目经理", "avatar": "🗂️",
        "role": "Project management", "role_zh": "项目管理", "tags": ["reasoning", "chinese", "tool-use"],
        "prompt": "You get things moving: break out deliverable milestones and mark the owner, the dependencies, and the inputs for each. Point out the critical path and where delay is most likely, and give one smallest action that can start this week.",
        "prompt_zh": "你负责把事排开:拆出可交付的里程碑,标出每个里程碑的负责人、依赖和输入;"
                      "指出关键路径与最容易延期的地方,并给出一个这周就能启动的最小动作。",
    },
]
AGENT_PRESET_BY_KEY = {a["key"]: a for a in AGENT_PRESETS}


# ---------------------------------------------------------------- 内置成员别名
# Members are matched by name throughout the app (group templates, the preset picker,
# @mentions), and the database stores whichever spelling was in use when the agent was
# created. These aliases let both spellings resolve to the same built-in entry, so a
# Chinese install (agents stored as 小助) and a fresh English one (stored as Aide)
# behave the same without touching anyone's data.
BUILTIN_AGENTS: list[dict] = [*SEED_AGENTS, *AGENT_PRESETS]
BUILTIN_AGENT_ALIASES: dict[str, dict] = {}
for _a in BUILTIN_AGENTS:
    BUILTIN_AGENT_ALIASES[_a["name"]] = _a
    if _a.get("name_zh"):
        BUILTIN_AGENT_ALIASES[_a["name_zh"]] = _a


def builtin_for(name: str | None) -> dict | None:
    """The built-in preset entry a member name belongs to, in either language."""
    return BUILTIN_AGENT_ALIASES.get(name or "")


def builtin_names(entry: dict) -> list[str]:
    """Both spellings of a built-in member's name."""
    return [n for n in (entry.get("name"), entry.get("name_zh")) if n]


def twin_name(name: str | None) -> str | None:
    """The other-language spelling of a built-in member name, if there is one."""
    entry = builtin_for(name)
    if not entry:
        return None
    for n in builtin_names(entry):
        if n != name:
            return n
    return None


def localize_agent(agent: dict, lang: str) -> dict:
    """Show a built-in member in `lang`.

    Only fields whose stored value is still the built-in one are swapped: anything the
    user edited is left exactly as they wrote it. Names match on either spelling, so
    this works for an existing Chinese install and a fresh English one alike.
    """
    entry = builtin_for(agent.get("name"))
    if not entry:
        return agent
    out = dict(agent)
    for field, zh_field in (("name", "name_zh"), ("role", "role_zh"), ("prompt", "prompt_zh")):
        zh = entry.get(zh_field)
        if not zh:
            continue
        current = agent.get(field) or ""
        if current not in {entry.get(field) or "", zh}:
            continue                       # user edited this field — keep their text
        out[field] = zh if lang == "zh" else (entry.get(field) or "")
    return out

# ---------------------------------------------------------------- 群聊模板
TEMPLATES: list[dict] = [
    {
        "id": "office", "name": "办公文档", "scene": "office",
        "desc": "通知、汇报、方案:资料员查资料,文案起草,校对把关。",
        "members": ["Aide", "Librarian", "Copywriter", "Proofreader"], "host": "Aide",
        "skills": ["公文写作规范"], "prompt": "",
    },
    {
        "id": "video", "name": "视频制作", "scene": "video",
        "desc": "选题 → 脚本 → 分镜 → 审校。",
        "members": ["Aide", "Copywriter", "Storyboard", "Proofreader"], "host": "Aide",
        "skills": ["短视频分镜规范"], "prompt": "",
    },
    {
        "id": "writing", "name": "创作写作", "scene": "writing",
        "desc": "策划出方向,文案成稿,校对润色。",
        "members": ["Aide", "Planner", "Copywriter", "Proofreader"], "host": "Aide",
        "skills": [], "prompt": "",
    },
    {
        "id": "brainstorm", "name": "头脑风暴", "scene": "writing",
        "desc": "主持人控场,先发散再收敛,记录员出纪要。",
        "members": ["Facilitator", "Planner", "Reviewer", "Scribe"], "host": "Facilitator",
        "skills": ["头脑风暴规则"], "prompt": "",
    },
    {
        "id": "review", "name": "评审会", "scene": "office",
        "desc": "提交材料 → 多角度评审 → 结论与待办。",
        "members": ["Facilitator", "Reviewer", "Analyst", "Scribe"], "host": "Facilitator",
        "skills": ["评审会规则"], "prompt": "",
    },
    # 以下模板只在「设置 → 模板中心」里展示,不放首页(首页保持 5 张卡片,不喧宾夺主)。
    {
        "id": "research", "name": "资料调研", "scene": "office", "home": False,
        "desc": "资料员检索、分析师核算、记录汇总成调研纪要。",
        "members": ["Aide", "Librarian", "Analyst", "Scribe"], "host": "Aide",
        "skills": ["调研报告规范"], "prompt": "",
    },
    {
        "id": "code", "name": "代码开发", "scene": "office", "home": False,
        "desc": "澄清需求 → 实现 → 评审 → 记录结论。",
        "members": ["Aide", "Programmer", "Reviewer", "Scribe"], "host": "Aide",
        "skills": ["代码评审清单"], "prompt": "",
    },
    {
        "id": "data", "name": "数据分析", "scene": "office", "home": False,
        "desc": "说清口径 → 计算 → 复核 → 结论与限制。",
        "members": ["Aide", "Analyst", "Librarian", "Fact-checker"], "host": "Aide",
        "skills": ["数据分析规范"], "prompt": "",
    },
    {
        "id": "translate", "name": "翻译校对", "scene": "writing", "home": False,
        "desc": "先译后校:术语统一,保留原文对照。",
        "members": ["Aide", "Translator", "Proofreader"], "host": "Aide",
        "skills": ["中英互译规范"], "prompt": "",
    },
    {
        "id": "proposal", "name": "方案撰写", "scene": "office", "home": False,
        "desc": "策划定方向,文案成稿,风控挑风险,编辑收尾。",
        "members": ["Aide", "Planner", "Copywriter", "Risk", "Editor"], "host": "Aide",
        "skills": ["公文写作规范"], "prompt": "",
    },
    {
        "id": "report", "name": "研究报告", "scene": "writing", "home": False,
        "desc": "查资料 → 定口径 → 写 → 核查 → 校对,面向论文与正式报告。",
        "members": ["Aide", "Researcher", "Librarian", "Analyst", "Proofreader"], "host": "Aide",
        "skills": ["研究报告结构"], "prompt": "",
    },
    {
        "id": "clinical", "name": "研究方案讨论", "scene": "office", "home": False,
        "desc": "讨论研究设计、统计口径与合规边界;只谈方法与流程,不要输入患者信息。",
        "members": ["Facilitator", "Researcher", "Analyst", "Risk", "Scribe"], "host": "Facilitator",
        "skills": ["风险自查清单"],
        "prompt": "本群用于讨论研究设计与流程。请勿在此输入任何患者个人信息或可识别数据;"
                  "需要举例时用虚构或脱敏的描述。结论仅作方法层面的讨论,不构成医疗建议。",
    },
]

# ---------------------------------------------------------------- 提示词库示例
SEED_PROMPTS: list[dict] = [
    {
        "title": "先给结论", "kind": "general", "use_globally": False,
        "content": "回答先给一句话结论,再按重要性分点展开;能用数字和例子说明的不要空泛描述。",
    },
    {
        "title": "严谨核查", "kind": "general", "use_globally": False,
        "content": "涉及事实、数据、日期时,注明来源或说明不确定;没有把握就直接说不知道,不要编造。",
    },
    {
        "title": "本群项目背景", "kind": "group", "use_globally": False,
        "content": "项目:{{group_name}}。参与者:{{members}}。请围绕 {{date}} 之前需要交付的成果协作,交付物用 Markdown 输出。",
    },
    {
        "title": "先问清楚再动手", "kind": "general", "use_globally": False,
        "content": "动手前先用不超过 3 个问题确认目标、交付形式和边界;如果信息已经足够,不要反问,"
                   "直接给结果并说明你采用的假设。",
    },
    {
        "title": "输出即交付物", "kind": "general", "use_globally": False,
        "content": "不要描述你打算怎么写,直接给出可用的成品:完整段落、可运行的代码、能直接发出去的文本。"
                   "需要解释时放在成品后面,不要混在成品里。",
    },
    {
        "title": "给出推荐方案", "kind": "general", "use_globally": False,
        "content": "有多个可行方案时,明确推荐一个并说清理由和代价;不要只罗列选项让人自己挑。",
    },
    {
        "title": "交接给下一位", "kind": "group", "use_globally": False,
        "content": "本轮发言结束时,用单独一行写「给下一位:@成员名 + 需要他接着做什么」;"
                   "不需要接力时写「无需接力」。",
    },
]
