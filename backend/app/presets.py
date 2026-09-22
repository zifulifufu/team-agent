"""Built-in model provider presets (like Cherry Studio's "add provider" list).

kind decides how it maps onto the LiteLLM model string:
  deepseek           -> deepseek/<model>
  openai_compatible  -> openai/<model> + api_base   (Kimi, Qwen, Zhipu, SiliconFlow, OpenRouter, llama.cpp, LM Studio ...)
  anthropic          -> anthropic/<model>
  gemini             -> gemini/<model>
  ollama             -> ollama_chat/<model> + api_base
  minimax_video      -> not a chat provider at all: no model string, no model list. It is a
                        video-generation server (MiniMax H3 via SGLang / vLLM) and this app
                        reaches it only through `app/video.py`. `video.MEDIA_KINDS` is the one
                        list of such kinds, and `store.list_models()` keeps them out of the
                        chat model list.
"""

from __future__ import annotations

from . import channels

PRESETS: list[dict] = [
    {
        "preset": "deepseek",
        "name": "DeepSeek",
        "kind": "deepseek",
        "base_url": "",
        "is_local": False,
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "hint": "The first choice in China; the routing layer prefers it by default.", "hint_zh": "国内首选,路由层默认优先调用。",
    },
    {
        "preset": "moonshot",
        "name": "Moonshot Kimi", "name_zh": "月之暗面 Kimi",
        "kind": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "is_local": False,
        "models": ["kimi-k3"],
        "hint": "Strong on long documents and research write-ups. Click Choose model to see every variant and its strengths.", "hint_zh": "长文本、资料整理见长。点「选择模型」可看到全部型号及强项。",
    },
    {
        "preset": "dashscope",
        "name": "Alibaba Cloud Model Studio (Qwen)", "name_zh": "阿里云百炼(通义千问)",
        "kind": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "is_local": False,
        "models": ["qwen3.8-max"],
        "hint": "Chinese writing and multimodal work.", "hint_zh": "中文写作、多模态。",
    },
    {
        "preset": "zhipu",
        "name": "Zhipu GLM", "name_zh": "智谱 GLM",
        "kind": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "is_local": False,
        "models": ["glm-5.3", "glm-5.3-flash"],
        "hint": "",
    },
    {
        "preset": "siliconflow",
        "name": "SiliconFlow", "name_zh": "硅基流动",
        "kind": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "is_local": False,
        "models": [],
        "hint": "Aggregates many open-source models; model IDs look like Qwen/Qwen2.5-72B-Instruct.", "hint_zh": "聚合多家开源模型,模型 ID 形如 Qwen/Qwen2.5-72B-Instruct。",
    },
    {
        "preset": "volcengine",
        "name": "Volcano Ark (Doubao)", "name_zh": "火山方舟(豆包)",
        "kind": "openai_compatible",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "is_local": False,
        "models": [],
        "hint": "The model ID can be Ark's Model ID (such as doubao-seed-...), or an inference endpoint ID created in the console (ep-xxxx) — whichever is actually available in your account.", "hint_zh": "模型 ID 可以直接填方舟的 Model ID(如 doubao-seed-…),也可以填控制台创建的推理接入点 ID(ep-xxxx);以你账号里实际可用的为准。",
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
        "hint": "One key for a large range of models; model IDs look like anthropic/claude-sonnet-4.5.", "hint_zh": "一个 Key 调用国内外大量模型,模型 ID 形如 anthropic/claude-sonnet-4.5。",
    },
    {
        "preset": "minimax",
        "name": "MiniMax",
        "kind": "openai_compatible",
        "base_url": "https://api.minimax.cn/v1",
        "is_local": False,
        "models": [],
        "hint": "Strong on long context, coding and agents.", "hint_zh": "长上下文、编程与智能体见长。",
    },
    {
        "preset": "hunyuan",
        "name": "Tencent Hunyuan", "name_zh": "腾讯混元",
        "kind": "openai_compatible",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "is_local": False,
        "models": [],
        "hint": "Enter the key, then click Fetch model list to see the Hunyuan models available to you.", "hint_zh": "填入密钥后点「获取模型列表」即可看到当前可用的混元模型。",
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
        "preset": "metachat",
        "name": "MetaChat", "name_zh": "MetaChat 元语",
        "kind": "openai_compatible",
        "base_url": "https://llm-api.mmchat.xyz/v1",
        "is_local": False,
        "models": [],
        "hint": "One key for GPT, Claude, Gemini, Grok, DeepSeek, GLM, Kimi and more. Model IDs are the upstream names (gpt-5.2, claude-opus-4-6, gemini-2.5-pro); click Fetch model list after adding. Backup address: https://llm-api.mmchat.dev/v1", "hint_zh": "一个 Key 调用 GPT、Claude、Gemini、Grok、DeepSeek、GLM、Kimi 等。模型 ID 用上游原名(gpt-5.2、claude-opus-4-6、gemini-2.5-pro),添加后点「获取模型列表」。备用地址:https://llm-api.mmchat.dev/v1",
    },
    {
        # The gateway listens on loopback, but it forwards to whatever providers are configured
        # inside Cherry Studio — which are usually cloud. `is_local` has to stay False: it is the
        # flag that lets a provider run while "outbound calls" is off (see router.build_chain), so
        # marking it local would quietly turn the offline guarantee into a lie.
        "preset": "cherry-studio",
        "name": "Cherry Studio (local gateway)", "name_zh": "Cherry Studio(本地网关)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:23333/v1",
        "is_local": False,
        "models": [],
        "hint": "In Cherry Studio: Settings → Tools → API Gateway, start it, then paste the cs-sk- key it generates. The port (23333) is editable after adding. Model IDs are whatever you configured inside Cherry Studio.", "hint_zh": "在 Cherry Studio 里:设置 → 工具 → API Gateway,启动后把它生成的 cs-sk- 密钥填进来。端口(23333)添加后可改。模型 ID 是你在 Cherry Studio 里配好的那些。",
    },
    {
        "preset": "ollama",
        "name": "Ollama (local)", "name_zh": "Ollama(本地)",
        "kind": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "is_local": True,
        "models": ["qwen2.5:7b"],
        "hint": "The local fallback. Used automatically when outbound calls are off, or when the cloud fails.", "hint_zh": "本地兜底模型。外呼被禁用或云端失败时自动使用。",
    },
    {
        "preset": "deepseek-selfhost",
        "name": "DeepSeek (self-hosted)", "name_zh": "DeepSeek(自建服务)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:30000/v1",
        "is_local": True,
        "models": ["deepseek-ai/DeepSeek-V4-Flash"],
        "hint": "Point it at a DeepSeek-V4 or V3 that you deployed yourself with SGLang / vLLM / llama.cpp; use whatever model name your server reports.", "hint_zh": "接入你自己用 SGLang / vLLM / llama.cpp 部署的 DeepSeek-V4 或 V3;模型名以服务里的为准。",
    },
    {
        "preset": "llamacpp",
        "name": "llama.cpp / LM Studio (local)", "name_zh": "llama.cpp / LM Studio(本地)",
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "is_local": True,
        "models": [],
        "hint": "Any local OpenAI-compatible server can be added here.", "hint_zh": "任何本地 OpenAI 兼容服务都可以在这里接入。",
    },
    {
        # A media provider, not a chat provider: it has no /chat/completions and no model list,
        # and this app only ever reaches it through the video API (`app/video.py`). It is
        # offered as a preset rather than seeded, because an endpoint nobody runs would just be
        # a dead row in everyone's provider list.
        "preset": "minimax-h3",
        "name": "MiniMax H3 (self-hosted video)", "name_zh": "MiniMax H3(自建视频生成)",
        "kind": "minimax_video",
        "base_url": "http://127.0.0.1:30010",
        "is_local": True,
        "models": [],
        "hint": "Not a chat model: H3 generates video with stereo audio (4-15s, 768p). Serve it with SGLang or vLLM and point this at that server — port 30010 for the FL2VA checkpoint, 30011 for Ref2VA. Members can then use the generate_video tool. The weights are tens of GB (about 42.5 GB even pruned) and the official example uses 4 GPUs; 2K output and the H3-Context-IR prompt shaper are not open source. On a rented cloud GPU, turn Local off below so the offline switch gates it too.",
        "hint_zh": "不是对话模型:H3 生成带立体声的视频(4-15 秒、768p)。用 SGLang 或 vLLM 跑起来后,这里填那个服务地址 —— FL2VA 检查点用 30010,Ref2VA 用 30011。之后成员就能用 generate_video 工具。权重几十 GB(精简版也要约 42.5 GB),官方示例用 4 张卡;2K 输出与 H3-Context-IR 提示词预处理未开源。如果跑在租来的云 GPU 上,请把下面的「本地」关掉,让「允许外呼」也能管住它。",
    },
    {
        # The one media kind that is reached through a *gateway* rather than a self-hosted
        # server: /images/generations is what OpenAI serves, what MetaChat serves on its
        # OpenAI-compatible address (GPT-Image), and what most aggregators implement. A key
        # and a model name is the whole setup — no GPU, which is why this is offered as a
        # preset while the H3 one above is not seeded.
        "preset": "openai-image",
        "name": "Image generation (OpenAI-compatible)", "name_zh": "绘画(OpenAI 兼容)",
        "kind": "openai_image",
        "base_url": "https://llm-api.mmchat.xyz/v1",
        "is_local": False,
        "models": [],
        "hint": "Not a chat model: it answers /images/generations. The address above is MetaChat's OpenAI-compatible one, where gpt-image-1.5 and the other GPT-Image models live; any other service that speaks the same endpoint works too (put its key below and set the model name under Permissions & control → Image generation). MetaChat's richer image models — Seedream, FLUX, Z-Image, Grok Imagine, Midjourney — use asynchronous endpoints of their own and are not reachable through this kind yet.",
        "hint_zh": "不是对话模型:它提供 /images/generations。上面的地址是 MetaChat 的 OpenAI 兼容入口,gpt-image-1.5 等 GPT-Image 模型在这里;其他实现同一接口的服务也可以(把密钥填在下面,再到「权限与操控 → 绘画」里填模型名)。MetaChat 更强的图像模型 —— Seedream、FLUX、Z-Image、Grok Imagine、Midjourney —— 走的是它们自己的异步接口,这个 kind 目前还够不到。",
    },
]

PRESET_BY_ID = {p["preset"]: p for p in PRESETS}

DEFAULT_SYSTEM_PROMPT = (
    "You are \"{{agent_name}}\" ({{agent_role}}), working in the group chat \"{{group_name}}\" "
    "alongside the human user and the other AI members. "
    "Today is {{date}} {{weekday}}.\n\n"
    "How we work:\n"
    "1. Know your own strengths and your share of the job: do only your part, hand the rest to "
    "whoever is better suited to it, do not talk past each other, and do not repeat what someone "
    "has already said.\n"
    "2. When you need another member, @mention them by name and say exactly what you need from "
    "them; the member you named will receive it and reply. When you need no one, mention no one — "
    "and never mention yourself.\n"
    "3. Build on what others have produced instead of restating it; when you disagree, say so and "
    "give your evidence.\n"
    "4. Answer with the result. Do not paraphrase the user's message and skip the pleasantries.\n"
    "5. A prefix like [Name] in the transcript only marks who is speaking — never add one to your "
    "own reply."
)
DEFAULT_SYSTEM_PROMPT_ZH = (
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
    # when off, no non-local provider is ever called (offline or confidential setups)
    "external_calls_enabled": True,
    # master switch for external agents (e.g. WorkBuddy), off by default. They are agents with
# tools that may read and write files, so you have to turn them on yourself
    "external_agents_enabled": False,
    # routing priority chain: tried in order, the last entry should be a local model
    "route_chain": ["deepseek/deepseek-flash", "ollama/qwen2.5:7b"],
    # how many rounds of agent replies one user message may trigger at most (stops an endless
# @ loop between them)
    "max_hops": 8,
    # how many group chat history messages are included per call
    "history_limit": 30,
    # context management: how many characters each past message may contribute (the middle is
# cut once over), and how many characters a tool result may take when fed back to the model
    "history_clip": 3000,
    "tool_output_limit": 6000,
    # timeout for a single request (seconds)
    "request_timeout": 60,
    # how many consecutive failures trip the breaker, and how long it stays tripped (seconds)
    "circuit_threshold": 2,
    "circuit_cooldown": 30,
    # ---- prompts
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # ---- collaboration: auto = the owner decides whether to delegate; on = always delegate
# first; off = never delegate (@ hand-off only)
    "plan_mode": "auto",
    "plan_max_tasks": 8,
    # ---- tool calls: how many tool rounds one reply may run at most (0 = tool calls off),
# and the timeout of a single tool (seconds)
    "tool_rounds": 4,
    "tool_timeout": 60,
    # ---- permissions and control: tool call approval
    # ask_risky = ask only for tools that run code or have side effects (plugins, non-read-only
# MCP); ask_all = ask for every tool except current_time; allow_all = let everything through
    "perm_mode": "ask_risky",
    "perm_timeout": 120,       # how many seconds to wait for your confirmation, after which it counts as a denial
    "perm_allow": [],          # tool names that are "always allowed"
    "perm_deny": [],           # tool names that are "always forbidden"
    # ---- members writing and running code, off by default. Running code is an `exec`-risk
    # tool, so with the default perm_mode the user is asked before every single run.
    "code_enabled": False,
    "code_timeout": 60,        # how long one run may take before it is killed (seconds)
    "code_workdir": "",        # empty = <data dir>/workspace; only this directory is reachable as cwd
    # ---- images. `vision_cloud` is deliberately separate from `external_calls_enabled`:
    # sending text off the machine and sending a picture the user chose to attach are
    # different decisions, so the second one needs its own yes. Local vision models are
    # never affected by it.
    "vision_cloud": False,
    "vision_max_mb": 8,        # per-image cap, checked before anything is written to disk
    # ---- video generation (MiniMax H3 and anything else speaking the same video API), off by
    # default. Not a chat model: it is reached through its own video endpoints and is gated like
    # any other outbound call, which is why a rented GPU box has to be marked non-local at the
    # provider rather than here.
    "video_enabled": False,
    "video_provider_id": "",   # which video provider to use; empty = the first enabled one
    "video_short_edge": 768,   # output short edge in pixels (H3 is natively 768; 2K needs a module that is not open source)
    "video_max_seconds": 15,   # longest clip a member may ask for (H3 itself accepts 4-15)
    "video_timeout": 900,      # how long one generation may take before giving up, in seconds
    "video_max_mb": 512,       # cap on the downloaded file, checked before it is saved
    # ---- image generation through an OpenAI-compatible /images/generations endpoint. Unlike
    # video this needs no local GPU: a key and a model name are the whole setup, which is why it
    # is the one media capability that works with a gateway such as MetaChat (see the
    # "Image generation (OpenAI-compatible)" preset). `image_model` is a setting rather than a
    # column on the provider because one gateway serves many models and a media provider carries
    # no model of its own.
    "image_enabled": False,
    "image_provider_id": "",   # which image provider to use; empty = the first enabled one
    "image_model": "gpt-image-1",
    "image_size": "1024x1024",
    "image_timeout": 180,      # how long one generation may take, in seconds
    "image_max_mb": 24,        # cap on the downloaded file, checked before it is saved
    # ---- memory <-> Obsidian: one folder in the vault to sync with (empty = disabled)
    "obsidian_dir": "",
    "obsidian_auto": False,    # when enabled, sync automatically every 30 seconds
    # ---- memory and document library
    "memory_enabled": True,
    "memory_auto_extract": True,
    "memory_top_k": 6,
    "library_top_k": 5,
    # ---- updates (every check is read-only; installing code-like extensions always needs
# your confirmation)
    "app_repo": "",            # of the form owner/repo: the release repository of the app itself
    "catalog_url": "",         # URL of the model catalog JSON (may be left empty; defaults to
# backend/app/data/catalog.json inside app_repo)
    "github_token": "",        # optional, raises the GitHub API rate limit
    # off by default: when on, the backend goes online by itself 20 seconds after start
# (GitHub / ollama.com / HuggingFace).
    # corporate or confidential environments must not see unauthorized automatic outbound
# traffic; press "check for updates" by hand when you want to check.
    "auto_check_updates": False,
    "update_interval_hours": 12,
    "auto_update_skills": False,  # only text skills may update automatically; plugins / MCP / the app itself always need
# manual confirmation
    # ---- chat channels (see channels/spec.py, which owns these keys). Off by default: this
    # is the only place where input arrives from *outside* the machine. Three things are
    # deliberately strict — a channel authenticates every request by the platform's own
    # signature (no secret configured = every request refused), only allowlisted senders may
    # speak, and the round they trigger is limited to read-only tools. Any channel needing a
    # public address also needs its hostname registered, because the API otherwise accepts
    # loopback hosts only and nothing could reach it.
    **channels.defaults(),
    # ---- scoring a planned round, so a group can notice its own weak hand-offs (see scoring.py).
    # Off by default: it costs one extra model call per planned round. The judge is meant to be a
    # model that is *not* one of the group's members — a model grading its own answer is the least
    # useful signal available — so an empty `score_judge_model` picks a usable non-member, preferring
    # a local one, and grading fails closed (mechanical signals only) when none is available.
    "scoring_enabled": False,
    "score_judge_model": "",           # empty = pick an eligible model that is not in the group
    "score_threshold": 50,             # percent: below this a task counts as needing rework
    "score_excerpt_chars": 800,        # how much of each deliverable the judge reads (head + tail)
    "score_max_lessons": 1,            # how many lessons one round may write into memory
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
        "skills": ["Office writing conventions"],
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
        "skills": ["Short video storyboards"],
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


# ---------------------------------------------------------------- member presets
# the preset library behind "add an agent at any time". tags are the strengths this role
# needs: when no model is picked by hand, models are chosen from these strengths.
#
# Two groups live here: the general roles below (`kind: "role"`, injected when they are merged)
# and the domain experts in `EXPERT_PRESETS` (`kind: "expert"`). The UI shows them as two
# sections, and both kinds are joined the same way — one click adds the member to a group.
ROLE_PRESETS: list[dict] = [
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
# ---------------------------------------------------------------- domain experts
# Same shape as the roles above, one extra `kind` field so the picker can show a separate section.
# Each entry states how the expert works *and* where its competence stops: an expert that answers
# beyond its remit (inventing a citation, a dose or a guideline grade) is worse than one that says
# what is missing. Add an expert by appending here — nothing else needs to change.
EXPERT_PRESETS: list[dict] = [
    {
        "key": "trial-design", "name": "Trial designer", "name_zh": "临床研究设计专家", "avatar": "🏥",
        "role": "Clinical trial design", "role_zh": "临床研究设计", "kind": "expert",
        "tags": ["reasoning", "long-context"],
        "prompt": "You design clinical studies. Start by pinning the question down in PICO terms, then the design (RCT, cohort, case-control, cross-sectional), the inclusion and exclusion criteria, the primary and secondary endpoints, and the sample-size rationale. Name the biases you are controlling and how (randomisation, blinding, control arm, follow-up), and finish with what makes the study feasible here and now. Where a decision belongs to the statistician or the ethics committee, say so instead of guessing.",
        "prompt_zh": "你负责临床研究设计。先把问题压成 PICO,再定设计类型(随机对照/队列/病例对照/横断面)、入排标准、主要与次要终点、样本量依据。写清你控制了哪些偏倚、怎么控制(随机化、盲法、对照、随访),最后说明在这家中心落地的可行性。该由统计师或伦理委员会拍板的地方直接说明,不要替他们猜。",
    },
    {
        "key": "biostat", "name": "Biostatistician", "name_zh": "医学统计专家", "avatar": "📈",
        "role": "Medical statistics", "role_zh": "医学统计", "kind": "expert",
        "tags": ["reasoning", "coding"],
        "prompt": "You are a medical statistician. Before choosing anything, establish the design, the data types, the groups being compared, and the sample size. Then name the test or model, state the assumptions it needs (distribution, independence, missing data), and how multiple comparisons are handled. Report effect sizes with confidence intervals rather than p-values alone, and keep every number reproducible. You never invent data, and a sample-size estimate comes with its parameters and the formula behind it.",
        "prompt_zh": "你是医学统计专家。选方法之前先确认设计类型、数据类型、比较组与样本量;再给出检验方法或模型,说明它依赖的前提(分布、独立性、缺失数据)以及多重比较如何处理。报告要有效应量和置信区间,不能只给 p 值,每个数字都要可复核。绝不编造数据;样本量估算要给出参数与所用公式。",
    },
    {
        "key": "evidence", "name": "Evidence specialist", "name_zh": "循证医学专家", "avatar": "📖",
        "role": "Evidence and literature", "role_zh": "循证与文献", "kind": "expert",
        "tags": ["reasoning", "long-context", "tool-use"],
        "prompt": "You work from the evidence. Turn the question into a PICO, propose the search (databases, keywords, limits), screen what comes back, and grade the certainty (GRADE or Oxford levels). Say how strong the conclusion is and what would weaken it. Above all: never invent a citation. Quote only what is actually in the group's library, and mark anything whose source you have not seen as needing verification.",
        "prompt_zh": "你以证据为工作基础。把问题转成 PICO,给出检索方案(数据库、关键词、限定条件),筛选返回的文献,并做证据分级(GRADE 或牛津等级)。说明结论有多强、什么情况下会被推翻。最重要的一条:绝不编造文献——只引用群里资料库中确实有的内容;没看过原文的,标注「需核对原文」。",
    },
    {
        "key": "paper", "name": "Academic writer", "name_zh": "论文写作专家", "avatar": "✍️",
        "role": "Academic writing", "role_zh": "学术写作", "kind": "expert",
        "tags": ["writing", "chinese", "long-context"],
        "prompt": "You write and revise manuscripts. Work in IMRaD order — method before results, results before discussion — and give a skeleton before filling it in. The abstract must match the body, table and figure titles must stand on their own, and the discussion has to separate our findings from previous work and name the limitations. Never touch a number, never overstate a conclusion, and never hide a negative result.",
        "prompt_zh": "你负责论文的撰写与修改。按 IMRaD 推进:先方法、再结果、后讨论;先给骨架再落笔。摘要要与正文一致,图表题目要能独立看懂,讨论要区分「本研究结果」和「与既有研究比较」并写明局限。不动一个数字,不夸大结论,不隐瞒阴性结果。",
    },
    {
        "key": "ethics", "name": "Ethics reviewer", "name_zh": "伦理与合规专家", "avatar": "⚖️",
        "role": "Research ethics and compliance", "role_zh": "伦理与合规", "kind": "expert",
        "tags": ["reasoning", "long-context"],
        "prompt": "You review material through research ethics and data compliance: ethics approval, informed consent, protection of vulnerable subjects, personal data (de-identification, minimum necessary, storage, cross-site sharing), and the agreements a multicentre study needs. Grade each item high risk / worth noting / acceptable, and for each one write the trigger and the mitigation. You are not legal advice, and when a local regulation matters you ask for the current text instead of relying on memory.",
        "prompt_zh": "你从研究伦理与数据合规两个角度审材料:伦理批件、知情同意、弱势受试者保护、个人信息(去标识、最小必要、存储、跨中心共享),以及多中心协作需要的协议。逐条按「高风险 / 需注意 / 可接受」分级,每条写清触发条件与缓解办法。你不是法律意见;涉及当地法规时,要求提供最新文本,不要凭记忆下结论。",
    },
    {
        "key": "crf", "name": "Clinical data manager", "name_zh": "数据管理与 CRF 专家", "avatar": "🗃️",
        "role": "Clinical data management", "role_zh": "临床数据管理", "kind": "expert",
        "tags": ["reasoning", "tool-use"],
        "prompt": "You look after the data. Review fields one by one: whether each one is necessary, whether its type is right (date, code, number), whether the unit is stated, whether the allowed values are complete, which logic checks would catch an impossible entry, and how missing and out-of-range values are handled. Define derived variables explicitly, and when a field changes, spell out the effect on data already collected and on the analysis plan.",
        "prompt_zh": "你负责数据。逐字段评审:是否必要、类型是否正确(日期/编码/数值)、单位是否写明、可选值是否齐全、有哪些逻辑校验能拦住不可能的值、缺失与超范围怎么处理。派生变量要写清定义;字段一旦变更,必须说明对已收集数据和分析计划的影响。",
    },
    {
        "key": "guideline", "name": "Guideline interpreter", "name_zh": "临床指南解读专家", "avatar": "📋",
        "role": "Clinical guideline interpretation", "role_zh": "临床指南解读", "kind": "expert",
        "tags": ["reasoning", "long-context", "tool-use"],
        "prompt": "You turn guidelines into something a ward can follow: for each recommendation, what it says, its strength and evidence level, who it applies to, and the practical steps — then the gap between that and the local workflow. Always name the guideline, its version and its year. Never state a recommendation grade from memory; when two guidelines disagree, put them side by side and say where they part.",
        "prompt_zh": "你把指南变成病房能照着做的东西:每条推荐讲清内容、推荐强度与证据级别、适用于谁、实施步骤,再对比本地流程的差距。必须注明指南名称、版本与年份。不要凭记忆写推荐等级;两份指南冲突时并列呈现,并指出分歧在哪里。",
    },
    {
        "key": "stroke", "name": "Stroke specialist", "name_zh": "卒中专病专家", "avatar": "🧠",
        "role": "Acute stroke care", "role_zh": "急性卒中", "kind": "expert",
        "tags": ["reasoning", "long-context"],
        "prompt": "You work on acute stroke: treatment time windows, imaging and vascular assessment, who qualifies for reperfusion therapy and who does not, early complications (haemorrhagic transformation, oedema, dysphagia, DVT), aetiological classification such as TOAST, and secondary prevention. Lay the reasoning out as checkpoints and list the data that is still missing. You support the clinician's decision rather than making it: whenever you name a threshold, say which guideline and version it comes from.",
        "prompt_zh": "你处理急性卒中:治疗时间窗、影像与血管评估、谁适合再灌注治疗、早期并发症(出血转化、脑水肿、吞咽障碍、深静脉血栓)、病因分型(如 TOAST)与二级预防。把判断过程写成检查点,并列出还缺哪些数据。你辅助临床决策而非替代它:给出阈值时,说明它来自哪份指南、哪个版本。",
    },
    {
        "key": "imaging", "name": "Imaging reviewer", "name_zh": "医学影像解读专家", "avatar": "🩻",
        "role": "Medical imaging", "role_zh": "医学影像", "kind": "expert",
        "tags": ["reasoning", "long-context"],
        "prompt": "You read imaging reports and images described to you: state the modality and sequence first, then the findings, the signs and their differential, how they fit the clinical picture, and what to do next. Name the scale or grading system you are using. You do not replace the radiologist — when the information is not enough, say exactly which sequence, plane or phase you would need before answering.",
        "prompt_zh": "你解读影像报告与描述:先写明检查类型与序列,再讲所见、征象与鉴别、与临床是否吻合、下一步建议。使用评分或分级时写明是哪一套标准。你不替代影像科医师——信息不足时,直接说明还需要哪个序列、层面或期相才能回答。",
    },
    {
        "key": "patient", "name": "Patient communicator", "name_zh": "患者沟通专家", "avatar": "💬",
        "role": "Patient communication", "role_zh": "患者沟通", "kind": "expert",
        "tags": ["chinese", "writing"],
        "prompt": "You explain medical matters to patients and families: conclusion first, then why, then what to do. Replace jargon with plain words, and where a term has to stay, explain it once in brackets. For risk and consent, be balanced and concrete — numbers and everyday comparisons rather than adjectives — and never create panic. You make no promises about outcomes, and for any alarming symptom you tell them to seek care now.",
        "prompt_zh": "你把医学内容讲给患者和家属听:先结论、再理由、最后怎么办。把术语换成日常说法,必须保留的术语首次出现时用括号解释。风险与知情沟通要平衡、具体——用量化和生活化的对比,不要用形容词吓人。不承诺疗效;出现需要警惕的症状时,明确提示尽快就医。",
    },
    {
        "key": "med-english", "name": "Medical English editor", "name_zh": "医学英语专家", "avatar": "🌍",
        "role": "Medical English and translation", "role_zh": "医学英语与翻译", "kind": "expert",
        "tags": ["writing", "chinese"],
        "prompt": "You handle Chinese-English medical writing. Keep terminology consistent and give the original term on first use, follow the conventions for numbers and statistics (SD or SE stated, 95% CI, italic p, units), and use the tense and voice journals expect. You never change how strong a claim is — if the Chinese hedges, the English hedges too — and when a term has several accepted translations you offer them instead of silently picking one.",
        "prompt_zh": "你负责中英医学写作。术语保持一致、首次出现标注原文;数字与统计写法符合规范(注明 SD/SE、95% CI、p 值斜体、单位);时态语态符合期刊惯例。不得改变结论的强度——中文留有余地,英文也要留;一个术语有多个通行译法时列出来,不要默默替用户选一个。",
    },
    {
        "key": "pharm", "name": "Drug safety expert", "name_zh": "用药安全专家", "avatar": "💊",
        "role": "Drug safety", "role_zh": "用药安全", "kind": "expert",
        "tags": ["reasoning", "long-context"],
        "prompt": "You check drug therapy: indication, dose (loading and maintenance, renal and hepatic adjustment), interactions, adverse effects, and what has to be monitored. Output the key points plus a short list of what still has to be verified in the chart. A specific dose always comes with its source and version — you never give one from memory — and the decision stays with the treating clinician.",
        "prompt_zh": "你核对药物治疗:适应证、剂量(负荷与维持、肝肾调整)、相互作用、不良反应与需要监测的指标。输出要点,并附一份「还需要在病历里核实什么」的清单。具体剂量必须注明来源与版本,不凭记忆给;最终决定由主管医生做。",
    },
]
# Experts are ordinary members too — same shape, plus the `kind` field the picker groups by.
AGENT_PRESETS: list[dict] = [{**p, "kind": "role"} for p in ROLE_PRESETS] + EXPERT_PRESETS
AGENT_PRESET_BY_KEY = {a["key"]: a for a in AGENT_PRESETS}


# ---------------------------------------------------------------- built-in member aliases
# Members are matched by name throughout the app (group templates, the preset picker,
# @mentions), and the database stores whichever spelling was in use when the agent was
# created. These aliases let both spellings resolve to the same built-in entry, so a
# Chinese install (agents stored under their Chinese names) and a fresh English one (stored as Aide)
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


def builtin_names(entry: dict | None) -> list[str]:
    """Both spellings of a built-in member's name.

    `None` — which is what `builtin_for` answers for a member that is *not* built in — gives no
    names rather than an exception. That is the whole point: three callers in `gallery.py` are
    written as `builtin_names(builtin_for(x)) or [x]`, and the fallback never ran because the
    crash happened one call earlier. With any member of the user's own in the database, the whole
    template gallery answered 500.
    """
    entry = entry or {}
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


# --------------------------------------------------------------- model members
# A member pulled in from "my models" has its role and prompt generated by the app.
# Canonical English is what gets stored; the display layer swaps in the request
# language, and a role or prompt the user edited is left exactly as written — the same
# rule the built-in members follow.
MODEL_MEMBER_PROMPT = (
    "You are the model \"{{model_name}}\" itself, taking part as a member of the group. Play to "
    "your own strengths, and hand the parts you are not good at to whichever member fits better "
    "— do not all just say your own thing."
)
MODEL_MEMBER_PROMPT_ZH = (
    "你就是模型「{{model_name}}」本身,以群成员的身份参与协作。发挥你这个模型的强项承担任务,"
    "不擅长的部分交给更合适的成员,不要各说各话。"
)
MODEL_MEMBER_ROLE = "Model member · {kind}"
MODEL_MEMBER_ROLE_ZH = "模型成员 · {kind}"
MODEL_ROLE_PREFIXES = ("Model member · ", "模型成员 · ")
KIND_LABELS = {"本地": "Local", "云端": "Cloud"}  # i18n-keep: mapping table: the Chinese kind words are the keys, looked up by value
KIND_LABELS_ZH = {en: zh for zh, en in KIND_LABELS.items()}


def model_member_role(kind: str, lang: str = "en") -> str:
    """The generated role of a member pulled in from "my models"."""
    return (MODEL_MEMBER_ROLE if lang == "en" else MODEL_MEMBER_ROLE_ZH).format(kind=kind)


def model_member_prompt(lang: str = "en") -> str:
    """The prompt such a member starts with."""
    return MODEL_MEMBER_PROMPT if lang == "en" else MODEL_MEMBER_PROMPT_ZH


def localize_model_member(agent: dict, lang: str) -> dict:
    """A model member as it should read in `lang`.

    Its role and prompt are generated, so they are swapped whenever they still look
    generated (the built-in kind words map across as well). Anything the user edited is
    left alone.
    """
    if agent.get("origin") != "model":
        return agent
    out = dict(agent)
    role = out.get("role") or ""
    for prefix in MODEL_ROLE_PREFIXES:
        if role.startswith(prefix):
            kind = role[len(prefix):].strip()
            kind = (KIND_LABELS.get(kind, kind) if lang == "en"
                    else KIND_LABELS_ZH.get(kind, kind))
            out["role"] = model_member_role(kind, lang)
            break
    if (out.get("prompt") or "").strip() in {MODEL_MEMBER_PROMPT.strip(), MODEL_MEMBER_PROMPT_ZH.strip()}:
        out["prompt"] = model_member_prompt(lang)
    return out


def localize_provider(prov: dict, lang: str) -> dict:
    """Show a provider's built-in name in `lang`.

    A provider added from a preset keeps that preset's id (a second one added later gets a
    four-character suffix, which is looked through), so the canonical name is recoverable from
    the row itself. As with a member, the name is swapped only while it still equals what the
    preset ships — in either language — so a provider the user renamed keeps their name, and a
    row created before this existed (whose name was stored already localized) is put right again.
    """
    out = dict(prov)
    pid = str(prov.get("id") or "")
    entry = PRESET_BY_ID.get(pid)
    if entry is None and len(pid) > 5 and pid[-5] == "-":
        entry = PRESET_BY_ID.get(pid[:-5])       # `add_provider` disambiguates with `-abcd`
    if entry is None:
        return out
    english, zh = entry.get("name") or "", entry.get("name_zh")
    if not english or not zh or (prov.get("name") or "") not in {english, zh}:
        return out
    out["name"] = zh if lang == "zh" else english
    return out


def provider_name_view(pid: str, name: str, lang: str | None = None) -> str:
    """The same rule for a row that carries only the provider's id and name — a model's join."""
    return localize_provider({"id": pid, "name": name}, lang or i18n.current())["name"]


def localize_member(agent: dict, lang: str) -> dict:
    """Any member as it should read in `lang`.

    Three kinds of member arrive here — a built-in one, one that *is* a model, and one that is
    another application's agent — and each stores its display text from a different source, so
    each needs its own rule. What they share is the property that matters: a field the user
    rewrote is left exactly as they wrote it.
    """
    from . import external      # local: `external` never imports presets, and this keeps it one-way
    return external.localize_member(localize_model_member(localize_agent(agent, lang), lang), lang)


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

# ---------------------------------------------------------------- group chat templates
TEMPLATES: list[dict] = [
    # Group-chat templates. `name`/`desc`/`prompt` are the canonical English text and
    # `<field>_zh` the Chinese wording; templates.create_group_from_template() picks one
    # by request language, and the API returns whichever matches. `skills` points at the
    # canonical skill names in tools.EXAMPLE_SKILLS (aliases resolve the old Chinese ones).
    {
        "id": "office", "name": "Office documents", "name_zh": "办公文档", "scene": "office",
        "desc": "Notices, reports, proposals: the librarian digs out the material, the copywriter drafts, the proofreader checks it.",
        "desc_zh": "通知、汇报、方案:资料员查资料,文案起草,校对把关。",
        "members": ["Aide", "Librarian", "Copywriter", "Proofreader"], "host": "Aide",
        "skills": ["Office writing conventions"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "video", "name": "Video production", "name_zh": "视频制作", "scene": "video",
        "desc": "Topic -> script -> storyboard -> review.",
        "desc_zh": "选题 → 脚本 → 分镜 → 审校。",
        "members": ["Aide", "Copywriter", "Storyboard", "Proofreader"], "host": "Aide",
        "skills": ["Short video storyboards"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "writing", "name": "Creative writing", "name_zh": "创作写作", "scene": "writing",
        "desc": "The planner sets the direction, the copywriter drafts, the proofreader polishes.",
        "desc_zh": "策划出方向,文案成稿,校对润色。",
        "members": ["Aide", "Planner", "Copywriter", "Proofreader"], "host": "Aide",
        "skills": [], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "brainstorm", "name": "Brainstorming", "name_zh": "头脑风暴", "scene": "writing",
        "desc": "The facilitator keeps it moving: diverge first, converge after, and the scribe writes up the notes.",
        "desc_zh": "主持人控场,先发散再收敛,记录员出纪要。",
        "members": ["Facilitator", "Planner", "Reviewer", "Scribe"], "host": "Facilitator",
        "skills": ["Brainstorming rules"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "review", "name": "Review meeting", "name_zh": "评审会", "scene": "office",
        "desc": "Submit the material -> review it from several angles -> verdict and to-dos.",
        "desc_zh": "提交材料 → 多角度评审 → 结论与待办。",
        "members": ["Facilitator", "Reviewer", "Analyst", "Scribe"], "host": "Facilitator",
        "skills": ["Review meeting rules"], "prompt": "", "prompt_zh": "",
    },
    # The templates below only appear in Settings -> Template gallery; the home page keeps
    # its five cards so the important actions stay in front.
    {
        "id": "research", "name": "Research", "name_zh": "资料调研", "scene": "office", "home": False,
        "desc": "The librarian searches, the analyst checks the numbers, the scribe writes the findings up.",
        "desc_zh": "资料员检索、分析师核算、记录汇总成调研纪要。",
        "members": ["Aide", "Librarian", "Analyst", "Scribe"], "host": "Aide",
        "skills": ["Research findings write-up"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "code", "name": "Code development", "name_zh": "代码开发", "scene": "office", "home": False,
        "desc": "Clarify the requirement -> implement -> review -> record the outcome.",
        "desc_zh": "澄清需求 → 实现 → 评审 → 记录结论。",
        "members": ["Aide", "Programmer", "Reviewer", "Scribe"], "host": "Aide",
        "skills": ["Code review checklist"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "data", "name": "Data analysis", "name_zh": "数据分析", "scene": "office", "home": False,
        "desc": "Agree the definitions -> compute -> cross-check -> conclusion and limits.",
        "desc_zh": "说清口径 → 计算 → 复核 → 结论与限制。",
        "members": ["Aide", "Analyst", "Librarian", "Fact-checker"], "host": "Aide",
        "skills": ["Data analysis conventions"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "translate", "name": "Translation and proofreading", "name_zh": "翻译校对",
        "scene": "writing", "home": False,
        "desc": "Translate first, check after: one consistent terminology, source kept alongside.",
        "desc_zh": "先译后校:术语统一,保留原文对照。",
        "members": ["Aide", "Translator", "Proofreader"], "host": "Aide",
        "skills": ["Chinese-English translation"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "proposal", "name": "Proposal writing", "name_zh": "方案撰写", "scene": "office", "home": False,
        "desc": "The planner sets the direction, the copywriter drafts, the risk role hunts for problems, the editor finishes.",
        "desc_zh": "策划定方向,文案成稿,风控挑风险,编辑收尾。",
        "members": ["Aide", "Planner", "Copywriter", "Risk", "Editor"], "host": "Aide",
        "skills": ["Office writing conventions"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "report", "name": "Research report", "name_zh": "研究报告", "scene": "writing", "home": False,
        "desc": "Search -> agree the definitions -> write -> fact-check -> proofread, for papers and formal reports.",
        "desc_zh": "查资料 → 定口径 → 写 → 核查 → 校对,面向论文与正式报告。",
        "members": ["Aide", "Researcher", "Librarian", "Analyst", "Proofreader"], "host": "Aide",
        "skills": ["Research report structure"], "prompt": "", "prompt_zh": "",
    },
    {
        "id": "clinical", "name": "Study design discussion", "name_zh": "研究方案讨论",
        "scene": "office", "home": False,
        "desc": "Discuss the study design, the statistical definitions, and where compliance draws the line. Method and process only — never enter patient information.",
        "desc_zh": "讨论研究设计、统计口径与合规边界;只谈方法与流程,不要输入患者信息。",
        "members": ["Facilitator", "Researcher", "Analyst", "Risk", "Scribe"], "host": "Facilitator",
        "skills": ["Risk self-check"],
        "prompt": "This group is for discussing study design and process. Do not enter any patient "
                  "personal information or identifiable data here; when you need an example, describe "
                  "it in fictional or de-identified terms. Conclusions are methodological discussion "
                  "only and are not medical advice.",
        "prompt_zh": "本群用于讨论研究设计与流程。请勿在此输入任何患者个人信息或可识别数据;"
                     "需要举例时用虚构或脱敏的描述。结论仅作方法层面的讨论,不构成医疗建议。",
    },
]

# ---------------------------------------------------------------- prompt library examples
SEED_PROMPTS: list[dict] = [
    # Prompt library entries seeded on first run. `title` is the canonical English value
    # and therefore the identity that gets stored and de-duplicated against; `title_zh`
    # and `content_zh` carry the Chinese wording. localize_prompt() swaps them in for the
    # request language, and PROMPT_ALIASES makes both spellings of a title resolve to the
    # same entry, so a library seeded before the translation still matches.
    {
        "key": "leading-conclusion", "title": "Lead with the conclusion", "title_zh": "先给结论",
        "kind": "general", "use_globally": False,
        "content": "State the conclusion in one sentence first, then expand point by point in order "
                   "of importance; where a number or an example would make it concrete, use one "
                   "instead of a vague description.",
        "content_zh": "回答先给一句话结论,再按重要性分点展开;能用数字和例子说明的不要空泛描述。",
    },
    {
        "key": "verify-sources", "title": "Verify before you assert", "title_zh": "严谨核查",
        "kind": "general", "use_globally": False,
        "content": "For facts, figures, and dates, cite a source or say that you are unsure; when you "
                   "do not know, say so plainly rather than making something up.",
        "content_zh": "涉及事实、数据、日期时,注明来源或说明不确定;没有把握就直接说不知道,不要编造。",
    },
    {
        "key": "group-background", "title": "Project background for this group", "title_zh": "本群项目背景",
        "kind": "group", "use_globally": False,
        "content": "Project: {{group_name}}. Participants: {{members}}. Work towards the deliverables "
                   "due before {{date}}, and output deliverables as Markdown.",
        "content_zh": "项目:{{group_name}}。参与者:{{members}}。请围绕 {{date}} 之前需要交付的成果协作,"
                      "交付物用 Markdown 输出。",
    },
    {
        "key": "ask-first", "title": "Ask first, then act", "title_zh": "先问清楚再动手",
        "kind": "general", "use_globally": False,
        "content": "Before starting, confirm the goal, the deliverable format, and the boundaries in no "
                   "more than 3 questions; if you already have enough, do not ask back — give the "
                   "result and state the assumptions you made.",
        "content_zh": "动手前先用不超过 3 个问题确认目标、交付形式和边界;如果信息已经足够,不要反问,"
                      "直接给结果并说明你采用的假设。",
    },
    {
        "key": "output-deliverable", "title": "Output the deliverable itself", "title_zh": "输出即交付物",
        "kind": "general", "use_globally": False,
        "content": "Do not describe how you intend to write it — give the finished thing: complete "
                   "paragraphs, runnable code, text that could be sent as it stands. Put any "
                   "explanation after the deliverable, not mixed into it.",
        "content_zh": "不要描述你打算怎么写,直接给出可用的成品:完整段落、可运行的代码、能直接发出去的文本。"
                      "需要解释时放在成品后面,不要混在成品里。",
    },
    {
        "key": "recommend-one", "title": "Recommend one option", "title_zh": "给出推荐方案",
        "kind": "general", "use_globally": False,
        "content": "When several approaches would work, recommend one explicitly and say what it costs; "
                   "do not just list the options and leave the choice to the reader.",
        "content_zh": "有多个可行方案时,明确推荐一个并说清理由和代价;不要只罗列选项让人自己挑。",
    },
    {
        "key": "handoff", "title": "Hand off to the next member", "title_zh": "交接给下一位",
        "kind": "group", "use_globally": False,
        "content": "End your turn with a line of its own reading \"Next: @name + what they should pick "
                   "up\"; if nothing needs handing on, write \"No hand-off needed\".",
        "content_zh": "本轮发言结束时,用单独一行写「给下一位:@成员名 + 需要他接着做什么」;"
                      "不需要接力时写「无需接力」。",
    },
]


# Both spellings of a built-in prompt title resolve to the same entry, so a library
# seeded before the content was translated still de-duplicates correctly.
PROMPT_ALIASES: dict[str, dict] = {}
for _p in SEED_PROMPTS:
    for _name in (_p.get("title"), _p.get("title_zh")):
        if _name:
            PROMPT_ALIASES.setdefault(_name, _p)


def prompt_for(title: str | None) -> dict | None:
    """The built-in prompt a stored title belongs to, in either language."""
    return PROMPT_ALIASES.get(title or "")


def localize_system_prompt(text: str, lang: str) -> str:
    """The system prompt in `lang`, while it is still one of the built-in defaults.

    An install from before the prompt was translated holds the Chinese text; it is
    recognised here so the Prompts page shows one language at a time. Anything the user
    wrote themselves comes back untouched.
    """
    if text == DEFAULT_SYSTEM_PROMPT:
        return DEFAULT_SYSTEM_PROMPT_ZH if lang == "zh" else DEFAULT_SYSTEM_PROMPT
    if text == DEFAULT_SYSTEM_PROMPT_ZH:
        return DEFAULT_SYSTEM_PROMPT if lang == "en" else DEFAULT_SYSTEM_PROMPT_ZH
    return text


def display_name(name: str | None, lang: str) -> str:
    """A built-in member's name as it should be shown in `lang`."""
    entry = builtin_for(name)
    if not entry:
        return name or ""
    return (entry.get("name_zh") if lang == "zh" else entry.get("name")) or name or ""


def prompt_key(title: str | None) -> str | None:
    """The stable key of the built-in prompt a stored title belongs to."""
    entry = prompt_for(title)
    return entry.get("key") if entry else None


def prompt_titles(title: str | None) -> list[str]:
    """Every spelling a stored prompt title might have been written in."""
    entry = prompt_for(title)
    if not entry:
        return [title] if title else []
    return [n for n in (entry.get("title"), entry.get("title_zh")) if n]


def localize_prompt(row: dict, lang: str) -> dict:
    """Show a stored prompt in `lang`, leaving anything the user edited alone."""
    entry = prompt_for(row.get("title"))
    if not entry:
        return row
    out = dict(row)
    for field, zh_field in (("title", "title_zh"), ("content", "content_zh")):
        zh = entry.get(zh_field)
        base = entry.get(field) or ""
        if not zh:
            continue
        current = row.get(field) or ""
        if current not in {base, zh}:
            continue                       # user edited this field — keep their text
        out[field] = zh if lang == "zh" else base
    return out
