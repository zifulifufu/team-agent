"""Skills (text skills) and plugins (Python tools). See mcp_client.py for MCP and
toolhub.py for unified dispatch.

  * Skills -- data directory skills/<name>/SKILL.md. Can be ticked per member (injected
              into that member's prompt) or attached to a whole group (scope: group, the
              "group rules" kind, followed by everyone). Plain text, works with any model.
  * Plugins -- data directory plugins/*.py, each defining register(registry) to register
              some tools. Plugins are Python code running inside this process with no
              sandbox: only install ones you have read and trust.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import i18n

# ------------------------------------------------------------------- skills
EXAMPLE_SKILLS: dict[str, dict] = {
    # Built-in example skills. As with SEED_AGENTS the English text is the canonical
    # value — it is what gets written to skills/<name>/SKILL.md and what reaches the
    # model — and `<field>_zh` carries the Chinese wording. `localskill()` below swaps
    # in the right one for the request language, and SKILL_ALIASES lets either spelling
    # of a name resolve to the same entry, so a Chinese install whose skills were
    # seeded under their Chinese names and a fresh English one behave the same.
    "office-writing": {
        "name": "Office writing conventions", "name_zh": "公文写作规范",
        "description": "Structure and wording for office documents",
        "description_zh": "办公文档写作的结构与措辞要求", "scope": "member",
        "body": "When writing an office document (notice, report, proposal), follow:\n"
                "1. Open with one sentence giving the purpose and the conclusion;\n"
                "2. Break the body into points, one piece of information each;\n"
                "3. Every figure carries its definition and its date;\n"
                "4. Close with the next action, who owns it, and the deadline;\n"
                "5. Keep the tone formal and restrained — no colloquialisms, no hype.",
        "body_zh": "写办公文档(通知、汇报、方案)时遵循:\n"
                   "1. 开头一句话交代目的和结论;\n2. 正文分点,每点一个信息;\n"
                   "3. 数据要带口径和时间;\n4. 结尾写明下一步动作、负责人和时间节点;\n5. 语气正式、克制,不用口语和夸张修辞。",
    },
    "short-video-storyboard": {
        "name": "Short video storyboards", "name_zh": "短视频分镜规范",
        "description": "Output format for short-video scripts and storyboards",
        "description_zh": "短视频脚本与分镜的输出格式", "scope": "member",
        "body": "When producing a short-video script:\n"
                "1. State the one-line theme and the target audience first;\n"
                "2. The first 3 seconds must carry a hook;\n"
                "3. The storyboard table has: shot no. | picture | voice-over or dialogue | duration | camera | sound effects;\n"
                "4. Keep the total duration inside what the user asked for and show the total at the end.",
        "body_zh": "输出短视频脚本时:\n1. 先给一句话主题和目标受众;\n"
                   "2. 前 3 秒必须有钩子;\n3. 分镜表列:镜号 | 画面 | 旁白/台词 | 时长 | 镜头 | 音效;\n"
                   "4. 总时长控制在用户要求内,并在末尾给出总时长核算。",
    },
    "brainstorming": {
        "name": "Brainstorming rules", "name_zh": "头脑风暴规则",
        "description": "Group prompt: diverge first, converge second",
        "description_zh": "群聊提示词:先发散再收敛的讨论规则", "scope": "group",
        "body": "This group is in brainstorming mode:\n"
                "1. Diverge: everyone offers at least 2 distinct directions, no judging yet, and no repeating what has already been said;\n"
                "2. Converge: the facilitator leads the group through screening by feasibility / value / cost and narrows it to 1-2 directions;\n"
                "3. Every idea must say in one sentence who does what and why it works;\n"
                "4. The scribe closes with: the directions, the reasoning, and the next step.",
        "body_zh": "本群进入头脑风暴模式:\n"
                   "1. 发散阶段:每人至少提出 2 个不同方向,先不评判,不重复别人已经说过的点子;\n"
                   "2. 收敛阶段:主持人带大家按「可行性 / 价值 / 成本」筛出 1~2 个方向;\n"
                   "3. 每个点子一句话说清楚「谁、做什么、为什么有效」;\n4. 最后由记录员整理:方向、理由、下一步。",
    },
    "review-meeting": {
        "name": "Review meeting rules", "name_zh": "评审会规则",
        "description": "Group prompt: review-meeting flow and output format",
        "description_zh": "群聊提示词:评审会的流程与输出格式", "scope": "group",
        "body": "This group is in review-meeting mode:\n"
                "1. The facilitator restates what is being reviewed and against which criteria;\n"
                "2. Each reviewer speaks from their own expertise, grades every finding critical / moderate / suggestion, and offers a fix;\n"
                "3. No generic praise and no remarks about people; when you disagree, show your evidence;\n"
                "4. The facilitator closes with a verdict (pass / pass after changes / fail) and a to-do list (owner + deadline).",
        "body_zh": "本群进入评审会模式:\n"
                   "1. 主持人先复述评审对象与评审标准;\n"
                   "2. 评审成员各自从自己的专业角度给出意见,按「严重 / 一般 / 建议」分级,并给出改进办法;\n"
                   "3. 不要泛泛表扬,不要人身评价;有分歧时各自给依据;\n"
                   "4. 最后由主持人给出结论(通过 / 修改后通过 / 不通过)和待办清单(负责人 + 时间)。",
    },
    "relay-writing": {
        "name": "Relay writing rules", "name_zh": "接力创作规则",
        "description": "Group prompt: how consecutive writers hand off",
        "description_zh": "群聊提示词:多人接力写作的衔接规则", "scope": "group",
        "body": "This group is in relay-writing mode:\n"
                "1. Each writer must carry forward the previous writer's characters, setting, and tone — never overturn what is already established;\n"
                "2. Write only your own section, then leave one sentence for the next writer about the threads to watch;\n"
                "3. The proofreader closes by unifying style, timeline, and names.",
        "body_zh": "本群进入接力创作模式:\n"
                   "1. 后一位必须承接前一位的人物、设定和语气,不推翻已有设定;\n"
                   "2. 每人只写自己负责的那一段,写完用一句话交代给下一位需要注意的伏笔;\n"
                   "3. 最后由校对统一文风、时间线和人名。",
    },
    "debate": {
        "name": "Debate rules", "name_zh": "辩论规则",
        "description": "Group prompt: proposition vs. opposition, and the judge's summary",
        "description_zh": "群聊提示词:正反方辩论与裁判总结", "scope": "group",
        "body": "This group is in debate mode:\n"
                "1. The facilitator defines the motion; the proposition and the opposition each set out 3 arguments with evidence;\n"
                "2. Cross-examination targets only the gaps in the other side's arguments — do not restate your own position;\n"
                "3. The judge closes with each side's strongest argument, each side's weakness, and a leaning with reasons.",
        "body_zh": "本群进入辩论模式:\n"
                   "1. 主持人先界定辩题;正方、反方各陈述 3 个论点并给出论据;\n"
                   "2. 交叉质询:只针对对方论点的漏洞,不重复自己的立场;\n"
                   "3. 裁判总结:双方最强的论点、各自的漏洞、最终倾向及理由。",
    },
    "code-review": {
        "name": "Code review checklist", "name_zh": "代码评审清单",
        "description": "The order to check code in during a review",
        "description_zh": "评审代码时按顺序检查的清单", "scope": "member",
        "body": "Review code in this order:\n"
                "1. Is it the right thing: is this what was asked for? Are the edge cases handled (empty values, oversized input, concurrency, retries)?\n"
                "2. Can it fail: exception paths, resource release, out-of-range and null values;\n"
                "3. Security: input validation, injection, secrets or sensitive data in logs;\n"
                "4. Maintainability: naming, single responsibility, duplicated code, tests where they are needed;\n"
                "5. Output: grade every finding critical / moderate / suggestion, and give its location, the problem, and the fix. Never settle for \"consider optimizing\".",
        "body_zh": "评审代码时按这个顺序看:\n"
                   "1. 需求对不对:实现的是不是要求的事?边界条件(空值、超长、并发、失败重试)处理了吗?\n"
                   "2. 会不会出错:异常路径、资源释放、越界与空值;\n"
                   "3. 安全:输入校验、注入、密钥与日志里有没有敏感信息;\n"
                   "4. 可维护:命名、职责是否单一、重复代码、有没有必要的测试;\n"
                   "5. 输出格式:按「严重 / 一般 / 建议」分级,每条给出位置、问题、改法;不要只说「建议优化」。",
    },
    "research-findings": {
        "name": "Research findings write-up", "name_zh": "调研报告规范",
        "description": "Structure and evidence requirements for research output",
        "description_zh": "调研类输出的结构与证据要求", "scope": "member",
        "body": "When writing up research findings:\n"
                "1. Open with one sentence giving the conclusion and the recommendation;\n"
                "2. Cite a source for every conclusion (document title / link / data definition); mark anything uncited as \"unverified\";\n"
                "3. Keep facts, inferences, and opinions in separate sections rather than mixed together;\n"
                "4. Proactively list counter-evidence or uncertainty;\n"
                "5. Close with what further information is needed to settle the question.",
        "body_zh": "写调研结果时:\n"
                   "1. 开头一句话给结论与建议;\n"
                   "2. 每个结论后面标出处(文档标题 / 链接 / 数据口径),没有出处的标「未核实」;\n"
                   "3. 把事实、推断、观点分开写,不要混着写;\n"
                   "4. 主动列出反面证据或不确定性;\n"
                   "5. 结尾给出为了定论还需要补的信息。",
    },
    "data-analysis": {
        "name": "Data analysis conventions", "name_zh": "数据分析规范",
        "description": "Definitions, calculations, and limits for data conclusions",
        "description_zh": "数据结论的口径、计算与局限说明", "scope": "member",
        "body": "When giving a data conclusion:\n"
                "1. State the definitions first: data range, time window, sample size, filters;\n"
                "2. Write out the calculation so someone else can reproduce it (formula or steps);\n"
                "3. Give units and magnitude on numbers; when reporting a change, give both the percentage and the absolute value;\n"
                "4. Note data-quality problems and limits, and never present correlation as causation;\n"
                "5. Put the conclusion in the first sentence, with the evidence after it.",
        "body_zh": "给数据结论时:\n"
                   "1. 先说口径:数据范围、时间窗、样本量、筛选条件;\n"
                   "2. 写清计算方式,让别人能复核(给公式或步骤);\n"
                   "3. 数字带单位与量级;说变化时百分比与绝对值都给;\n"
                   "4. 说明数据质量问题与局限,不要把相关性说成因果;\n"
                   "5. 结论一句话放开头,证据跟在后。",
    },
    "translation": {
        "name": "Chinese-English translation", "name_zh": "中英互译规范",
        "description": "Tone and terminology consistency for Chinese-English translation",
        "description_zh": "中英互译的语气与术语一致性要求", "scope": "member",
        "body": "When translating between Chinese and English:\n"
                "1. Read the whole text first and settle the register (formal / conversational / technical), then keep it consistent throughout;\n"
                "2. Translate proper nouns consistently; on first mention gloss the original in brackets;\n"
                "3. Do not translate word for word — recast the sentences the way the target language prefers;\n"
                "4. Keep numbers, units, and citations exactly as they are;\n"
                "5. For long texts, output paragraph by paragraph so the work can be checked sentence by sentence.",
        "body_zh": "做中英互译时:\n"
                   "1. 先通读全文定语气(正式 / 口语 / 技术文档),整篇保持一致;\n"
                   "2. 专有名词统一译法,首次出现时用「中文(English)」标注原文;\n"
                   "3. 不逐字硬译,按目标语言的表达习惯重组句子;\n"
                   "4. 原样保留数字、单位与引用;\n"
                   "5. 长文分段对照输出,方便逐句校对。",
    },
    "report-structure": {
        "name": "Research report structure", "name_zh": "研究报告结构",
        "description": "Section skeleton for a formal research report or paper",
        "description_zh": "正式研究报告 / 论文的章节骨架", "scope": "member",
        "body": "For a formal research report or paper, follow this skeleton:\n"
                "1. Abstract: one sentence each for the problem, the method, the main result, and the conclusion;\n"
                "2. Introduction: background, what earlier work lacks, the question this piece answers;\n"
                "3. Methods: design, subjects or data source, metrics and statistical definitions, steps someone could reproduce;\n"
                "4. Results: findings only, with numbered figures and tables;\n"
                "5. Discussion: relation to earlier work, limitations, how far the conclusions generalize;\n"
                "6. Conclusion and recommendations. Keep one citation format throughout.",
        "body_zh": "写正式研究报告或论文时按这个骨架:\n"
                   "1. 摘要:问题、方法、主要结果、结论各一句;\n"
                   "2. 引言:背景、已有工作的不足、本文要回答的问题;\n"
                   "3. 方法:设计、对象或数据来源、指标与统计口径、可复现的步骤;\n"
                   "4. 结果:只写发现,图表编号并说明;\n"
                   "5. 讨论:与已有研究的关系、局限、结论能外推到哪里;\n"
                   "6. 结论与建议。全文引用格式统一。",
    },
    "risk-check": {
        "name": "Risk self-check", "name_zh": "风险自查清单",
        "description": "Compliance and risk check before publishing or partnering",
        "description_zh": "对外发布或对外合作前的合规与风险自查", "scope": "member",
        "body": "Before publishing or partnering, check each item and give \"present / absent / unconfirmed\" plus a suggested action:\n"
                "1. Data: any personal data, identifying information, or unpublished internal data?\n"
                "2. Rights: do you have the right to use the assets, data, and quotations? Are the licence terms met (attribution, licence copy, commercial use allowed)?\n"
                "3. Claims: any absolute promises, implied efficacy, or unverified comparisons?\n"
                "4. Compliance: does it touch the rules of the industry you are in (for example advertising and disclosure in healthcare or finance)?\n"
                "5. Security: do the files you are about to send out contain keys, internal addresses, or internal names?",
        "body_zh": "对外发布 / 合作前逐项自查,每项给「有 / 无 / 待确认」和处置建议:\n"
                   "1. 数据:有没有个人信息、可识别信息、未公开的内部数据?\n"
                   "2. 授权:素材、数据、引用是否有权使用?许可要求是否满足(署名、许可副本、是否允许商用)?\n"
                   "3. 表述:有没有绝对化承诺、功效暗示、未经核实的对比?\n"
                   "4. 合规:是否触及所在行业的监管要求(如医疗、金融的宣传与信息披露)?\n"
                   "5. 安全:要发出去的文件里有没有密钥、内网地址、内部人名?",
    },
    "remotion-video": {
        "name": "Video as code (Remotion)", "name_zh": "程序化视频(Remotion)",
        "description": "How to build and render a video from React code in the workspace",
        "description_zh": "用 React 代码在工作目录里做出并渲染一条视频的做法", "scope": "member",
        "body": "Making a video with Remotion means writing React and rendering it to a file — not editing a timeline. Work inside the group workspace.\n"
                "1. Check the ground before promising anything: Node.js 22 or newer. Run `node -v` first, and if it is missing or older say so, rather than starting a project that cannot render.\n"
                "2. Set the project up once and reuse it: a subdirectory of the workspace holding `remotion`, `@remotion/cli` and `@remotion/renderer`. Installing those, plus the headless browser downloaded on the first render, can take longer than one `run_code` call is allowed — if the timeout stops it, say so and let the user decide; do not quietly abandon the video.\n"
                "3. One composition per video, registered in the entry file with explicit width, height, fps and duration. Everything the user might want changed — title, figures, dates, wording, colours, the rows of a list — belongs in that composition's props and is passed in with `--props '{...}'`; never rewrite the component for each new video.\n"
                "4. Every animation must come from the current frame (`interpolate`, `spring`, `Sequence`). CSS transitions, `animation` keyframes and animation utility classes are not rendered correctly and are forbidden — this is the most common way a Remotion video comes out wrong.\n"
                "5. Render with a shell command: `npx remotion render <entry> <CompositionId> <file>.mp4 --props '{...}'`. Time and randomness arrive as props; `Date.now()` or `Math.random()` inside a composition makes the output different every run.\n"
                "6. Write the .mp4 into the workspace and name it, together with the command you ran, in your reply. You cannot watch the result: describe what you composed, never what it looks like.",
        "body_zh": "用 Remotion 做视频,是写 React 再渲染成文件,而不是在时间线上剪。工作在本群工作目录里进行。\n"
                   "1. 先探路再承诺:需要 Node.js 22 或更新。先跑 `node -v`,没有或太旧就直说,别开一个根本渲染不出来的项目。\n"
                   "2. 项目只装一次、反复用:工作目录下的一个子目录里放 `remotion`、`@remotion/cli`、`@remotion/renderer`。"
                   "装这些,加上首次渲染要下载的无头浏览器,可能超过一次 `run_code` 允许的时间——超时被终止就直说,让用户决定,"
                   "不要悄悄把这条视频放掉。\n"
                   "3. 一条视频一个 composition,在入口文件里登记,宽高、帧率、时长都写明。"
                   "用户可能想改的一切——标题、数字、日期、措辞、颜色、列表行——都放进它的 props,用 `--props '{...}'` 传进去;"
                   "不要为每条新视频重写组件。\n"
                   "4. 所有动画都必须由当前帧推出(`interpolate`、`spring`、`Sequence`)。"
                   "CSS 过渡、`animation` 关键帧和动画工具类渲染不正确,一律不要用——这是 Remotion 视频做坏最常见的原因。\n"
                   "5. 用 shell 命令渲染:`npx remotion render <入口> <CompositionId> <文件>.mp4 --props '{...}'`。"
                   "时间与随机数都要当参数传进来;在 composition 里用 `Date.now()` 或 `Math.random()`,每次都渲染出不一样的结果。\n"
                   "6. 把 .mp4 写进工作目录,并在回复里给出文件名和你跑过的命令。你看不到成片:只说你编了什么,不要描述画面。",
    },
    "hyperframes-video": {
        "name": "HTML to video (HyperFrames)", "name_zh": "HTML 变视频(HyperFrames)",
        "description": "How to build a scene as a web page and render it to MP4",
        "description_zh": "把网页做成视频场景并渲染成 MP4 的做法", "scope": "member",
        "body": "HyperFrames renders a web page into a video: the timeline is HTML, CSS and JavaScript. Work inside the group workspace.\n"
                "1. Check the ground first: Node.js 22 or newer, and `ffmpeg` for local encoding. Name whichever is missing instead of starting a project that cannot finish.\n"
                "2. One project per video: `npx hyperframes init <name>` in the workspace, then `npx hyperframes render` for the MP4. `npx hyperframes preview` opens a local preview for the user to look at — you cannot see it, so never describe what it shows.\n"
                "3. Author the scene as a self-contained page: an index file plus the CSS, JavaScript, fonts and assets it needs, all inside the project folder. Motion belongs in the page, and timing follows the framework's own data attributes rather than a timeline you edit by hand.\n"
                "4. Parameterize rather than fork: values declared as composition variables are overridden at render time, so one page yields many videos. Headline, figures, dates and colours are variables; the file is not rewritten per video.\n"
                "5. Keep every asset local. A page that renders standalone in a browser can also be rendered elsewhere; one that depends on something on the internet cannot.\n"
                "6. Rendering takes minutes, and `run_code` stops a call that times out. If the budget is too short, say so and ask for a longer one — do not shorten the video or drop frames to fit.\n"
                "7. Name the .mp4 path and the command you ran in your reply. You cannot watch the result.",
        "body_zh": "HyperFrames 把一个网页渲染成视频:时间线就是 HTML、CSS 和 JavaScript。工作在本群工作目录里进行。\n"
                   "1. 先探路:需要 Node.js 22 或更新,本机编码还需要 `ffmpeg`。缺哪个就说哪个,别开一个做不完的项目。\n"
                   "2. 一条视频一个项目:在工作目录里 `npx hyperframes init <名字>`,再用 `npx hyperframes render` 出 MP4。"
                   "`npx hyperframes preview` 是开给用户看的本地预览——你看不到,所以不要描述里面是什么样。\n"
                   "3. 场景写成一个自包含的页面:一个入口文件,加上它需要的 CSS、JavaScript、字体和素材,全部放在项目目录里。"
                   "动效写在页面里,时序按框架自己的 data 属性走,而不是你手工去剪时间线。\n"
                   "4. 参数化,不要复制:声明成 composition 变量的值可以在渲染时覆盖,所以一个页面能出很多条视频。"
                   "标题、数字、日期、颜色都是变量,而不是每条视频改一遍文件。\n"
                   "5. 素材全部本地化。能在浏览器里独立渲染的页面,才能拿到别处渲染;依赖外网上东西的不能。\n"
                   "6. 渲染要几分钟,而 `run_code` 超时会终止调用。时间不够就直说、要求放宽——不要为了塞进去而把视频剪短或丢帧。\n"
                   "7. 在回复里给出 .mp4 路径和你跑过的命令。你看不到成片。",
    },
    "capcut-draft": {
        "name": "Jianying (CapCut) drafts", "name_zh": "剪映草稿(Jianying / CapCut)",
        "description": "Build a video by generating a Jianying draft the user opens themselves",
        "description_zh": "生成一份剪映草稿,由用户自己在剪映里打开的做法", "scope": "member",
        "body": "There is no packaged MCP server for Jianying, so this is the code route: write a Python "
                "program that builds a Jianying draft, and the user opens Jianying to see it. Work inside the "
                "group workspace.\n"
                "1. Ask where the draft folder is before writing anything. In Jianying it is the draft box → right-click a draft → open its location; on macOS the path is usually ~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft. Do not guess: a draft written anywhere else is one the user cannot open, and they will see nothing and conclude the work failed.\n"
                "2. Do not install anything permanently — run the program with `uv run --with pyJianYingDraft python build.py`. That library writes Jianying's own draft files, so what the user gets is an ordinary project they can go on editing by hand.\n"
                "3. Build it in the documented order: `DraftFolder(<draft folder>).create_draft(width, height, fps)` returns the script object; append tracks; add the material before the segment that uses it; `save()` at the end. Match the size to the footage (portrait 1080x1920, landscape 1920x1080) — nothing is resized for you.\n"
                "4. Only reference files that really exist, and give Jianying absolute paths: it reads the media itself, so a file in this workspace has to stay where it is. Say which paths you used.\n"
                "5. Report the draft name and what is in it, and ask the user to open Jianying to check. You cannot watch the result: never describe how it looks.\n"
                "6. If the user asks for a Jianying MCP connection rather than a generated draft, say plainly that the ones that exist are not packaged — they would have to be cloned from a repository by hand — and that this route needs nothing installed beyond uv.",
        "body_zh": "剪映没有现成的、可以直接装上的 MCP 服务器,所以走代码这条路:写一个 Python 程序生成剪映草稿,"
                   "由用户在剪映里打开看。工作在本群工作目录里进行。\n"
                   "1. 动手之前先问草稿目录在哪。剪映里是「草稿箱 → 右键某个草稿 → 打开草稿位置」;"
                   "macOS 上路径通常是 ~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft。不要猜:"
                   "写到别处的草稿用户根本打不开,他会什么都看不到,然后以为活没干成。\n"
                   "2. 不要永久安装任何东西——用 `uv run --with pyJianYingDraft python build.py` 跑那段程序。"
                   "这个库写的是剪映自己的草稿文件,所以用户拿到的就是一个普通工程,之后还能手工再改。\n"
                   "3. 按文档的顺序搭:`DraftFolder(<草稿目录>).create_draft(宽, 高, 帧率)` 返回脚本对象,"
                   "再追加轨道;先加素材、再加用它做的那一段;最后 `save()`。画幅要跟素材一致"
                   "(竖屏 1080x1920,横屏 1920x1080)——没有人会替你缩放。\n"
                   "4. 只引用真实存在的文件,而且要给剪映绝对路径:素材是它自己去读的,所以工作目录里的文件必须留在原地。"
                   "在回复里说明你用了哪些路径。\n"
                   "5. 回复里给出草稿名和里面有什么,并请用户打开剪映核对。你看不到成片:不要描述画面。\n"
                   "6. 如果用户问的是「能不能连剪映的 MCP」而不是生成草稿,就直说:现有的那几个都没有打包发布,"
                   "得自己把仓库 clone 下来;而这条代码路线除了 uv 不需要装别的东西。",
    },
}


# Both spellings of a built-in skill name (and its stable key) resolve to the same
# entry, so a skill seeded before the content was translated still matches — for the
# "already installed" badge, for the skills a group template asks for, and for the
# body injected into the prompt.
SKILL_ALIASES: dict[str, dict] = {}
for _key, _entry in EXAMPLE_SKILLS.items():
    for _name in (_key, _entry.get("name"), _entry.get("name_zh")):
        if _name:
            SKILL_ALIASES.setdefault(_name, _entry)


def skill_for(name: str | None) -> dict | None:
    """The built-in skill a name refers to, in either language (or its key)."""
    return SKILL_ALIASES.get(name or "")


def skill_names(name: str | None) -> list[str]:
    """Every spelling a stored skill name might have been written in."""
    entry = skill_for(name)
    if not entry:
        return [name] if name else []
    out = [entry["name"]]
    if entry.get("name_zh"):
        out.append(entry["name_zh"])
    return out


def display_skill_name(name: str | None, lang: str) -> str:
    """A built-in skill name as it should be shown in `lang` (either spelling resolves)."""
    entry = skill_for(name)
    if not entry:
        return name or ""
    return (entry.get("name_zh") if lang == "zh" else entry["name"]) or (name or "")


def canonical_skill_name(name: str | None) -> str:
    """The language-neutral name a built-in skill is stored under."""
    entry = skill_for(name)
    return entry["name"] if entry else (name or "")


def localize_skill(skill: "Skill", lang: str) -> "Skill":
    """Show `skill` in `lang`, but only where the user has not edited it.

    A field is swapped only while it still matches the built-in text in either
    language, so anything the user rewrote is handed back exactly as they wrote it.
    """
    entry = skill_for(skill.name)
    if not entry:
        return skill
    out = copy.copy(skill)
    for attr, zh_attr in (("name", "name_zh"), ("description", "description_zh"),
                          ("body", "body_zh")):
        zh = entry.get(zh_attr)
        base = (entry.get(attr) or "").strip()
        if not zh:
            continue
        current = (getattr(skill, attr) or "").strip()
        if current not in {base, zh.strip()}:
            continue                       # user edited this field — keep their text
        setattr(out, attr, zh.strip() if lang == "zh" else base)
    return out


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: str
    scope: str = "member"   # member = ticked per member | group = group rules, attached to the group
    version: str = ""

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "path": self.path,
                "scope": self.scope, "version": self.version}


def safe_skill_name(name: str) -> str:
    n = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", name).strip().strip(".")
    return n[:60]


def frontmatter_value(lines: list[str], i: int) -> tuple[str, int]:
    """Read one frontmatter value starting at line `i`, returning (value, next index).

    Handles the YAML block scalars (`>` folded, `|` literal) as well as a plain or quoted
    one-liner. That matters because the two notations are equally common in the wild — a
    Claude-style skill whose description is written as `description: >` would otherwise be
    read as the literal string ">", and importing it would look like it worked.

    Public because a second reader (the expert packages imported from WorkBuddy) has the
    same problem with the same notation, and a rule that exists in two places drifts.
    """
    key, sep, rest = lines[i].partition(":")
    if not sep:
        return "", i + 1
    rest = rest.strip()
    if rest not in (">", "|", ">-", "|-", ">+", "|+"):
        return rest.strip("\"'"), i + 1
    folded = rest.startswith(">")
    chunk: list[str] = []
    i += 1
    while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
        chunk.append(lines[i].strip())
        i += 1
    value = (" " if folded else "\n").join(x for x in chunk if x).strip()
    return value, i


def parse_skill_text(text: str, default_name: str, path: str = "") -> Skill:
    name, desc, body, scope, version = default_name, "", text, "member", ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        lines = m.group(1).splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
                i += 1
                continue                       # blank, comment, or a nested key we do not use
            key = line.partition(":")[0].strip()
            value, i = frontmatter_value(lines, i)
            if key == "name" and value:
                name = value
            elif key == "description":
                desc = value
            elif key == "scope" and value in ("member", "group"):
                scope = value
            elif key == "version":
                version = value
        body = m.group(2)
    return Skill(name=name, description=desc, body=body.strip(), path=path, scope=scope, version=version)


def _parse_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_skill_text(text, path.parent.name, str(path))


def render_skill(name: str, description: str, body: str, scope: str = "member", version: str = "") -> str:
    head = f"---\nname: {name}\ndescription: {description.strip()}\nscope: {scope}\n"
    if version:
        head += f"version: {version}\n"
    return head + f"---\n{body.strip()}\n"


def write_text_atomic(path: Path, text: str) -> None:
    """Write beside the target and rename.

    Replacing a skill or a plugin is exactly when this matters: an interrupted write would leave a
    truncated file, with the old version already gone and nothing to re-download it from.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_skill(skills_dir: Path, name: str, description: str, body: str, scope: str = "member",
                version: str = "", old_name: str | None = None) -> Skill:
    folder = safe_skill_name(name)
    if not folder:
        raise ValueError(i18n.pick_now("That skill name is not valid", "技能名称不合法"))
    d = skills_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    # The name is already stripped of separators, but the folder is still checked: this is the one
    # place that turns a remote string into a directory, so it should not depend on that alone.
    if not d.resolve().is_relative_to(skills_dir.resolve()):
        raise ValueError(i18n.pick_now("That skill name is not valid", "技能名称不合法"))
    write_text_atomic(d / "SKILL.md",
                      render_skill(folder, description, body, scope if scope in ("member", "group") else "member", version))
    if old_name and safe_skill_name(old_name) != folder:
        delete_skill(skills_dir, old_name)
    return _parse_skill(d / "SKILL.md")  # type: ignore[return-value]


def delete_skill(skills_dir: Path, name: str) -> bool:
    d = skills_dir / safe_skill_name(name)
    if d.is_dir() and d.parent == skills_dir:
        shutil.rmtree(d)
        return True
    return False


# What an imported skill may drag along: the files its own text tells the reader to open or run
# (`references/*.md`, `scripts/*.mjs`). Bounded at every level, because this copies out of a
# directory another application wrote and neither side is trusted to be reasonable.
SKILL_EXTRA_MAX_FILES = 400
SKILL_EXTRA_MAX_FILE = 8 * 1024 * 1024        # one file
SKILL_EXTRA_MAX_BYTES = 40 * 1024 * 1024      # the whole skill


def copy_skill_files(src_dir: Path, dest_dir: Path, *, skip: tuple[str, ...] = ("SKILL.md",),
                     only_missing: bool = False) -> int:
    """Copy a skill's supporting files next to its SKILL.md; returns how many were copied.

    **A skill is not always one file.** The ones worth importing are exactly the ones that are not:
    HyperFrames' creation workflows and WorkBuddy's media skills ship a `references/` tree their own
    text tells the reader to open, and sometimes a `scripts/` tree it tells the reader to run.
    Importing only the SKILL.md left every one of those pointers dangling — the skill appeared in the
    list, complete and installed, while each instruction in it led to a file that did not exist. That
    is worse than not importing it, because nothing anywhere says so.

    Links are skipped rather than followed and every directory is re-checked for containment: the
    source belongs to another application and the destination is this app's own data directory.
    """
    if not src_dir.is_dir():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    copied = files = total = 0
    for path in sorted(src_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(src_dir)
        # Editor and VCS leftovers, and the file that was just written from the text.
        if rel.as_posix() in skip or any(part.startswith(".") for part in rel.parts):
            continue
        if files >= SKILL_EXTRA_MAX_FILES or total >= SKILL_EXTRA_MAX_BYTES:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > SKILL_EXTRA_MAX_FILE:
            continue
        target = dest_dir / rel
        if only_missing and target.is_file():
            continue                      # a repair fills gaps; it does not take back what is there
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Created here, a moment ago, and checked anyway: the workspace the caller owns may
            # already hold a link at one of these names.
            if not target.parent.resolve().is_relative_to(root):
                continue
            data = path.read_bytes()
        except OSError:
            continue
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        copied += 1
        files += 1
        total += len(data)
    return copied


def skill_extra_files(skill: "Skill") -> int:
    """How many files a skill carries beyond its SKILL.md — 0 for a plain text skill.

    Read by the interface and by the prompt, so both can say out loud that this skill has files of
    its own: it is the difference between "a paragraph of rules" and "a manual you have to go and
    read", and the reader deserves to know which one they are turning on.

    Dotted names are not counted, for the same reason `copy_skill_files` does not copy them: a
    leftover `.tmp` from an interrupted write is not a file this skill came with, and counting it
    would make a half-copied skill look whole.
    """
    if not skill.path:
        return 0
    folder = Path(skill.path).parent
    if not folder.is_dir():
        return 0
    n = 0
    for path in folder.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name == "SKILL.md":
            continue
        if path.name.startswith(".") or path.name.endswith(".tmp"):
            continue
        n += 1
        if n >= SKILL_EXTRA_MAX_FILES:
            break
    return n


def ensure_example_skills(skills_dir: Path, flag: Callable[[str], bool] | None = None) -> None:
    """Write the example skills on first run. flag(key) reports whether it has been written
    before and marks it at the same time, so a deleted example is not written back on the
    next start, while newly added examples still reach existing users."""
    was_empty = not any(skills_dir.iterdir())
    for key, ex in EXAMPLE_SKILLS.items():
        if flag is not None:
            if flag(f"seed_skill:{key}"):
                continue
        elif not was_empty:
            return
        # The canonical English name is what lands on disk; a Chinese UI shows the
        # Chinese wording through localize_skill() without touching the file.
        name = ex["name"]
        # …but a skill seeded under its *other* spelling is the same skill, not a missing one.
        # Writing the canonical folder as well is exactly how one skill became two: the seed marker
        # used to be keyed by the name, the name changed from Chinese to English when the built-in
        # text became bilingual, so every marker missed its skill and the whole set was written a
        # second time. `localize_skill` then showed both copies with the same title, and a member
        # could be given both. `merge_builtin_skill_copies` below clears the ones already on disk;
        # this check is what stops it happening again.
        if any((skills_dir / n).is_dir() for n in skill_names(key)):
            continue
        d = skills_dir / name
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(render_skill(name, ex["description"], ex["body"], ex["scope"]), encoding="utf-8")


def _is_seeded_copy(folder: Path, entry: dict) -> bool:
    """Is this folder the built-in skill as this app wrote it — in either language, at any age?

    Compared by parsed fields, not byte-for-byte. The frontmatter has gained keys since the first
    installs (`scope` was added later), and a copy that is merely missing a line the current writer
    emits is still untouched text; comparing the rendered file called those two user edits and left
    them in place, which is one duplicate too many. Name, description and body are compared exactly,
    and those are what editing a skill actually changes — so a skill the user rewrote never matches.
    """
    got = _parse_skill(folder / "SKILL.md")
    if not got:
        return False
    for name_key, desc_key, body_key in (("name", "description", "body"),
                                         ("name_zh", "description_zh", "body_zh")):
        name = entry.get(name_key)
        if not name:
            continue
        if (got.name.strip() == name.strip()
                and got.description.strip() == (entry.get(desc_key) or "").strip()
                and got.body.strip() == (entry.get(body_key) or "").strip()):
            return True
    return False


def _rename_skill_refs(store: Any, old: str, new: str) -> None:
    """Point every selection that names `old` at `new`.

    Skipping this is the quiet half of the bug: the lookups resolve by either spelling, so the
    prompt would still be right, but the tick in the interface matches on the stored string — so
    the member's skill would show as unticked, and saving that member for any other reason would
    drop it.
    """
    for a in store.list_agents():
        if old in (a.get("skills") or []):
            store.update_agent(a["id"], {"skills": [new if x == old else x for x in a["skills"]]})
    for g in store.list_groups():
        chosen = (g.get("ext") or {}).get("skills") or []
        if old in chosen:
            store.update_group(g["id"], {"ext": {"skills": [new if x == old else x for x in chosen]}})
    if store.get_source("skill", old):
        store.delete_source("skill", old)


def merge_builtin_skill_copies(store: Any) -> list[tuple[str, str]]:
    """One built-in skill is one folder, however many names it has picked up over time.

    Every built-in skill that predates the bilingual rewrite is on disk twice: the seed marker was
    keyed by the skill's name, the name moved from Chinese to English, so the marker for
    `brainstorming` and the marker for `头脑风暴规则` were different keys — and the second seeding
    wrote the whole set again under the canonical names. Nothing looked broken, because a skill is
    found by either spelling; the visible result is the settings page listing `头脑风暴规则` twice
    with the same description, and a member able to be given the same rule twice.

    Kept: the canonical English folder, or the surviving one when only the legacy spelling is
    there. Removed: only a folder whose SKILL.md is still exactly what was written. Returns the
    `(kept, removed)` pairs.
    """
    skills_dir = store.data_dir / "skills"
    if not skills_dir.is_dir():
        return []
    pairs: list[tuple[str, str]] = []
    for _key, entry in EXAMPLE_SKILLS.items():
        canonical = safe_skill_name(entry["name"])
        spellings = [n for n in (canonical, safe_skill_name(entry.get("name_zh") or "")) if n]
        present = [skills_dir / n for n in dict.fromkeys(spellings) if (skills_dir / n).is_dir()]
        if len(present) < 2:
            continue
        keep = skills_dir / canonical if (skills_dir / canonical).is_dir() else present[0]
        for folder in present:
            if folder == keep or not _is_seeded_copy(folder, entry):
                continue
            shutil.rmtree(folder)
            pairs.append((keep.name, folder.name))
    for kept, gone in pairs:
        _rename_skill_refs(store, gone, kept)
        # The marker that made this happen, in the notation that caused it. Stable keys are what is
        # used now; a name-keyed one can only ever refer to a skill that has been removed.
        store._x("DELETE FROM meta WHERE key=?", (f"seed_skill:{gone}",))
    return pairs


# ----------------------------------------------------------------- categories
# A fixed, ordered set of purposes, and the order the settings page shows them in. The list used to
# be flat and alphabetical by *folder* name — which on a Chinese install puts all thirteen Chinese
# names after all the English ones, so two copies of one skill sat far apart, nothing of the same
# kind was ever adjacent, and the only way to find anything was to read all of it.
SKILL_CATEGORY_ORDER: tuple[str, ...] = (
    "writing", "video", "research", "analysis", "facilitation", "code", "translation", "meta",
    "imported", "other",
)

# The built-in skills, filed by hand. Their descriptions are written to be read by a model, so
# guessing a section from one files about half of them somewhere surprising.
BUILTIN_SKILL_CATEGORY: dict[str, str] = {
    "office-writing": "writing",
    "report-structure": "writing",
    "relay-writing": "writing",
    "research-findings": "research",
    "data-analysis": "analysis",
    "code-review": "code",
    "short-video-storyboard": "video",
    "remotion-video": "video",
    "hyperframes-video": "video",
    "capcut-draft": "video",
    "brainstorming": "facilitation",
    "review-meeting": "facilitation",
    "debate": "facilitation",
    "risk-check": "facilitation",
    "translation": "translation",
}

# For everything else — an imported skill or one the user wrote. First hit wins, so the narrower
# words come first: "storyboard" is about video even though it is a kind of writing.
_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("video", ("video", "视频", "分镜", "storyboard", "remotion", "hyperframes", "capcut", "剪映",
               "animation", "动画", "ffmpeg")),
    ("translation", ("translat", "翻译", "互译", "localis", "localiz")),
    ("meta", ("skill-creator", "skill-development", "plugin-", "session-report", "playground",
              "hookify", "example-skill", "example-command", "agent-development", "onboard",
              "buddy")),
    ("research", ("research", "研究", "调研", "文献", "论文", "paper", "学术", "academic",
                  "citation", "olympiad")),
    ("analysis", ("data", "数据", "分析", "excel", "sheet", "表格", "csv", "chart", "图表", "统计")),
    ("code", ("code", "代码", "mcp", "debug", "refactor", "test", "frontend", "前端", "hook",
              "command", "api", "deploy", "build-", "plugin", "skill", "agent")),
    ("facilitation", ("meeting", "会议", "评审", "review", "brainstorm", "头脑风暴", "debate",
                      "辩论", "relay", "接力", "risk", "风险", "checklist", "清单")),
    ("writing", ("writ", "写作", "文案", "公文", "document", "文档", "report", "报告", "docx",
                 "pptx", "排版", "摘要", "word", "slide")),
)


def builtin_key(name: str | None) -> str:
    """The stable key of the built-in skill stored under `name`, or ""."""
    for key, entry in EXAMPLE_SKILLS.items():
        if name in (key, entry.get("name"), entry.get("name_zh")):
            return key
    return ""


def category_of(name: str | None, description: str = "", *, imported: bool = False) -> str:
    """Which section a skill belongs in: its built-in filing, else a guess from its words."""
    key = builtin_key(name)
    if key and key in BUILTIN_SKILL_CATEGORY:
        return BUILTIN_SKILL_CATEGORY[key]
    if imported:
        return "imported"
    hay = f"{name or ''} {description}".lower()
    for cat, words in _CATEGORY_HINTS:
        if any(w in hay for w in words):
            return cat
    return "other"


def category_rank(cat: str) -> int:
    """Position of a section, so every reader sorts it the same way."""
    return SKILL_CATEGORY_ORDER.index(cat) if cat in SKILL_CATEGORY_ORDER else len(SKILL_CATEGORY_ORDER)


def list_skills(skills_dir: Path) -> list[Skill]:
    out = []
    for p in sorted(skills_dir.glob("*/SKILL.md")):
        s = _parse_skill(p)
        if s:
            out.append(s)
    return out


def skills_prompt(skills_dir: Path, names: list[str], max_chars: int = 4000, group: bool = False) -> str:
    """Assemble the ticked skills into one prompt block. When group=True the heading is
    written as "Group rules".

    Names are looked up by every spelling a built-in skill may have been stored
    under, and the text handed to the model is the one matching the request
    language — a group created on a Chinese install and one created on an English
    install produce the same prompt in the same language.
    """
    by_name: dict[str, Skill] = {}
    for s in list_skills(skills_dir):
        for spelling in skill_names(s.name):
            by_name.setdefault(spelling, s)
    lang = i18n.current()
    parts, used = [], 0
    for n in names:
        s = by_name.get(n)
        if not s:
            continue
        s = localize_skill(s, lang)
        is_group = group or s.scope == "group"
        head = i18n.pick(lang, "Group rule" if is_group else "Skill",
                         "群聊规则" if is_group else "技能")
        where = ""
        if skill_extra_files(s):
            # A skill with files of its own is a manual, not a paragraph: its text says "read
            # references/x.md" and "run scripts/y.mjs", and those only resolve if the reader is told
            # where they live. Nobody was, so every such line led nowhere. The path is the app's own
            # data directory, which a member's own tools can read.
            folder = Path(s.path).parent
            where = i18n.pick(
                lang,
                f"\nThis skill has files of its own. `<SKILL_DIR>` means {folder} — read what it "
                f"points you at from there, and run its scripts from that directory. Its "
                f"instructions do not work without them.",
                f"\n这个技能有自己的文件。`<SKILL_DIR>` 指的是 {folder} —— 它让你读的东西都在那里,"
                f"要跑的脚本也从那个目录里跑。没有这些文件,它的说明是走不通的。")
        chunk = (f"【{head}:{s.name}】{where}\n{s.body}" if lang == "zh"  # i18n-keep: already bilingual: 【Head: name】 vs [Head: name]
                 else f"[{head}: {s.name}]{where}\n{s.body}")
        if used + len(chunk) > max_chars:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts)


# ------------------------------------------------------------------ plugins
ToolFn = Callable[[dict[str, Any]], Awaitable[Any] | Any]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    fn: ToolFn
    source: str = "builtin"      # builtin | plugin | mcp
    plugin: str = ""             # plugin id (file name without .py)

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters,
                "source": self.source, "plugin": self.plugin}


@dataclass
class PluginInfo:
    id: str
    file: str
    name: str = ""
    description: str = ""
    version: str = ""
    tools: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "file": self.file, "name": self.name or self.id, "description": self.description,
                "version": self.version, "tools": self.tools, "error": self.error}


class _PluginScope:
    """Registry handed to a plugin: tools registered through it are recorded under that plugin."""

    def __init__(self, reg: "ToolRegistry", plugin: str):
        self._reg, self._plugin = reg, plugin

    def register(self, name: str, description: str, parameters: dict | None, fn: ToolFn) -> None:
        old = self._reg._tools.get(name)
        if old is not None:
            # previously a later registration silently displaced an earlier one: the displaced
# plugin still showed the tool on the permissions page, but it could not be called
# and nothing warned about it
            raise ValueError(i18n.pick_now(f"Tool name \"{name}\" is already taken by {('plugin ' + old.plugin) if old.plugin else 'a built-in tool'} — pick another name", f"工具名「{name}」已被{('插件 ' + old.plugin) if old.plugin else '内置工具'}占用,请换个名字"))
        self._reg._add(Tool(name, description, parameters or {"type": "object", "properties": {}}, fn, "plugin", self._plugin))


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.plugins: dict[str, PluginInfo] = {}

    @property
    def errors(self) -> list[str]:
        return [f"{p.file}: {p.error}" for p in self.plugins.values() if p.error]

    def _add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        if tool.plugin and tool.plugin in self.plugins:
            self.plugins[tool.plugin].tools.append(tool.name)

    def register(self, name: str, description: str, parameters: dict | None, fn: ToolFn,
                 source: str = "builtin") -> None:
        self._add(Tool(name, description, parameters or {"type": "object", "properties": {}}, fn, source))

    def list(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def plugin_tools(self, plugin_ids: list[str]) -> list[Tool]:
        return [t for t in self._tools.values() if t.plugin in plugin_ids]

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        tool = self._tools[name]
        if inspect.iscoroutinefunction(tool.fn):
            return await tool.fn(args)
        res = await asyncio.to_thread(tool.fn, args)   # run plain functions in a thread so a slow plugin cannot block the whole backend
        if hasattr(res, "__await__"):
            res = await res  # type: ignore[misc]
        return res

    def load_plugins(self, plugins_dir: Path) -> None:
        """Example plugin file:
            PLUGIN = {"name": "Greeting", "description": "Say hello", "version": "1.0"}   # optional
            def register(registry): registry.register("hello", "Say hi", None, lambda a: "hi")"""
        for name in [n for n, t in self._tools.items() if t.source == "plugin"]:
            del self._tools[name]
        self.plugins = {}
        for f in sorted(plugins_dir.glob("*.py")):
            info = PluginInfo(id=f.stem, file=f.name)
            self.plugins[f.stem] = info
            try:
                spec = importlib.util.spec_from_file_location(f"team_agent_plugin_{f.stem}", f)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                meta = getattr(mod, "PLUGIN", None)
                if isinstance(meta, dict):
                    info.name = str(meta.get("name", ""))
                    info.description = str(meta.get("description", ""))
                    info.version = str(meta.get("version", ""))
                mod.register(_PluginScope(self, f.stem))
            except Exception as e:  # noqa: BLE001
                info.error = f"{type(e).__name__}: {e}"[:300]


def build_registry(plugins_dir: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.load_plugins(plugins_dir)
    return reg
