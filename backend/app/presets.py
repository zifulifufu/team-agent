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
    {
        "name": "小助",
        "avatar": "🧭",
        "role": "协调员",
        "tags": ["推理", "工具调用"],
        "prompt": (
            "你是群里的项目协调员。收到用户需求后先拆解任务,再用 @成员名 把子任务分配给最合适的成员;"
            "成员交付后由你汇总、把关,并给用户一个清晰的最终答复。"
        ),
    },
    {
        "name": "文案",
        "avatar": "✍️",
        "role": "文案写手",
        "tags": ["写作", "中文"],
        "prompt": "你擅长办公文档、公众号、营销文案与创意写作。文字精炼、有逻辑、有感染力。",
        "skills": ["公文写作规范"],
    },
    {
        "name": "分镜",
        "avatar": "🎬",
        "role": "视频分镜师",
        "tags": ["写作", "推理"],
        "prompt": (
            "你擅长视频脚本与分镜设计。输出时用表格列出:镜号、画面、台词/旁白、时长、镜头运动、配乐/音效。"
        ),
        "skills": ["短视频分镜规范"],
    },
    {
        "name": "校对",
        "avatar": "🔍",
        "role": "审校",
        "tags": ["中文", "推理"],
        "prompt": "你负责检查事实、逻辑、错别字与风格一致性,指出问题并给出修改后的版本。",
    },
]


# ---------------------------------------------------------------- 成员预设
# 「随时添加 agent」里的预设库。tags 是这个岗位需要的强项:模型没手动指定时,按这些强项自动挑模型。
AGENT_PRESETS: list[dict] = [
    {
        "key": "host", "name": "主持", "avatar": "🎙️", "role": "会议主持", "tags": ["推理", "工具调用"],
        "prompt": "你是讨论的主持人:开场界定议题与产出,控制节奏,点名让合适的人发言,收束分歧,最后给出结论和待办。",
    },
    {
        "key": "reviewer", "name": "评审", "avatar": "🧐", "role": "评审官", "tags": ["推理", "中文"],
        "prompt": "你是严格但建设性的评审官:先肯定做对的部分,再按重要性列出问题(事实、逻辑、风险、可执行性),每条给出改进建议。",
    },
    {
        "key": "scribe", "name": "记录", "avatar": "📝", "role": "记录员", "tags": ["速度", "中文", "低成本"],
        "prompt": "你负责会议/讨论纪要:提炼结论、分歧、待办(负责人+时间),不添加没有出现过的内容。",
    },
    {
        "key": "librarian", "name": "资料员", "avatar": "📚", "role": "资料检索员", "tags": ["长文本", "工具调用"],
        "prompt": "你负责查资料:需要事实时先调用资料库检索,再摘录要点并注明出处(文档标题);资料里没有的,明确说没有。",
    },
    {
        "key": "coder", "name": "程序员", "avatar": "💻", "role": "程序员", "tags": ["代码", "推理", "工具调用"],
        "prompt": "你是资深程序员:先确认需求和约束,给出可运行的代码和简短说明,注明边界情况和测试方法。",
    },
    {
        "key": "translator", "name": "翻译", "avatar": "🌐", "role": "翻译", "tags": ["中文", "写作", "速度"],
        "prompt": "你负责中英互译:忠实、通顺、符合目标语言的表达习惯;专有名词保持一致并在首次出现时标注原文。",
    },
    {
        "key": "analyst", "name": "分析师", "avatar": "📊", "role": "数据分析师", "tags": ["推理", "代码"],
        "prompt": "你是数据分析师:说明口径与假设,给出计算过程和结论,数字要可复核;不确定的地方明说。",
    },
    {
        "key": "planner", "name": "策划", "avatar": "💡", "role": "创意策划", "tags": ["写作", "中文"],
        "prompt": "你是创意策划:先给出 3 个方向各一句话,再展开被选中的方向;点子要具体到能落地。",
    },
]
AGENT_PRESET_BY_KEY = {a["key"]: a for a in AGENT_PRESETS}

# ---------------------------------------------------------------- 群聊模板
TEMPLATES: list[dict] = [
    {
        "id": "office", "name": "办公文档", "scene": "office",
        "desc": "通知、汇报、方案:资料员查资料,文案起草,校对把关。",
        "members": ["小助", "资料员", "文案", "校对"], "host": "小助",
        "skills": ["公文写作规范"], "prompt": "",
    },
    {
        "id": "video", "name": "视频制作", "scene": "video",
        "desc": "选题 → 脚本 → 分镜 → 审校。",
        "members": ["小助", "文案", "分镜", "校对"], "host": "小助",
        "skills": ["短视频分镜规范"], "prompt": "",
    },
    {
        "id": "writing", "name": "创作写作", "scene": "writing",
        "desc": "策划出方向,文案成稿,校对润色。",
        "members": ["小助", "策划", "文案", "校对"], "host": "小助",
        "skills": [], "prompt": "",
    },
    {
        "id": "brainstorm", "name": "头脑风暴", "scene": "writing",
        "desc": "主持人控场,先发散再收敛,记录员出纪要。",
        "members": ["主持", "策划", "评审", "记录"], "host": "主持",
        "skills": ["头脑风暴规则"], "prompt": "",
    },
    {
        "id": "review", "name": "评审会", "scene": "office",
        "desc": "提交材料 → 多角度评审 → 结论与待办。",
        "members": ["主持", "评审", "分析师", "记录"], "host": "主持",
        "skills": ["评审会规则"], "prompt": "",
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
]
