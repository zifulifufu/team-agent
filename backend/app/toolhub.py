"""Unified tool dispatch: built-in tools + plugin tools + MCP tools, giving each member a list
of what is usable based on "what this group has enabled".

Built-in tools: current_time / library_search / library_read / memory_search / memory_save /
run_code (only when "let members run code" is on) / generate_video (only when video generation is
on and a video provider is reachable). Whether they are available depends on the group settings
(library switch, memory switch), while plugins and MCP tools only become usable once ticked for
the group — ticking is your authorization for "let members of this group call it on their own".
Every call is recorded in the message's tool trace and is visible below the bubble.
"""

from __future__ import annotations

from . import (animate, assemble, attachments, coderun, comfyui, embed, ffmpeg, figure, i18n,
                 imagegen, layouts, media, net, study, video, vision)

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .approvals import policy_for
from .hooks import scrub_args, scrub_text
from .library import Library
from .mcp_client import McpManager, pick_transport, slug
from .memory import MemoryService, looks_sensitive
from .store import Store
from .tools import ToolRegistry
from . import advisor, docwrite, proclog


@dataclass
class ToolOutcome:
    text: str
    ok: bool = True
    ms: int = 0
    denied: bool = False       # blocked by permissions (denied by the user, timed out unconfirmed, or forbidden):
# never really executed
    # Files the call produced, so the UI can offer them instead of only printing a path.
    # [{"kind": "video", "name": "...", "bytes": 123}] — where they live is the group's workspace,
    # which the UI already knows how to reach.
    files: list[dict] = field(default_factory=list)


@dataclass
class ToolContext:
    group: dict
    agent: dict
    tools: dict[str, dict] = field(default_factory=dict)   # name -> spec (including source / server_id)
    problems: list[str] = field(default_factory=list)      # connection failures and the like, shown to the user
    # At least one MCP server has merely "not been connected for the first time yet", which is
# not a real error. The UI shows one extra note based on this;
    # a flag is used rather than matching on wording, because the text in problems follows
# the request language.
    mcp_deferred: bool = False
    # Set only when the member *is* a generator (see `media.member_target`): `{"provider_id",
    # "model", "use"}`. It exists because the generators otherwise read their provider and model
    # from the group's settings, and a member called "Seedance" has to run Seedance whatever was
    # chosen last — one field rather than a second copy of the tool.
    media: dict = field(default_factory=dict)

    def specs(self) -> list[dict]:
        return list(self.tools.values())


BUILTIN_SPECS: dict[str, dict] = {
    # English is the canonical text and `<field>_zh` the Chinese wording; the specs are run
    # through `i18n.localize()` where they are turned into a tool list, so the description the
    # model and the UI see follows the request language. (Calling pick_now in here would be
    # evaluated once at import and freeze whichever language was current then.)
    "current_time": {
        "description": "Get the current local date and time",
        "description_zh": "获取当前本地日期和时间",
        "risk": "read",
        "parameters": {"type": "object", "properties": {}},
    },
    "library_search": {
        "description": "Search the library for the most relevant documents (returns document "
                       "titles and the passage that matched, one passage per document). Reach for "
                       "this first whenever you need to cite a fact, a figure or a rule. The "
                       "knowledge base may be in another language than this conversation, and that "
                       "is where a search quietly fails: a query written entirely in your own "
                       "language often does not reach a passage written in another one, however "
                       "long or well phrased it is. Improve it by putting the subject's term in the "
                       "library's language *into the same query* — \\\"脑血管痉挛 angioplasty\\\" "
                       "reaches the vasospasm page where \\\"蛛网膜下腔出血 脑血管痉挛\\\" does not — "
                       "then search again before concluding nothing is there.",
        "description_zh": "在资料库里检索最相关的文档(返回文档标题和命中的那一段,每篇只出一条)。"
                          "需要引用事实、图谱或规定时先用它。注意资料库的语言可能和这段对话不同,"
                          "而这正是检索会静默失败的地方:整句都用你说的语言写,常常够不到另一种语言的"
                          "段落 —— 写多长、写多完整都没用。有效的改法是**把主题在资料库语言里的术语"
                          "放进同一条查询里**(如「脑血管痉挛 angioplasty」能命中血管痉挛那一页,"
                          "而「蛛网膜下腔出血 脑血管痉挛」不能),然后再搜一次,别直接下结论说没有。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords or a question to search for",
                      "description_zh": "检索关键词或问题"},
            "top_k": {"type": "integer", "description": "How many passages to return; 5 by default",
                      "description_zh": "返回片段数,默认 5"}}, "required": ["query"]},
    },
    "library_read": {
        "description": "Read a document from the library by title, one chunk at a time.",
        "description_zh": "按标题读取资料库中某份文档的原文(分段读取)。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {
            "doc": {"type": "string", "description": "Document title or ID",
                    "description_zh": "文档标题或 ID"},
            "start": {"type": "integer", "description": "Character offset to start from; 0 by default",
                      "description_zh": "从第几个字符开始,默认 0"}}, "required": ["doc"]},
    },
    "memory_search": {
        "description": "Search long-term memory (user preferences, earlier decisions, past practice).",
        "description_zh": "检索长期记忆(用户偏好、以往决定、过往做法)。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    "memory_save": {
        "description": "Record one durably useful piece of information in this group's memory (a "
                       "preference, a decision, a lesson). Never record keys, passwords or personal "
                       "data.",
        "description_zh": "把一条长期有用的信息记入本群记忆(偏好、决定、教训)。不要记密钥、密码和个人隐私。",
        # The risk lives with the spec so that every consumer — dispatch and the Permissions
        # page alike — sees the same value. Building a spec by hand and letting risk_of infer
        # from the name is how a new built-in ends up advertised as read-only.
        "risk": "write",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "One sentence, at most 120 characters",
                        "description_zh": "一句话,不超过 120 字"},
            "kind": {"type": "string", "enum": ["preference", "fact", "decision", "lesson"]}},
            "required": ["content"]},
    },
    "run_code": {
        "description": "Write a program and run it, then read its output. Use it to calculate "
                       "something, transform a file, or try a snippet out. The working directory "
                       "is a private workspace: files you write there stay there. There is no "
                       "interaction and no long-running process — the run is killed when it "
                       "times out. Python is the usual choice; shell is for short commands.",
        "description_zh": "写一段程序并运行,再读它的输出。适合做计算、转换文件、试一小段代码。工作目录是"
                          "专属的:你在里面写的文件就留在里面。不支持交互和长时间运行的进程,超时会被终止。"
                          "一般用 python,shell 适合短命令。",
        # Every built-in carries its own `risk` (see BUILTIN_SPECS); this is the one place
        # that copies it into the per-call spec, so dispatch and the Permissions page agree.
        "risk": "exec",
        "parameters": {"type": "object", "properties": {
            "language": {"type": "string", "enum": ["python", "shell"],
                         "description": "python or shell", "description_zh": "python 或 shell"},
            "code": {"type": "string", "description": "The program text",
                     "description_zh": "程序正文"},
            "cwd": {"type": "string",
                    "description": "Optional subdirectory of the workspace to run in",
                    "description_zh": "可选:工作目录下的子目录,在这里运行"}},
            "required": ["language", "code"]},
    },
    "generate_video": {
        "description": "Generate a short video through this group's video provider, and save it "
                       "into the group's workspace. Which provider is configured matters, because "
                       "their rules differ. A self-hosted MiniMax H3 makes 4-15 second clips with "
                       "sound and is prompted as shots and then sound (\"[Shot 1] ... "
                       "overall_soundscape: ... non_diegetic_music: ...\"); a workspace image can be "
                       "first_frame/last_frame. MetaChat's video API (Grok Video, Midjourney Video) "
                       "makes 1-15 second clips and generates ONLY from a reference image, which must "
                       "be an http(s) URL because that service downloads it itself. Volcengine's Ark "
                       "(Doubao Seedance 2.5) makes 4-30 second clips, has sound on by default, takes "
                       "a workspace file or a URL for every kind of reference (pictures, video, audio) "
                       "and is the only one that accepts reference_videos/reference_audios; its "
                       "prompt refers to the references by position (\"@图片1\", \"@视频1\"). A local "
                       "ComfyUI renders from the prompt alone — no reference material at all, no "
                       "sound, and slower than the hosted ones — but it costs nothing per clip. "
                       "Rendering takes minutes, on a local one often more than ten. You cannot "
                       "watch or hear the result: say what you asked for, never describe what came "
                       "out.",
        "description_zh": "用本群配置的视频服务商生成一段短视频,存进本群工作目录。服务商不同,规矩也不同:"
                          "自建的 MiniMax H3 生成 4-15 秒、带声音的片段,提示词要写成「先分镜、再说声音」"
                          "(如「[Shot 1] … overall_soundscape: … non_diegetic_music: …」),可以用工作目录里的图片"
                          "当 first_frame/last_frame;MetaChat 的视频接口(Grok Video、Midjourney Video)"
                          "生成 1-15 秒的片段,而且只能「按参考图生成」,参考图必须是 http(s) 地址——它自己去下载;"
                          "火山方舟(Doubao Seedance 2.5)生成 4-30 秒的片段,默认带声音,参考素材(图、视频、音频)"
                          "既可以是工作目录里的文件也可以是公网地址,而且是唯一支持 reference_videos/"
                          "reference_audios 的;它的提示词用序号指代素材(「@图片1」「@视频1」)。"
                          "本地的 ComfyUI 只按提示词生成 —— 不收任何参考素材、没有声音、比云端慢,但不按次计费。"
                          "渲染需要等几分钟,本地那种常常要十分钟以上。"
                          "你看不到也听不到结果:只说你要求了什么,绝不要描述生成出来的画面。",
        # Every built-in carries its own `risk`: it runs something outside this app, so it asks.
        "risk": "exec",
        # Its own budget. Rendering is minutes, while `tool_timeout` is sized for a tool call that
        # answers quickly — without this the call would be killed mid-render.
        "timeout_key": "video_timeout",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "What to generate. On a self-hosted H3: shots, then soundscape and music. On Ark: a description of the shot, naming any references by their position (@图片1, @视频1)",
                       "description_zh": "要生成什么。自建 H3:分镜,再写声音环境与配乐。火山方舟:对画面的描述,用序号指代素材(@图片1、@视频1)"},
            "duration_seconds": {"type": "integer",
                                 "description": f"Clip length, {video.MIN_SECONDS}-{video.MAX_SECONDS} seconds, and at most what this group allows; the group's own limit is used when omitted",
                                 "description_zh": f"时长,{video.MIN_SECONDS}-{video.MAX_SECONDS} 秒,且不超过本群上限;不填则用本群允许的最长时长"},
            "aspect_ratio": {"type": "string", "enum": list(video.ASPECT_RATIOS),
                             "description": f"One of: {', '.join(video.ASPECT_RATIOS)}; omit for the provider's own default. Which of them apply depends on the provider",
                             "description_zh": f"可选:{', '.join(video.ASPECT_RATIOS)};不填则用服务商自己的默认值。具体哪些可用取决于服务商"},
            "first_frame": {"type": "string",
                            "description": "Reference image to start from: a path inside the workspace on a self-hosted H3 server or on Ark, an http(s) URL on MetaChat's video API (which fetches it itself, and where it is required). On Ark a keyframe pins the aspect ratio to adaptive. A local ComfyUI renders from the prompt alone and refuses this",
                            "description_zh": "起始参考图:自建 H3 或火山方舟可填工作目录内的路径;MetaChat 的视频接口必须是 http(s) 地址(它自己去取),而且是必填的。方舟上用了关键帧就会把画幅锁成 adaptive。本地 ComfyUI 只按提示词生成,填了会被拒绝"},
            "last_frame": {"type": "string",
                           "description": "Image to end on, same forms as first_frame. Self-hosted H3 and Ark only — MetaChat's API generates from a single keyframe, and a local ComfyUI has no image input at all, so both refuse this",
                           "description_zh": "结束图片,写法同 first_frame。仅自建 H3 与火山方舟支持;MetaChat 的接口只按一张参考图生成、本地 ComfyUI 根本没有图片输入口,填了都会被拒绝"},
            "reference_images": {"type": "array", "items": {"type": "string"},
                                 "description": "Extra reference pictures, for the models that take them (Ark only). Pass a workspace path or an http(s) URL; the prompt can name them by position (@图片1)",
                                 "description_zh": "额外的参考图,仅火山方舟支持。可填工作目录内的路径或 http(s) 地址;提示词里可以按序号引用(@图片1)"},
            "reference_videos": {"type": "array", "items": {"type": "string"},
                                 "description": "Reference clips, Ark only: give the model a camera move or a subject to carry over (@视频1)",
                                 "description_zh": "参考视频,仅火山方舟支持:把运镜或主体交给模型参考(@视频1)"},
            "reference_audios": {"type": "array", "items": {"type": "string"},
                                 "description": "Reference audio, Ark only: a voice or a piece of music to use (@音频1)",
                                 "description_zh": "参考音频,仅火山方舟支持:要沿用的音色或音乐(@音频1)"},
            "generate_audio": {"type": "boolean",
                               "description": "Ark only, and only when you want the opposite of its default: Seedance 2.5 already makes sound, so pass false for a silent clip",
                               "description_zh": "仅火山方舟,而且只在你要「反着来」时才填:Seedance 2.5 默认就生成声音,想静音才填 false"},
            "seed": {"type": "integer",
                     "description": "0 for a random result. Self-hosted H3, Ark and ComfyUI take it; MetaChat's API has no seed at all",
                     "description_zh": "填 0 表示随机。自建 H3、火山方舟和 ComfyUI 支持;MetaChat 的接口没有 seed"}},
            "required": ["prompt"]},
    },
    "assemble_video": {
        "description": "Turn the shots this group already has into ONE film: the pieces are joined, "
                       "the narration is recorded, and the words are put on screen. This is the "
                       "step between \"a few clips\" and \"a video somebody can watch\", and it is "
                       "local and free — nothing is generated and nothing is billed. Give a shot "
                       "list in order; each entry is {\"clip\": \"video/x.mp4\", \"seconds\": 6, "
                       "\"say\": \"narration to record\", \"text\": \"words on screen\"}, or "
                       "{\"title\": \"...\", \"subtitle\": \"...\"} for a card with no footage. Every "
                       "shot that shows a picture also carries `credit` — where that picture came "
                       "from and under what terms — because a film that cannot answer that cannot be "
                       "published, and shots without it are listed in the answer. "
                       "**A shot can move by itself: give it \"anim\" instead of a picture** and "
                       "this app draws the motion frame by frame — see `make_animation` for the "
                       "list. Use it for every shot that explains a mechanism, and a real "
                       "recording for anatomy: a film whose every picture is a still plays as a "
                       "slide show, and the answer says so when that happens. "
                       "Pictures can be shots too (they get a slow push and hold as long as they "
                       "are told). A shot's length follows the narration when you do not give one, "
                       "because a line cut off mid-sentence is worse than running long. The result "
                       "is one .mp4 with burned-in subtitles, plus a .srt and a shot sheet beside "
                       "it so the group can edit a line and assemble again. You cannot watch or "
                       "hear the result — report what you assembled, never how it looks.",
        "description_zh": "把本群已有的镜头拼成**一条**成片:片段接起来、旁白配上、字上屏。它就是「有"
                          "几段片段」到「别人能看的片子」之间的那一步,全程在本机完成、不花钱——不生成任何"
                          "东西。按顺序给一份分镜表,每条形如 {\"clip\": \"video/x.mp4\", \"seconds\": 6, "
                          "\"say\": \"要录的旁白\", \"text\": \"屏幕上的字\"};没有素材的卡片写 "
                          "{\"title\": \"...\", \"subtitle\": \"...\"}。**镜头可以自己动:把画面换成 `anim`**"
                          "(本程序逐帧画出来,可用清单见 `make_animation`);凡是解释机制的镜头都用它,解剖用真实录像 —— "
                          "每一镜都是静帧的片子播出来就是幻灯片,装配结果会明确告诉你这一点。"
                          "图片也能当镜头(会缓慢推近,并按住"
                          "你给的时长)。不写时长时,镜头长度跟着旁白走 —— 话被截断比超时长更糟。产物是一个"
                          "烧好字幕的 .mp4,旁边还有 .srt 和一份分镜表,便于改一行重新装配。"
                          "你看不到也听不到结果:只说你装配了什么,不要描述画面。",
        # It runs `ffmpeg` and `say` on this machine, so it asks like the other runners do.
        "risk": "exec",
        # Its own budget: a three-minute film is thousands of frames, and `tool_timeout` is sized for
        # a call that answers quickly.
        "timeout_key": "assemble_timeout",
        "parameters": {"type": "object", "properties": {
            "layout": {"type": "string",
                       "description": "Which look to assemble in. \"default\" is the plain one. "
                                      "The shipped names are default, public-science (vertical, big "
                                      "subtitles held low, an opening and closing card, a standing "
                                      "credit line at the top), case-review (16:9, small captions, "
                                      "tight shots) and lecture (16:9, plain captions); the user's "
                                      "own layouts sit beside those in their layouts folder. It "
                                      "decides picture size, frame rate, caption size and "
                                      "placement, the cards, the credit line and how long a shot "
                                      "may run — an explicit `size`/`fps`/`fit` still wins over it",
                       "description_zh": "用哪套版式来装配。\"default\" 是原本那套朴素版式。自带的名字有 "
                                         "default(朴素)、public-science(竖屏、字幕更大更靠下、有片头片尾卡、"
                                         "顶部一条贯穿全片的署名/出处行)、case-review(16:9、小字幕、节奏紧)、"
                                         "lecture(16:9、朴素字幕);用户自己的版式和他自带的放在一起。"
                                         "它决定画幅、帧率、字幕大小与位置、片头片尾卡、署名行、以及单个镜头的时长上限 —— "
                                         "显式给了 size/fps/fit 时以显式值为准"},
            "shots": {"type": "array", "items": {"type": "object"}, "description":
                      "The shots, in order. Each: {clip: a path inside the workspace, seconds: how long, "
                      "say: narration to record, text: words on screen, audio: voice|clip|mix|silent, "
                      "motion: true|false for a still, anim: an animation spec} — or {title, subtitle} "
                      "for a card with no footage. `anim` is either a name or {kind, camera, "
                      "labels, coil_turns, show}",
                      "description_zh":
                      "按顺序的镜头。每条形如 {clip: 工作目录内的路径, seconds: 时长, say: 要录的旁白, "
                      "text: 屏幕字幕, audio: voice|clip|mix|silent, motion: 静帧是否缓慢推近, anim: 让镜头自己动的动画} —— "
                      "没有素材的卡片写 {title, subtitle}"},
            "size": {"type": "string", "enum": list(assemble.SIZES),
                     "description": f"Picture size, one of: {', '.join(assemble.SIZES)}. 1080x1920 is the "
                                    "default (vertical); footage of any shape is fitted into it",
                     "description_zh": f"画幅,可选:{', '.join(assemble.SIZES)}。默认 1080x1920(竖屏);"
                                       "任何形状的素材都会被装进去"},
            "total_seconds": {"type": "integer",
                              "description": "The length the film has to be, if there is one. When it is "
                                             "short of the target, still shots are held longer and the "
                                             "result says by how much; when there is no still to hold, "
                                             "the shortfall is reported instead of faked",
                              "description_zh": "成片必须达到的总时长(如果有要求)。差得比目标短时,"
                                               "会按比例延长静帧镜头并说明延长了多少;没有静帧可延长时,"
                                               "如实报告差多少,不会假装凑够"},
            "fps": {"type": "integer", "description": "Frames per second; 24 by default",
                    "description_zh": "帧率,默认 24"},
            "fit": {"type": "string", "enum": list(assemble.FITS),
                    "description": "How footage of another shape goes into the frame. blur (default) keeps "
                                   "all of the picture and fills the rest with a blurred copy of it; "
                                   "cover fills the frame by cropping the sides away; contain leaves black "
                                   "bars. Use cover only when the subject is in the middle",
                    "description_zh": "素材形状不匹配时怎么装进画幅。blur(默认)保留完整画面,空出来的部分用"
                                      "它自己的模糊副本来填;cover 靠裁掉两侧铺满;contain 留黑边。"
                                      "只有当主体在正中间时才用 cover"},
            "voice": {"type": "string",
                      "description": "The voice to narrate with. A plain name is a macOS system "
                                     "voice; `voice:<name>` is one of the user's own cloned voices "
                                     "(see the voices folder), which is a local model and takes "
                                     "longer on its first line. Left out, a system voice is chosen "
                                     "by the language on screen. The result names the voice used",
                      "description_zh": "配旁白用的语音。普通名字是 macOS 系统嗓音;`voice:<名字>` 是"
                                        "用户自己克隆的音色(见 voices 目录下的那些),它是个本地模型,"
                                        "第一句会慢一些。留空则按屏幕上的语言挑一个系统嗓音。结果里会写明用的哪个"},
            "subtitles": {"type": "string", "enum": ["burn", "off"],
                          "description": "burn (default) puts the words into the picture; off leaves them "
                                         "in the .srt only",
                          "description_zh": "burn(默认)把字烧进画面;off 只留 .srt 文件"},
            "name": {"type": "string", "description": "What to call the film; left out, it is stamped with the time",
                     "description_zh": "成片的名字;留空则用时间戳"},
            "title": {"type": "string",
                      "description": "Title of the film. Given one, the layout draws a title card in "
                                     "front of the shots; left out, the film opens on the first shot",
                      "description_zh": "成片标题。填了就在成片最前面加一张标题卡;留空则直接从第一个镜头开始"},
            "closing": {"type": "string",
                        "description": "Text for a closing card after the last shot — a summary line, "
                                       "a call to action, or a disclaimer. Left out, the film ends on "
                                       "the last shot",
                        "description_zh": "片尾卡上的字 —— 一句总结、一句引导、或一句免责声明。留空则以最后一个镜头结束"},
            "credit": {"type": "string",
                       "description": "A standing line printed on **every** frame: whose pictures these "
                                      "are, which unit made it, where it may be used. It is on every "
                                      "frame rather than on a card because a re-cut drops cards first",
                       "description_zh": "印在**每一帧**上的一句话:画面来自谁、哪个单位做的、可用于何处。"
                                         "它印在每一帧而不是一张卡上,因为重新剪辑时最先丢的就是卡"}},
            "required": ["shots"]},
    },
    "make_animation": {
        "description": "Draw a real animation of a mechanism and save it as a clip: blood flowing "
                       "along the vessel, a weak spot bulging out into the sac, a coil being wound "
                       "into it, the microcatheter being navigated there, contrast opacifying the "
                       "finding. Every frame is computed and drawn on this machine, so the anatomy is "
                       "exactly the drawing the stills use and nothing is invented; the clip lands in "
                       "the workspace as an .mp4 ready to be a shot's `clip` in `assemble_video`, with "
                       "a poster frame beside it to look at first. Reach for this for every shot that "
                       "explains *how* something works — a film of still pictures plays as a slide "
                       "show. The drawings:\n" + animate.describe(),
        "description_zh": "把一个机制画成**真正的动画**并存成片段:血流沿血管流动、薄弱处鼓出成瘤、弹簧圈一圈圈填进去、"
                          "微导管被导航过去、造影剂让病变显影。每一帧都是本机算出来画出来的,所以解剖与静帧用的是同一张图、"
                          "不会凭空编造;产物是工作目录里的 .mp4,可直接当 `assemble_video` 里某镜的 `clip`,"
                          "旁边还有一张关键帧可以先看。凡是解释**机制**的镜头都用它 —— 一串静帧播出来就是幻灯片。"
                          "可选画面:\n" + animate.describe("zh"),
        # It draws frames here and runs `ffmpeg` to encode them, so it asks like the other runners.
        "risk": "exec",
        "timeout_key": "assemble_timeout",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(animate.ANIMATIONS),
                     "description": "Which animation to draw",
                     "description_zh": "画哪一种动画"},
            "seconds": {"type": "number",
                        "description": "How long the clip is; 5 by default. The motion is spread over "
                                       "exactly this long",
                        "description_zh": "片段多长,默认 5 秒。动作会正好铺满这个时长"},
            "size": {"type": "string", "enum": list(assemble.SIZES),
                     "description": f"Picture size, one of: {', '.join(assemble.SIZES)}. Keep it the same "
                                    "as the film's so the shot needs no rescaling",
                     "description_zh": f"画幅,可选:{', '.join(assemble.SIZES)}。请与成片一致,镜头就不用缩放"},
            "heading": {"type": "string", "description": "The line across the top: what we are looking at",
                        "description_zh": "顶部那一行:我们在看什么"},
            "credit": {"type": "string",
                       "description": "Where this drawing comes from — a drawing made here is not a real "
                                      "image, and the film has to say so",
                       "description_zh": "这张图的来源 —— 本程序画的不是真实影像,成片必须说明这一点"},
            "params": {"type": "object", "description":
                       "Shape and labels: {sac_at_x, sac_r, coil_turns, particles, show: [sac, catheter, "
                       "coil, flow], camera: none|push|pull, labels: [{text, at_part, from, to}]}. "
                       "`at_part` takes an anchor name (sac, coil, catheter_tip, vessel, flow_in, "
                       "flow_out) so a label points at the right thing instead of at a guessed "
                       "position; `from`/`to` are times in 0..1, so a label can appear when its part does",
                       "description_zh":
                       "形状与标注:{sac_at_x, sac_r, coil_turns, particles, show: [sac, catheter, coil, "
                       "flow], camera: none|push|pull, labels: [{text, at_part, from, to}]}。"
                       "`at_part` 用锚点名(sac/coil/catheter_tip/vessel/flow_in/flow_out),标注才会指到对的东西上;"
                       "`from`/`to` 是 0..1 的时间,标注就能在它说的部件出现时才出现"},
            "name": {"type": "string",
                     "description": "What to call the clip; left out, it is named after the animation",
                     "description_zh": "片段的名字;留空则用动画名"}},
            "required": ["kind"]},
    },
    "list_figures": {
        "description": "List the real pictures and recordings that came with one of this group's "
                       "documents. A knowledge base usually came from notes, atlases or case "
                       "collections that embed their own figures — angiograms, anatomical plates, "
                       "intra-operative photographs, procedure recordings — and those are already "
                       "correct, already captioned and already reviewed by somebody who knows the "
                       "subject. **Look here before you generate anything for a shot that shows "
                       "anatomy, a lesion, an instrument or a step of a procedure**: a real picture "
                       "beats anything a model can invent, and a model asked for a realistic artery "
                       "has no anatomy to draw from — only the metaphor in your prompt. The names it "
                       "prints are what `make_figure` takes.",
        "description_zh": "列出本群某篇文档**自己带的**真实图片与影像。知识库通常来自笔记、图谱或病例库,"
                          "而它们本身就嵌着自己的配图 —— 造影、解剖图版、术中照片、手术录像 —— 这些图本来是对的、"
                          "本来就有图注、而且被懂行的人审过。**凡是画面里要出现解剖结构、病变、器械或操作步骤的镜头,"
                          "先查这里,再考虑生成**:真实图片胜过模型能造出的任何东西,而被要求画「写实的动脉」的模型"
                          "并没有解剖可依,它只有你提示词里那个比喻。它打印出来的名字就是 `make_figure` 要收的。",
        "risk": "read",
        "parameters": {"type": "object", "properties": {
            "doc": {"type": "string", "description": "Document title (or id) — the same title library_search prints",
                    "description_zh": "文档标题(或 id)—— 和 library_search 打印出来的标题一样"}},
            "required": ["doc"]},
    },
    "make_figure": {
        "description": "Make ONE teaching picture for a film, out of exactly one of three sources. "
                       "(a) A real picture — from this group's workspace (`image`) or from a document's "
                       "own figures (`doc` + `figure`) — put on a titled canvas with a caption and a "
                       "credit line, optionally with arrows, circles, labels and a magnified inset. "
                       "(b) A schematic this app draws (`schematic`): a vessel, a bulge on its wall, a "
                       "catheter, a coil, an arrow for flow — clean, repeatable, and honest, because it "
                       "claims nothing it was not drawn with. For explaining a procedure to a general "
                       "audience the schematic is usually the better choice; use real pictures when the "
                       "subject has to be shown as it actually is. Marks are given as fractions of the "
                       "frame (0..1 from the top-left), never pixels. The file lands in this group's "
                       "workspace as a still, ready to be a shot in `assemble_video`, and it carries "
                       "**A still is the last frame of the same drawing** — these drawings can move "
                       "(`make_animation`: blood flowing, a bulge growing, a coil filling in). If the "
                       "shot has to explain how something works, ask for the animation rather than the "
                       "still: a film of stills plays as a slide show. "
                       "its own credit line — a picture in a film that cannot say where it came from is "
                       "one nobody may publish. You cannot see the result: say what you made, never "
                       "describe how it looks.",
        "description_zh": "为成片做**一张**教学图,来源三选一:(a) 真实图片 —— 本群工作目录里的(`image`),"
                          "或某篇文档自己带的配图(`doc` + `figure`)—— 放到带标题、图注和来源行的画布上,"
                          "可以加箭头、圈注、结构名和放大 inset;(b) 本程序自绘的**示意图**(`schematic`):"
                          "血管、管壁上的囊、微导管、弹簧圈、血流箭头 —— 干净、可重复、而且诚实,因为它不声称"
                          "任何没画进去的东西。**给大众讲操作过程时,示意图通常比照片更好用**;需要「就是长这样」"
                          "时才用真实图片。标注的位置用画面比例(0..1,从左上角算),不要给像素。产物落在本群"
                          "工作目录里,是一张静帧,可直接作为 `assemble_video` 的镜头,而且自带来源行。"
                          "**静帧就是同一张画的最后一帧** —— 这些画是可以动的(`make_animation`:血流、鼓出、"
                          "弹簧圈填塞)。如果这一镜是解释机制怎么运作的,要的就是动画而不是静帧:"
                          "一串静帧播出来就是幻灯片。 "
                          "片子里一张说不清来路的图,是谁都不能发布的。你看不到结果:只说你做了什么,不要描述画面。",
        # It writes a file in the workspace and executes nothing, so it is a write rather than an exec.
        "risk": "write",
        "parameters": {"type": "object", "properties": {
            "image": {"type": "string",
                      "description": "A real picture inside this group's workspace (relative path), e.g. uploads/x.jpg",
                      "description_zh": "本群工作目录内的真实图片(相对路径),例如 uploads/x.jpg"},
            "doc": {"type": "string",
                    "description": "A document title to take the picture from — with `figure`, and after list_figures",
                    "description_zh": "从中取图的文档标题 —— 与 `figure` 一起用,先跑 list_figures"},
            "figure": {"type": "string",
                       "description": "Which of that document's figures: its name, part of its name, or its number (\"2\")",
                       "description_zh": "该文档的哪一张图:名字、名字的一部分,或序号(「2」)"},
            "schematic": {"type": "string", "enum": list(figure.SCHEMATICS),
                          "description": f"Draw one of this app's schematics instead of using a picture: {', '.join(figure.SCHEMATICS)}",
                          "description_zh": f"改用自绘示意图:{', '.join(figure.SCHEMATICS)}"},
            "params": {"type": "object",
                       "description": "Schematic parameters: {show: [sac, catheter, coil, flow], sac_at_x, sac_r, "
                                      "coil_turns, labels: [{text, at: [x, y]}]}",
                       "description_zh": "示意图参数:{show: [sac, catheter, coil, flow], sac_at_x, sac_r, "
                                         "coil_turns, labels: [{text, at: [x, y]}]}"},
            "heading": {"type": "string", "description": "The band at the top: what we are looking at",
                        "description_zh": "顶部标题带:我们在看什么"},
            "caption": {"type": "string", "description": "One or two lines under the picture: what to notice",
                        "description_zh": "画面下一两行图注:该注意什么"},
            "credit": {"type": "string",
                       "description": "Where the picture came from and under what terms — e.g. \"Source: <url> · "
                                      "internal reference only\". Left out for a document's own figure, the "
                                      "document's recorded source is used; a schematic says it is a drawing",
                       "description_zh": "图片来自哪里、以什么条件使用 —— 如「来源:<url> · 仅内部对照」。"
                                         "取文档配图时留空会用该文档记录的来源;示意图则自动标注「示意图」"},
            "marks": {"type": "array", "items": {"type": "object"},
                      "description": "Marks on the picture: {kind: arrow|circle|label|inset, at: [x, y], from: [x, y] "
                                     "for an arrow, text, radius, zoom, corner}",
                      "description_zh": "图上的标注:{kind: arrow|circle|label|inset, at: [x, y], "
                                        "箭头再加 from: [x, y], 以及 text、radius、zoom、corner}"},
            "size": {"type": "string", "enum": list(assemble.SIZES),
                     "description": "Frame size; 1080x1920 by default",
                     "description_zh": "画幅;默认 1080x1920"},
            "fit": {"type": "string", "enum": ["full", "cover"],
                    "description": "full (default) keeps the whole picture — right for an angiogram, where the "
                                   "cropped part is often the part somebody points at; cover fills the frame",
                    "description_zh": "full(默认)保留完整画面 —— 造影图应该这样,被裁掉的那块往往正是要指的地方;"
                                     "cover 则填满画面"},
            "name": {"type": "string", "description": "What to call the file", "description_zh": "文件名"}},
            "required": []},
    },
    "write_document": {
        "description": "Put a finished draft into a real file the user can open: .docx (Word), .pptx "
                       "(slides), .xlsx (a workbook) or .md. This is how a task that says \"deliver a "
                       "report / a deck / a paper / a poster's text\" actually ends — talking about it "
                       "in the chat is not the deliverable. The body is plain Markdown-ish text and the "
                       "same body works in every format: `#`/`##` headings, `- ` bullets, `1. ` numbered "
                       "items, `| a | b |` tables, blank line between paragraphs. In .pptx each `##` "
                       "starts a slide (its body is the bullets under it); in .xlsx each `##` is a sheet. "
                       "The file lands inside this group's workspace and is indexed into its library, so "
                       "the next member can read it by title. Rewriting the same file name replaces it — "
                       "that is how a second draft is delivered. Nothing is generated or checked here: it "
                       "lays out the text you wrote. You cannot open the result, so do not describe the "
                       "pages — report the path, the size and what you put in it.",
        "description_zh": "把**写完的稿子**落成一个用户能打开的真实文件:.docx(Word)、.pptx(幻灯片)、"
                          ".xlsx(表格)、.md。凡是「交付一份报告/课件/论文/海报文案」的任务,终点就是它 —— "
                          "在群里聊过不算交付。正文是 Markdown 风格纯文本,四种格式共用同一份正文:"
                          "`#`/`##` 是标题、`- ` 是项目符号、`1. ` 是编号、`| a | b |` 是表格、空行分段。"
                          "在 .pptx 里每个 `##` 开一页(它下面的条目就是该页正文);在 .xlsx 里每个 `##` 是一张表。"
                          "文件落在本群工作目录里,并会被索引进本群知识库,下一位成员可以按标题读到它。"
                          "**用同一个文件名再写一次就是覆盖** —— 第二稿就该这么交。这里不生成也不核对内容,"
                          "只把你写的字排好;你看不到成品,所以不要描述版面,报路径、大小和你放了什么进去。",
        # It writes a file inside the workspace and runs nothing: a write, like `make_figure`.
        "risk": "write",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Where to write, relative to this group's workspace, e.g. 交付/科普脚本.docx. "
                                    "The suffix is added from `format` if you leave it out",
                     "description_zh": "写到哪里(相对本群工作目录),例如 交付/科普脚本.docx。"
                                       "不写后缀就按 `format` 补上"},
            "format": {"type": "string", "enum": list(docwrite.KINDS),
                       "description": "docx (Word) / pptx (slides) / xlsx (workbook) / md (plain text)",
                       "description_zh": "docx(Word)/ pptx(幻灯片)/ xlsx(表格)/ md(纯文本)"},
            "title": {"type": "string", "description": "The document's title, shown as its first heading "
                                                       "(and as the cover slide in pptx)",
                      "description_zh": "文档标题,作为第一个标题显示(pptx 里就是封面页标题)"},
            "body": {"type": "string", "description": "The whole text to lay out, in the Markdown-ish form "
                                                      "described above. This is the deliverable itself: write it "
                                                      "out in full, not a summary of it",
                     "description_zh": "要排版的全文,用上面那种 Markdown 风格。**交付物就是这段正文**:"
                                       "请写全,不要写成摘要"}},
            "required": ["path", "format", "body"]},
    },
    "process_log": {
        "description": "The ledger of what is wrong with the WAY THIS GROUP WORKS — not with the "
                       "content of anybody's document. Four actions:\n"
                       "  `scan` — the measured facts, straight out of the app's own record: who "
                       "spoke and who never did, which tool calls failed and with what, which tasks "
                       "never finished and why, which files the plan promised that are NOT on disk, "
                       "what is actually in the workspace, which system notes repeated, and where the "
                       "ledger itself stands. **Read this first.** An audit written from memory is an "
                       "opinion; an audit written against a scan is a finding.\n"
                       "  `report` — add one entry (title, symptom, evidence, severity, stage, cause, "
                       "fix). Evidence means what you measured: the system note, the task id, the "
                       "file that is missing. An entry without evidence is not worth keeping.\n"
                       "  `update` — move an entry on: status (open / fixed / verified / wontfix), a "
                       "note, and what a re-check showed. `verified` claims the same thing was run "
                       "again and the defect is gone; do not use it for \"I changed something\".\n"
                       "  `list` — read the ledger back, by status.\n"
                       "The ledger is a Markdown file in this group's workspace and is indexed into "
                       "its library, so the user can open it and the next round can see what has "
                       "already been reported and what has already been ruled out.\n"
                       "**It also fills itself**: after every round the app records the defects it "
                       "can measure (`by: auto`), and a defect seen again is *counted* (`seen ×N`) "
                       "rather than written twice. So read before you write — if the entry you were "
                       "about to add is already open, `update` that one instead.",
        "description_zh": "记录**这个群做事方式**的毛病的账本(不是哪位成员文档内容的问题)。四个动作:\n"
                          "  `scan` —— 程序自己记录下来的**实测事实**:谁发过言、谁一次没发,哪些工具调用失败以及失败原因,"
                          "哪些任务没做完及原因,计划里承诺过、但**工作目录里并不存在**的文件,工作目录里实际有什么,"
                          "哪条系统提示重复出现,以及账本自身现状。**先跑它。** 凭记忆写的审核是意见,"
                          "对着 scan 写的审核才是发现。\n"
                          "  `report` —— 新增一条:标题、现象、依据、严重度、环节、根因、修法。"
                          "「依据」指你量到的东西:哪条系统提示、哪个任务号、哪个文件不存在。没有依据的条目不值得留。\n"
                          "  `update` —— 推进一条:状态(open / fixed / verified / wontfix)、备注、以及复核结果。"
                          "`verified` 的含义是「同样的事又跑了一遍,问题已消失」,不要拿它表示「我改了点东西」。\n"
                          "  `list` —— 按状态把账本读回来。\n"
                          "账本是本群工作目录里的一个 Markdown 文件,并已索引进本群知识库 —— 用户能打开,"
                          "下一轮也能看到哪些已经报过、哪些已经被排除。**它还会自己填**:每一轮结束后程序会把"
                          "自己量得出来的毛病写进去(标 `by: auto`),同一个毛病再出现是**计数**"
                          "(`seen ×N`)而不是多写一条。所以**先读再写** —— 你想记的那条如果已经开着,"
                          "就用 `update` 推进它,不要另开一条。",
        # It writes a file inside the workspace and runs nothing (like `write_document`).
        "risk": "write",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["scan", "report", "update", "list"],
                       "description": "scan / report / update / list",
                       "description_zh": "scan / report / update / list"},
            "window": {"type": "integer",
                       "description": "scan: how many recent messages to measure; 120 by default, 500 at most",
                       "description_zh": "scan:量最近多少条消息;默认 120,最多 500"},
            "title": {"type": "string", "description": "report: one line naming the defect",
                      "description_zh": "report:一句话说明这个毛病"},
            "symptom": {"type": "string", "description": "report: what actually went wrong, seen from outside",
                        "description_zh": "report:从外面看到的现象是什么"},
            "evidence": {"type": "string",
                         "description": "report: what you measured — the note, the task id, the missing file",
                         "description_zh": "report:你量到的依据 —— 哪条提示、哪个任务号、哪个文件不在"},
            "severity": {"type": "string", "enum": list(proclog.SEVERITIES),
                         "description": "report: blocker (the work cannot finish) / major / minor",
                         "description_zh": "report:blocker(活干不完)/ major / minor"},
            "stage": {"type": "string", "enum": list(proclog.STAGES),
                      "description": "report: which part of the flow it is about",
                      "description_zh": "report:毛病出在流程的哪一段"},
            "cause": {"type": "string", "description": "report: why it happened, if you can tell",
                      "description_zh": "report:能判断的话,根因是什么"},
            "fix": {"type": "string", "description": "report: what should change",
                    "description_zh": "report:应该改什么"},
            "id": {"type": "string", "description": "update: the entry id, e.g. P-20260924-1",
                   "description_zh": "update:条目编号,例如 P-20260924-1"},
            "status": {"type": "string", "enum": list(proclog.STATES),
                       "description": "update: the new state",
                       "description_zh": "update:新的状态"},
            "note": {"type": "string", "description": "update: what was done about it",
                     "description_zh": "update:为它做了什么"},
            "verify": {"type": "string",
                       "description": "update: what the re-run showed (required in spirit for `verified`)",
                       "description_zh": "update:复核时看到的结果(`verified` 必须有这个)"},
            "status_filter": {"type": "string", "enum": list(proclog.STATES),
                              "description": "list: only entries in this state; default is open + fixed",
                              "description_zh": "list:只看这个状态的条目;默认是 open + fixed"}},
            "required": ["action"]},
    },
    "ask_advisor": {
        "description": "Ask an AI that is NOT a member of this group — the codex or Claude Code "
                       "command-line program installed on this machine — for a second opinion, "
                       "read-only. It is run inside this group's working directory and may read "
                       "anything in there; its tools are read-only and it is told not to change "
                       "anything. Hand it the *evidence* you already have (the scan output, the "
                       "entries you are unsure about) and ask a narrow question about the process — "
                       "why a step keeps failing, what to change in the way the work is split, "
                       "checked or handed over. What comes back is a hypothesis from a model that "
                       "cannot see the chat or the app: check each claim before you act on it, and "
                       "report it as \"the outside model says …\", never as a fact.\n"
                       "It takes minutes and costs money, and the user is asked to approve each call "
                       "(it runs a program on this machine). Ask once, and ask well. It is not for "
                       "writing content, and not for anything a search of this group's own library "
                       "would answer.",
        "description_zh": "问一个**不是本群成员**的 AI —— 这台机器上装的 codex 或 Claude Code 命令行 —— 要一份"
                          "只读的第二意见。它在本群工作目录里运行,可以读那里面的任何东西;它的工具是只读的,"
                          "也被告知不要改动任何东西。把手头已有的**依据**交给它(scan 的结果、你拿不准的条目),"
                          "问一个**窄**问题,而且问的是流程 —— 某一步为什么反复失败、分工/核查/交接的方式该改什么。"
                          "它给的是一个既看不到群聊、也看不到本程序的模型提出的**假设**:每条都要自己核对再动手,"
                          "引用时必须写成「外部模型认为……」,不能当事实。\n"
                          "一次几分钟、且要花钱,而且**每一次调用都要用户批准**(它在本机跑程序)。所以要一次问透;"
                          "它不是用来写正文的,也不是用来回答「查一下本群知识库就能知道」的问题的。",
        # It starts a program from this machine: an `exec`-risk tool, so the user is asked before
        # each call. That is the right friction — the outside model has whatever permissions its own
        # CLI gives it, which this app can only pin down for the built-in commands (`--tools Read,…`
        # for claude, `-s read-only` for codex), not for a command the user wrote themselves.
        "risk": "exec",
        "timeout_key": "advisor_timeout",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string",
                         "description": "What to ask, in one narrow question about the process",
                         "description_zh": "问什么:一个关于流程的、窄的问题"},
            "evidence": {"type": "string",
                         "description": "The facts to hand over — paste the `scan` output and the "
                                        "entries you are unsure about, not a summary of them",
                         "description_zh": "要交过去的依据 —— 把 `scan` 的输出和拿不准的条目原样贴上,"
                                           "不要写成摘要"}},
            "required": ["question"]},
    },
    "review_picture": {
        "description": "Look at ONE picture, or ONE frame of a video, in this group's workspace — through "
                       "the vision model this app is set up with — and get back what is actually in it. "
                       "Use it before signing off any picture or film: you cannot see, so this is how a "
                       "claim about how something looks gets checked instead of assumed. For a video, "
                       "pass `at_seconds` to choose the moment. Say what you want checked, and treat the "
                       "answer as a description of the picture, not as proof that it is clinically right.",
        "description_zh": "看**一张**本群工作目录里的图片,或一段视频中的**一帧** —— 借本程序配置的视觉模型,"
                          "拿回画面里真实有什么。给任何图或成片签字之前都用它:你自己看不见,「看起来对不对」"
                          "只能这样核,不能靠假定。视频要指定时刻就传 `at_seconds`。请写清要核什么,"
                          "并把回答当成对画面的**描述**,而不是「内容在临床上正确」的证明。",
        # `read`, not `exec`. It never writes and never changes anything, and the only effect outside
        # this machine is showing a picture to a cloud model — which is already governed by the
        # `vision_cloud` switch, the same explicit decision that applies when a user attaches an image
        # and it gets described. Marking it `exec` would put an approval prompt, and a read-only round's
        # tool list, in front of the one step whose whole purpose is to be used often.
        "risk": "read",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Path inside this group's workspace, e.g. video/shot.png or figures/x.png",
                     "description_zh": "本群工作目录内的相对路径,例如 video/shot.png 或 figures/x.png"},
            "question": {"type": "string",
                         "description": "What to check: e.g. \"is the blood flowing along the vessel, and does "
                                        "the tube look round?\" The answer is written against this",
                         "description_zh": "要核什么:例如「血是不是沿血管在流、管子看起来是不是圆的?」回答会针对它写"},
            "at_seconds": {"type": "number",
                           "description": "For a video: which moment to look at, in seconds; halfway by default",
                           "description_zh": "视频:看第几秒那一帧,默认取中间"}},
            "required": ["path"]},
    },
    "review_audio": {
        "description": "Hear an audio file, or the sound track of a video, inside this group's workspace: "
                       "how long it is, whether there is any sound in it at all, and a transcript of what "
                       "is said. Use it to check a voice sample or a narration before it goes into a "
                       "film. Transcription happens **on this machine** and nothing is sent anywhere, so "
                       "it needs no cloud model. If this machine has no transcriber the answer says so "
                       "and gives the single command that installs one — say that to the user rather than "
                       "guessing what the recording says.",
        "description_zh": "听一段本群工作目录里的音频,或一段视频里的声音:多长、里面到底有没有声音、说了什么。"
                          "音色样本或旁白要进成片之前,用它核。转写**在本机**完成、不外发,不需要云端模型。"
                          "这台机器若没装转写器,回答会明说,并给出装上它需要的那一条命令 —— "
                          "请把这句话转告用户,不要猜录音里说了什么。",
        # `read`, like `review_picture`: it changes nothing, and the one thing it runs is a local
        # transcriber writing nothing outside a temporary directory. Its own deadline is the
        # local-heavy-work budget rather than the generic one, because transcribing an hour of
        # narration is not a call that answers quickly.
        "risk": "read",
        "timeout_key": "assemble_timeout",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Path inside this group's workspace — an audio file, or a video whose "
                                    "sound you want",
                     "description_zh": "本群工作目录内的相对路径 —— 音频文件,或你想听它声音的视频"}},
            "required": ["path"]},
    },
    # ---------------------------------------------------------------- reading a reference
    "study_video": {
        "description": "Study someone else's video — a link (YouTube, Bilibili, Douyin and the other "
                       "sites `yt-dlp` knows) or a file in this group's workspace — and write down how it "
                       "is made: the aspect and length, how many shots and how long each, where the "
                       "captions sit and how big they are, how the sound is used, and what would have to "
                       "be true of a new video to read as the same format. It measures the file with "
                       "ffmpeg (length, frame size, frame rate, every cut and its timing, how loud the "
                       "audio is), looks at frames spread across it, listens to the speech, and leaves a "
                       "`参考风格-….md` brief in the workspace that the whole group can then work from. "
                       "Use it before making something 'in the style of' a reference, instead of guessing "
                       "the style from a description. Format and pacing are meant to be reused; the "
                       "reference's own footage and music are not — the brief says so.",
        "description_zh": "看懂别人做的视频 —— 一个链接(YouTube、B 站、抖音等 `yt-dlp` 支持的站),"
                          "或本群工作目录里的文件 —— 并把它的做法写下来:画幅与时长、几个镜头各多长、"
                          "字幕在什么位置多大、声音怎么用、要让新视频读起来是同一个格式必须满足哪几条。"
                          "它先用 ffmpeg 量(时长、画面尺寸、帧率、每一次切点及其时刻、音量),"
                          "再看散布在全片的若干帧、听里面的说话,最后在工作目录留下一份 `参考风格-….md`,"
                          "全群都能照着做。要「参考某个片子做」时先用它,别凭一句描述猜风格。"
                          "**可以照搬的是版式与节奏,参考片自己的画面素材与音乐不行** —— 规格里写明了。",
        # `exec`: unlike `review_picture`, this one *runs an external program* (`yt-dlp`) against a URL
        # the user chose and downloads what it finds. That is the guide's own line for `exec`, and it is
        # also the honest one: pulling a file off a platform is an action on the world, not a look.
        "risk": "exec",
        # Two model passes plus a download plus scene detection: the local-heavy budget, not the default.
        "timeout_key": "assemble_timeout",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string",
                    "description": "Link to the reference video (any site yt-dlp supports). Give this "
                                   "or `path`, not both",
                    "description_zh": "参考视频的链接(`yt-dlp` 支持的站都行)。它和 `path` 给一个即可"},
            "path": {"type": "string",
                     "description": "Or a video already inside this group's workspace",
                     "description_zh": "或者本群工作目录里已有的视频"},
            "note": {"type": "string",
                     "description": "What about this reference matters for our film (optional)",
                     "description_zh": "这个参考片里我们最关心什么(可选)"},
            "frames": {"type": "integer",
                       "description": "How many frames to look at, 4-12 (default 8)",
                       "description_zh": "看多少帧,4-12(默认 8)"}},
            "required": []},
    },
    "generate_image": {
        "description": "Draw one image from a text description through the image service this "
                       "machine is configured with. Describe the image itself — subject, "
                       "composition, style, and any text that must appear — rather than asking "
                       "for a summary or a diagram of a document. The file lands in this group's "
                       "workspace. You cannot see the result, so say what you asked for, never "
                       "describe what came out.",
        "description_zh": "用本机配置的图片服务,按文字描述画一张图。描述要写画面本身 —— 主体、构图、风格,"
                          "以及需要出现的文字 —— 而不是让它「总结一份文档」或「画一张文档的图」。文件会落在"
                          "本群工作目录里。你看不到生成结果,所以只说你要求了什么,绝不要描述画出来是什么样。",
        "risk": "exec",            # it reaches a service outside this app, so it asks first
        "timeout_key": "image_timeout",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "What to draw, written as a description of the picture",
                       "description_zh": "要画什么,写成对画面的描述"},
            "size": {"type": "string", "enum": list(imagegen.SIZES),
                     "description": f"One of: {', '.join(imagegen.SIZES)}; the configured default is used when omitted",
                     "description_zh": f"可选:{', '.join(imagegen.SIZES)};不填则用配置里的默认尺寸"}},
            "required": ["prompt"]},
    },
}

# Every spec is handed out through here so the descriptions follow the request language.
BUILTIN_TOOL_NAMES: tuple[str, ...] = tuple(BUILTIN_SPECS)


def ratio_label(w: int, h: int) -> str:
    """`"9:16"` for a picture whose sides are in a ratio anyone would recognise, else `"1080:1920"`.

    Named ratios only, because "9:16" is what a group agrees on in words and what a provider is asked
    for — comparing a measurement against a name has to go through the same vocabulary, or every clip
    would look non-compliant for being 1080x1920 rather than 0.5625.
    """
    if not w or not h:
        return ""
    for label, (a, b) in (("16:9", (16, 9)), ("9:16", (9, 16)), ("1:1", (1, 1)), ("4:3", (4, 3)),
                          ("3:4", (3, 4)), ("21:9", (21, 9)), ("2:3", (2, 3)), ("3:2", (3, 2))):
        if abs(w / h - a / b) <= 0.012 * (a / b):
            return label
    return f"{w}:{h}"


def builtin_specs() -> dict[str, dict]:
    """The built-in tool specs, described in the request language."""
    return i18n.localize(BUILTIN_SPECS)


def read_only_tools(tools: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Split a tool map into what a network-triggered round may use, and what it may not.

    The test is `risk == "read"` rather than "not exec and not write" on purpose: a tool whose
    risk word this code does not recognize has to disappear, not pass through — the same
    reasoning as `approvals.risk_of`. MCP tools fall into that group too, since the MCP branch
    stores no `risk` at all: a server advertising its own tool as read-only is repeating the
    server's claim, not something this app has verified, so those are withheld as well.
    """
    kept = {n: s for n, s in tools.items() if s.get("risk") == "read"}
    return kept, [n for n in tools if n not in kept]


def _age_label(when: float) -> str:
    """How long ago a file was written, in the words a reader uses ("3 minutes ago").

    The process log's file list is read as a timeline — "the deliverable is older than the plan that
    promised it" is a finding — so the age is shown next to the size rather than a raw timestamp
    nobody converts in their head.
    """
    minutes = max(0.0, (time.time() - when) / 60)
    if minutes < 1:
        return i18n.pick_now("just now", "刚刚")
    if minutes < 60:
        return i18n.pick_now(f"{int(minutes)} min ago", f"{int(minutes)} 分钟前")
    if minutes < 60 * 24:
        return i18n.pick_now(f"{int(minutes // 60)} h ago", f"{int(minutes // 60)} 小时前")
    return i18n.pick_now(f"{int(minutes // (60 * 24))} d ago", f"{int(minutes // (60 * 24))} 天前")


def timeout_budget(cfg: dict, spec: dict) -> float:
    """How long this tool may run for.

    A spec may name its own setting (`timeout_key`): rendering a video takes minutes, while
    `tool_timeout` is sized for a call that answers quickly. Without the override the video tool
    would be killed halfway through a render the GPU has already done the work for.
    """
    key = spec.get("timeout_key")
    return float(cfg[key]) if key else float(cfg["tool_timeout"])


def _str_list(value: object) -> list[str]:
    """A string-array argument, tolerating the two shapes a model actually sends.

    A model asked for `["a", "b"]` sometimes hands over a single string instead, and refusing that
    would fail a call whose intent is not in doubt. Anything else is dropped rather than
    stringified: an object here means the parameter was misunderstood, and inventing a path out of
    it would send the wrong reference — which is worse than sending none.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []



class ToolHub:
    def __init__(self, store: Store, registry: ToolRegistry, mcp: McpManager, library: Library,
                 memory: MemoryService, hooks: Any = None, router: Any = None):
        self.store, self.registry, self.mcp, self.library, self.memory = store, registry, mcp, library, memory
        # Needed by the one tool that spends a model call of its own (`review_picture`). Optional so
        # every existing construction — including a test's — keeps working, and the tool then reports
        # that it cannot look rather than raising inside a member's turn.
        self.router = router
        # Hooks are asked twice around a tool call: once to object (`pre_tool_use`) and once to be
        # told what happened (`tool.called`). They are consulted *after* the user's own permission
        # rules, so a hook can only ever tighten a call, never grant one (see app/hooks.py).
        self.hooks = hooks

    # ----------------------------------------------------------- listing
    def _mcp_name(self, server: dict, tool: str, taken: set[str]) -> str:
        base = f"mcp__{slug(server['name'])}__{tool}"
        name = base if base not in taken else f"{base}_{server['id'][:4]}"
        taken.add(name)
        return name

    async def context(self, group: dict, agent: dict, connect: bool = True,
                      read_only: bool = False) -> ToolContext:
        cfg = self.store.get_settings()
        ctx = ToolContext(group, agent)
        if int(cfg["tool_rounds"]) <= 0:
            return ctx
        ext = group["ext"]

        # A media member is the generator itself, so it gets exactly one tool — its own — and gets
        # it on the account of the provider it was made from rather than the group's. Returning
        # early is the point: everything below hands a *conversational* member the tools of the
        # group, and none of that applies to a member whose whole turn is one call.
        if agent.get("origin") == media.MEDIA_ORIGIN:
            specs = builtin_specs()
            target = media.member_target(self.store, agent)
            if target is None:
                ctx.problems.append(i18n.pick_now(
                    f"\"{agent['name']}\" is a generating member, but the model it was made from is "
                    "gone (its provider or the model itself was deleted), so it cannot do anything. "
                    "Take it out of the group, or add the model back and make the member again.",
                    f"「{agent['name']}」是生成成员,但它所依据的模型已经不在了(服务商或模型被删除),"
                    "所以它什么也做不了。请把它移出群聊,或重新添加那个模型再建一次。"))
                return ctx
            ctx.media = {"provider_id": target["provider"]["id"], "model": target["model"]["model_name"],
                         "use": target["use"]}
            prov = target["provider"]
            if target["use"] == "video":
                name = "generate_video"
                gated = video.blocked_by_offline(prov, cfg)
                off = i18n.pick_now(
                    f"\"{agent['name']}\" generates video through \"{prov['name']}\", which is "
                    "switched off or has no address — so it cannot reply.",
                    f"「{agent['name']}」通过「{prov['name']}」生成视频,而那个服务商已停用或没填地址,"
                    "所以它无法回复。")
            else:
                name = "generate_image"
                gated = imagegen.blocked_by_offline(prov, cfg)
                off = i18n.pick_now(
                    f"\"{agent['name']}\" draws through \"{prov['name']}\", which is switched off or "
                    "has no address — so it cannot reply.",
                    f"「{agent['name']}」通过「{prov['name']}」绘画,而那个服务商已停用或没填地址,"
                    "所以它无法回复。")
            if gated or not (prov["enabled"] and (prov["base_url"] or "").strip()):
                # Same rule as for a conversational member: nobody is handed a tool that cannot
                # work, and the user is told why instead of watching it fail.
                ctx.problems.append(gated or off)
                return ctx
            spec = specs[name]
            ctx.tools[name] = {"name": name, "description": spec["description"],
                               "parameters": spec["parameters"], "risk": spec.get("risk"),
                               "timeout_key": spec.get("timeout_key"), "source": "builtin"}
            return ctx

        specs = builtin_specs()          # descriptions in the request language

        def add(name: str, spec: dict, **extra: Any) -> None:
            # `timeout_key` has to travel with the spec, because `timeout_budget` reads it off the
            # per-call dict and not off the catalogue: without this line a tool that declares its own
            # budget got the generic `tool_timeout` instead, and a video render was killed at 60s
            # while it was halfway through work the GPU had already done. The media-member branch
            # below copies it by hand, which is how the asymmetry went unnoticed.
            ctx.tools[name] = {"name": name, "description": spec["description"],
                               "parameters": spec["parameters"], "risk": spec.get("risk"),
                               "timeout_key": spec.get("timeout_key"), **extra}

        add("current_time", specs["current_time"], source="builtin")
        # "The library is not empty for this group" now means "at least one knowledge base is in
        # scope and has an enabled document", not "the database has documents".
        if self.library.scope_ids(ext["library"], group["id"]):
            add("library_search", specs["library_search"], source="builtin")
            add("library_read", specs["library_read"], source="builtin")
            # The pictures that came with those documents. Offered on the same condition as the
            # search, because it is the same knowledge base: being able to read a document and not
            # being able to reach its figures is exactly the gap that made groups generate fake
            # anatomy while a real angiogram sat one lookup away.
            add("list_figures", specs["list_figures"], source="builtin")
        # Offered whenever something in this app can look at a picture at all. This is the gap that
        # let a group argue for twenty turns about a film none of them had seen: every member had
        # already said, correctly, "I cannot see the picture" — and there was no tool that could. The
        # one thing a drawing or a render needs before it is signed off is somebody looking at it.
        if self._can_review():
            add("review_picture", specs["review_picture"], source="builtin")
        # Offered whenever the machine can measure a file, which is whenever ffprobe is here — the
        # transcription being absent is not a reason to withhold it, it is the thing the answer has to
        # say. A group that cannot hear a voice sample and does not know why is worse off than one that
        # is told "no transcriber, here is the command". (This line is *not* part of the block above:
        # it was folded into `if self._can_review()` for one commit while adding `study_video`, which
        # silently took `review_audio` away from every group on a machine with no vision model — and
        # the tests that exist to keep the "here is the command" answer alive caught it.)
        if ffmpeg.probe_path():
            add("review_audio", specs["review_audio"], source="builtin")
        # Offered whenever the machine can *measure* a file — the same line `review_audio` takes, and
        # for the same reason: what is missing beyond that (something that can look, `yt-dlp` for a
        # link) is not a reason to withhold it, because the answer is the thing that has to say so.
        # ⚠️ Not `ctx.problems`: those are posted into the conversation as a system message once per
        # turn, so a group on a machine without a vision model would get an extra line in the chat on
        # every single turn — and the wording `vision.reason_missing` gives ("An image is attached…")
        # is written for the attachment path and would be a false statement here. The tool's own
        # answers name the missing piece instead.
        ok, why = study.available()
        if ok:
            add("study_video", specs["study_video"], source="builtin")
        else:
            ctx.problems.append(why)
        # The study tool runs after the two review tools on purpose: it needs something that can look,
        # ffmpeg to read the file, and (for a link) `yt-dlp`. Each missing piece is named in the panel
        # rather than left as a tool that would fail on first use.        # Drawing a frame needs neither a library nor a provider — a schematic is drawn here and a
        # workspace picture is already on disk — so it is offered whenever Pillow can be imported,
        # which is always. Gating it on anything else would send a group back to generating anatomy.
        add("make_figure", specs["make_figure"], source="builtin")
        # The deliverable channel: without it a group can discuss a deck for forty turns and every
        # tool it has produces pictures, video or code — never the file the user opens. Offered on the
        # same line `make_figure` takes (it needs nothing but the libraries this app already ships),
        # and a format whose library is missing is named by the tool's own answer rather than by
        # `problems`, which is posted into the chat on every turn.
        ok, why = docwrite.available()
        if ok:
            add("write_document", specs["write_document"], source="builtin")
        # The process engineer's two instruments. `process_log` is offered to every member (it needs
        # nothing but the workspace, and any member that notices a defect should be able to write it
        # down — a log only one member can add to is a log that stays empty); `ask_advisor` only when
        # a command-line model is actually installed, because a member handed a tool that cannot run
        # reports a failure that looks like its own fault.
        # ⚠️ Neither appends to `ctx.problems`: that list is posted into the chat as a system message
        # on every turn, so a machine without codex/claude would get a line in the conversation every
        # single turn. Settings → General shows the missing piece instead, next to the field.
        add("process_log", specs["process_log"], source="builtin")
        adv_ok, _adv_why = advisor.available(cfg, folder=self.store.workspace_dir(ctx.group["id"]))
        if adv_ok:
            add("ask_advisor", specs["ask_advisor"], source="builtin")
        if cfg["memory_enabled"] and ext["memory"]:
            add("memory_search", specs["memory_search"], source="builtin")
            add("memory_save", specs["memory_save"], source="builtin")
        if cfg["code_enabled"]:
            add("run_code", specs["run_code"], source="builtin")
        # Assembling is offered on the same condition as every other tool: only when it can work.
        # It needs nothing but ffmpeg, which is why it is not behind a switch — a group that can
        # generate clips but cannot join them is the state this tool exists to end.
        ok, why = assemble.available()
        if ok:
            add("assemble_video", specs["assemble_video"], source="builtin")
            # Same condition, same reason: drawing a real animation needs nothing but ffmpeg, and a
            # group that can only make stills will make a slide show.
            add("make_animation", specs["make_animation"], source="builtin")
        else:
            ctx.problems.append(why)
        if cfg["video_enabled"]:
            # Offered only when there is somewhere to generate. A member handed the tool without a
            # reachable server would keep retrying and report a failure that looks like its own
            # fault; the user gets the actual reason instead.
            vprov, why = video.pick_provider(self.store, cfg)
            blocked = video.blocked_by_offline(vprov, cfg) if vprov else ""
            if why or blocked:
                ctx.problems.append(why or blocked)
            else:
                add("generate_video", specs["generate_video"], source="builtin")
        if cfg["image_enabled"]:
            # Same rule as video: a member is not handed a tool that cannot work, and the user is
            # told why it is missing instead.
            iprov, why = imagegen.pick_provider(self.store, cfg)
            blocked = imagegen.blocked_by_offline(iprov, cfg) if iprov else ""
            if why or blocked:
                ctx.problems.append(why or blocked)
            else:
                add("generate_image", specs["generate_image"], source="builtin")
        for t in self.registry.plugin_tools(ext["plugins"]):
            if t.name in ctx.tools or t.name in BUILTIN_TOOL_NAMES:   # a plugin cannot displace a built-in tool (permission checks go by name)
                ctx.problems.append(i18n.pick_now(f"The plugin tool \"{t.name}\" has the same name as a built-in tool, so it was ignored.", f"插件工具「{t.name}」和内置工具重名,已忽略。"))
                continue
            ctx.tools[t.name] = {**t.spec(), "source": "plugin"}
        taken = set(ctx.tools)
        for sid in ext["mcp"]:
            server = self.store.get_mcp(sid)
            if not server or not server["enabled"]:
                continue
            if pick_transport(server) != "stdio" and not cfg["external_calls_enabled"]:   # remote MCP counts as outbound communication too
                ctx.problems.append(i18n.pick_now(f"MCP \"{server['name']}\" is a remote service and outbound calls are switched off, so it was not used this time.", f"MCP「{server['name']}」是远程服务,而「允许外呼」是关的,本次没有使用。"))
                continue
            st = self.mcp.state(sid)
            if (not st or st.status != "ready") and connect:
                st = await self.mcp.connect(server, timeout=30)
            if not st or st.status != "ready":
                if not (st and st.error):                              # simply never connected yet, not an error
                    ctx.mcp_deferred = True
                ctx.problems.append(i18n.pick_now(f"MCP \"{server['name']}\" is not connected: {(st.error if st else '') or 'not connected yet'}", f"MCP「{server['name']}」未连接:{(st.error if st else '') or '尚未连接'}"))
                continue
            for t in st.tools:
                name = self._mcp_name(server, t["name"], taken)
                ctx.tools[name] = {"name": name, "description": f"[{server['name']}] {t['description']}",
                                   "parameters": t["parameters"], "source": "mcp", "server_id": sid,
                                   "tool": t["name"], "read_only": t.get("read_only", False)}
        if read_only:
            ctx.tools, withheld = read_only_tools(ctx.tools)
            if withheld:
                ctx.problems.append(i18n.pick_now(
                    f"This round came from WhatsApp, so only read-only tools are available — {len(withheld)} tool(s) were withheld.",
                    f"这一轮来自 WhatsApp,因此只开放只读工具——已停用 {len(withheld)} 个工具。"))
        return ctx

    # ----------------------------------------------------------- calls
    def policy(self, spec: dict) -> str:
        return policy_for(self.store.get_settings(), spec)

    async def call(
        self, ctx: ToolContext, name: str, args: dict[str, Any],
        approve: Callable[[dict, dict], Awaitable[bool]] | None = None,
    ) -> ToolOutcome:
        """approve: calls that need confirmation are handed to it to ask the user (True = allowed).
When it is not supplied, calls needing confirmation are always denied."""
        t0 = time.time()
        spec = ctx.tools.get(name)
        if not spec:
            return ToolOutcome(i18n.pick_now(f"There is no tool called {name} (or it is not enabled for this group). Available tools: {', '.join(ctx.tools) or 'none'}", f"没有名为 {name} 的工具(或本群未启用)。可用工具:{', '.join(ctx.tools) or '无'}"), False)
        req = (spec["parameters"] or {}).get("required") or []
        missing = [r for r in req if r not in args]
        if missing:
            return ToolOutcome(i18n.pick_now(f"Missing required arguments: {', '.join(missing)}", f"缺少必填参数:{', '.join(missing)}"), False)
        pol = self.policy(spec)
        if pol == "deny":
            return ToolOutcome(i18n.pick_now(f"Tool {name} is blocked by the user under Permissions & control, so it was not run. Do not retry — find another way, or tell the user directly.", f"工具 {name} 已被用户在「权限与操控」里禁止,没有执行。不要重试,请换个办法或直接告诉用户。"), False, 0, True)
        if pol == "ask" and not (approve and await approve(spec, args)):
            return ToolOutcome(
                i18n.pick_now(f"The user did not approve this call ({name}: denied, or no confirmation before the timeout), so it was not run. Do not retry the same operation — find another way, or tell the user what you need and why.", f"用户没有批准这次调用({name}:拒绝或超时未确认),没有执行。不要重试同一操作,请换个办法,或直接告诉用户你需要做什么、为什么。"),
                False, int((time.time() - t0) * 1000), True,
            )
        if pol == "ask" and self.policy(spec) == "deny":   # while waiting for confirmation the user changed it to "forbidden"
            return ToolOutcome(i18n.pick_now(f"Tool {name} is blocked by the user under Permissions & control, so it was not run.", f"工具 {name} 已被用户在「权限与操控」里禁止,没有执行。"), False, 0, True)
        if self.hooks is not None:
            blocked, args = await self.hooks.gate_tool(ctx.group["id"], spec, args)
            if blocked:
                return ToolOutcome(
                    i18n.pick_now(f"{blocked} Do not retry the same call — find another way, or tell the user what you need and why.",
                                  f"{blocked}不要重试同一调用,请换个办法,或直接告诉用户你需要做什么、为什么。"),
                    False, 0, True)
        t0 = time.time()  # elapsed time excludes the wait for user confirmation
        timeout = timeout_budget(self.store.get_settings(), spec)
        try:
            text, ok, files = await asyncio.wait_for(self._dispatch(ctx, spec, args, timeout), timeout + 5)
        except asyncio.TimeoutError:
            self._note_unstoppable(spec)
            text, ok, files = i18n.pick_now(f"Tool execution timed out ({int(timeout)} seconds)", f"工具执行超时({int(timeout)} 秒)"), False, []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            text, ok, files = i18n.pick_now(f"Tool execution failed: {type(e).__name__}: {e}", f"工具执行出错:{type(e).__name__}: {e}")[:500], False, []
        outcome = ToolOutcome(text, ok, int((time.time() - t0) * 1000), False, files)
        if self.hooks is not None:
            self.hooks.notify("tool.called", ctx.group["id"], {
                "agent": ctx.agent.get("name", ""), "tool": name, "source": spec.get("source"),
                "args": scrub_args(args), "ok": ok, "ms": outcome.ms,
                "text": scrub_text(text),
            })
        return outcome

    def _note_unstoppable(self, spec: dict) -> None:
        """A plugin tool that is a plain function runs in a worker thread (see `tools.py`), and a
        thread cannot be cancelled: the timeout above ends the *wait*, not the work, so the plugin may
        still be writing files or spawning processes. Nothing here can stop it — code that has to be
        killable belongs in a subprocess (`coderun`) — so at least leave a trace."""
        if spec.get("source") == "plugin":
            print(f"plugin tool {spec.get('name')!r} is still running in a worker thread after its "
                  f"timeout; the side effects of that call were not stopped")

    async def _dispatch(self, ctx: ToolContext, spec: dict, args: dict, timeout: float) -> tuple[str, bool, list[dict]]:
        """Always (text, ok, files): whether the tool produced files is not a special case."""
        src, name = spec["source"], spec["name"]
        if src == "mcp":
            text, ok = await self.mcp.call_tool(spec["server_id"], spec["tool"], args, timeout)
            return text, ok, []
        if src == "plugin":
            res = await self.registry.call(name, args)
            return (res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)), True, []
        if name == "generate_video":
            return await self._generate_video(ctx, args)
        if name == "generate_image":
            return await self._generate_image(ctx, args)
        if name == "assemble_video":
            return await self._assemble_video(ctx, args)
        if name == "make_animation":
            return await self._make_animation(ctx, args)
        if name == "make_figure":
            return await self._make_figure(ctx, args)
        if name == "write_document":
            return await self._write_document(ctx, args)
        if name == "process_log":
            return await self._process_log(ctx, args)
        if name == "ask_advisor":
            return await self._ask_advisor(ctx, args)
        if name == "review_picture":
            return await self._review_picture(ctx, args)
        if name == "study_video":
            return await self._study_video(ctx, args)
        if name == "review_audio":
            return await self._review_audio(ctx, args)
        text, ok = await self._builtin(ctx, name, args)
        return text, ok, []

    # ----------------------------------------------------------- video generation
    async def _generate_video(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Render one clip through the video provider, into this group's workspace.

        Every refusal here says what is actually wrong, because the alternative — handing the
        model a generic "failed" — makes it retry and then blame itself.
        """
        cfg = self.store.get_settings()
        # A media member's turn arrives here too: it *is* a generator, so the provider it was
        # created from outranks the group's setting, and its own model name outranks `video_model`.
        # Without this, "@Seedance" would quietly render on whatever was chosen last.
        member = ctx.media or {}
        prov, why = video.pick_provider(self.store, cfg, prefer=str(member.get("provider_id") or ""))
        if prov is None:
            return why, False, []
        blocked = video.blocked_by_offline(prov, cfg)
        if blocked:
            return blocked, False, []
        model = str(member.get("model") or cfg.get("video_model") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return i18n.pick_now("The prompt was empty, so nothing was generated.", "提示词是空的,没有生成。"), False, []
        # Which values are even legal depends on the provider, so the shape is read from it rather
        # than from one shared list: H3 has no `adaptive` and MetaChat's API has no `21:9`, and
        # refusing a value the server would have taken is its own kind of wrong answer.
        shape = video.shape_of(prov["kind"])
        ratio = str(args.get("aspect_ratio") or shape["default_ratio"]).strip()
        if ratio not in shape["ratios"]:
            return i18n.pick_now(
                f"\"{ratio}\" is not an aspect ratio \"{prov['name']}\" produces, so nothing was generated. Use one of: {', '.join(shape['ratios'])}.",
                f"「{ratio}」不是「{prov['name']}」支持的画幅,没有生成。可用:{', '.join(shape['ratios'])}。",
            ), False, []
        # The workspace is created here rather than assumed to exist: this is the first thing a
        # fresh group does with it.
        workspace = self.store.workspace_dir(ctx.group["id"])
        want_first = str(args.get("first_frame") or "").strip()
        want_last = str(args.get("last_frame") or "").strip()
        # Reference material beyond the two keyframes: only Ark takes any of it, so a member that
        # sends some to H3 or MetaChat is told which provider would have accepted it rather than
        # having it silently dropped — a generation that ignored the reference it was given comes
        # back looking like the model's fault.
        ref_images = _str_list(args.get("reference_images"))
        ref_videos = _str_list(args.get("reference_videos"))
        ref_audios = _str_list(args.get("reference_audios"))
        if (ref_images or ref_videos or ref_audios) and prov["kind"] != video.ARK_KIND:
            return i18n.pick_now(
                f"\"{prov['name']}\" does not take reference pictures, video or audio, so nothing "
                "was generated. Only Volcengine Ark (Doubao Seedance) accepts those — use "
                f"first_frame otherwise. Available here: {', '.join(k for k, v in (('first_frame', want_first), ('last_frame', want_last)) if v) or 'the prompt alone'}.",
                f"「{prov['name']}」不接受参考图/参考视频/参考音频,没有生成。只有火山方舟(Doubao Seedance)"
                "支持这些 —— 其他服务商请改用 first_frame。",
            ), False, []
        seconds, clamped = video.clamp_seconds(args.get("duration_seconds"), int(cfg["video_max_seconds"]), prov["kind"])
        pinned = False
        if prov["kind"] == video.META_KIND:
            if not model:
                return i18n.pick_now(
                    "Video generation is set to MetaChat's media API, which serves more than one "
                    "model, but none is chosen — so nothing was generated. Pick one under "
                    "Permissions & control → Video generation.",
                    "视频生成用的是 MetaChat 的媒体接口,它有两个模型,但没有选定用哪个,所以没有生成。"
                    "请在「权限与操控 → 视频生成」里选一个。",
                ), False, []
            if want_last:
                return i18n.pick_now(
                    f"\"{model}\" generates from a single reference image, so there is no last-frame "
                    "slot to put that in, and nothing was generated. Pass the picture as first_frame.",
                    f"「{model}」只按一张参考图生成,没有「结束帧」这个位置,所以没有生成。请把图片填在 first_frame。",
                ), False, []
            try:
                frame = video.image_url(want_first)
            except video.VideoError as e:
                return str(e), False, []
            if not frame:
                return i18n.pick_now(
                    f"\"{model}\" generates from a reference image and MetaChat's video API offers no "
                    "text-only mode, so nothing was generated. Pass the picture as first_frame, as an "
                    "http(s) URL the service can fetch.",
                    f"「{model}」要按参考图生成,MetaChat 的视频接口没有纯文生视频的模式,所以没有生成。"
                    "请把图片填在 first_frame,并用它取得到的 http(s) 地址。",
                ), False, []
            payload = video.metachat_payload(
                prompt, model=model, ratio=ratio, duration_seconds=seconds,
                short_edge=int(cfg["video_short_edge"]), frame=frame,
            )
        elif prov["kind"] == video.ARK_KIND:
            if not model:
                return i18n.pick_now(
                    "Ark serves several video models and none is chosen, so nothing was generated. "
                    "Pick one under Permissions & control → Video generation.",
                    "火山方舟有多个视频模型,但没有选定用哪个,所以没有生成。请在「权限与操控 → 视频生成」里选一个。",
                ), False, []
            try:
                assets = {
                    "first_frame": video.ark_asset(want_first, workspace, what="keyframe", what_zh="关键帧"),
                    "last_frame": video.ark_asset(want_last, workspace, what="keyframe", what_zh="关键帧"),
                    "reference_images": tuple(video.ark_asset(u, workspace, what="reference picture", what_zh="参考图")
                                              for u in ref_images),
                    "reference_videos": tuple(video.ark_asset(u, workspace, what="reference clip", what_zh="参考视频")
                                              for u in ref_videos),
                    "reference_audios": tuple(video.ark_asset(u, workspace, what="reference audio", what_zh="参考音频")
                                              for u in ref_audios),
                }
            except video.VideoError as e:
                return str(e), False, []
            # A keyframe task pins the ratio to `adaptive`, so the adjustment happens here and the
            # member is told, exactly as a clamped duration is.
            ratio, pinned = video.ark_ratio(ratio, keyframe=bool(assets["first_frame"] or assets["last_frame"]))
            payload = video.ark_payload(
                prompt, model=model, ratio=ratio, duration_seconds=seconds,
                short_edge=int(cfg["video_short_edge"]), seed=int(args.get("seed") or 0),
                audio=args.get("generate_audio") if isinstance(args.get("generate_audio"), bool) else None,
                **assets,
            )
        elif prov["kind"] == video.COMFY_KIND:
            wf = str(model or comfyui.DEFAULT_WORKFLOW).strip() or comfyui.DEFAULT_WORKFLOW
            uses = comfyui.wants(comfyui.workflow_of(wf))
            # What this graph can take is read **off the graph**, not assumed from the provider.
            # `{{image}}` in it means a `LoadImage` is waiting for a file; its absence means there is
            # genuinely nowhere to put one, and a member who asked for a keyframe and quietly did not
            # get one would draw the wrong conclusion about why its clip does not resemble the still
            # it started from.
            material = [("image", want_first, "a first frame", "首帧"),
                        ("last_image", want_last, "a last frame", "末帧")]
            if ref_audios:
                material.append(("audio", ref_audios[0], "a voice sample", "一段音色样本"))
            unusable = [(label, label_zh) for slot, value, label, label_zh in material
                        if value and slot not in uses]
            if unusable:
                takes = [slot for slot in ("image", "last_image", "audio") if slot in uses]
                needed = [slot for slot, value, _label, _zh in material if value and slot not in uses]
                return i18n.pick_now(
                    f"The \"{wf}\" workflow has nowhere to put "
                    f"{', '.join(label for label, _zh in unusable)}, so nothing was generated. It "
                    f"{'takes ' + ', '.join('{{%s}}' % s for s in takes) if takes else 'renders from the prompt alone'}"
                    f" — a workflow of your own that accepts it is a JSON file with "
                    f"{', '.join('{{%s}}' % s for s in needed)} in its graph.",
                    f"「{wf}」工作流没有放 {', '.join(zh for _label, zh in unusable)} 的位置,所以没有生成。"
                    f"它{'接受 ' + '、'.join('{{%s}}' % s for s in takes) if takes else '只按提示词生成'} —— "
                    f"要收它就得是你自己写的工作流文件,并在图里放上 "
                    f"{'、'.join('{{%s}}' % s for s in needed)}。",
                ), False, []
            given: dict[str, str] = {}
            wanted_files = [(slot, value, label, label_zh)
                            for slot, value, label, label_zh in material if value and slot in uses]
            if wanted_files:
                # A local file is *handed over* rather than referenced: the graph's `LoadImage` reads
                # a file on the instance's own disk, so the bytes have to be there before the job is
                # queued. One client for all of them, closed before the render starts its own.
                async with net.client(prov.get("base_url") or "",
                                      timeout=comfyui.UPLOAD_TIMEOUT) as c:
                    for slot, value, label, label_zh in wanted_files:
                        try:
                            uri = video.frame_uri(value, workspace)
                        except video.VideoError as e:
                            return str(e), False, []
                        if not uri.startswith("file://"):
                            return i18n.pick_now(
                                f"ComfyUI runs on this machine and reads its own disk, so {label} has "
                                f"to be a file in this group's workspace rather than a link — \"{value}\" "
                                "cannot be read from here. Put the file in the workspace and name it by "
                                "its path, or use a provider that fetches links itself (Volcengine Ark).",
                                f"ComfyUI 跑在这台机器上、读的是自己的磁盘,所以{label_zh}必须是本群工作目录里的"
                                f"文件而不是一个链接 —— 「{value}」在这里读不到。请把文件放进工作目录再按路径引用,"
                                "或者改用会自己去取链接的服务商(火山方舟)。",
                            ), False, []
                        blob = Path(uri[len("file://"):])
                        try:
                            data = await asyncio.to_thread(blob.read_bytes)
                        except OSError as e:
                            return i18n.pick_now(
                                f"Could not read \"{value}\": {e}", f"读不到「{value}」:{e}"), False, []
                        try:
                            given[slot] = await comfyui.upload(prov, data, blob.name, client=c)
                        except comfyui.VideoError as e:
                            return str(e), False, []
            payload = comfyui.payload_for(
                wf, prompt=prompt, ratio=ratio, seconds=seconds,
                short_edge=int(cfg["video_short_edge"]), seed=int(args.get("seed") or 0),
                **given,
            )
        else:
            try:
                first = video.frame_uri(want_first, workspace)
                last = video.frame_uri(want_last, workspace)
            except video.VideoError as e:
                return str(e), False, []
            payload = video.build_payload(
                prompt, short_edge=int(cfg["video_short_edge"]), aspect_ratio=ratio, duration_seconds=seconds,
                seed=int(args.get("seed") or 0), first_frame=first, last_frame=last,
            )
        # Which workflow actually ran, and whether it is the one that was asked for. Only a
        # ComfyUI payload carries these; every other kind is a service that takes a prompt, so
        # there is nothing to report and `.get` gives "".
        wf_name = str(payload.get("workflow") or "")
        wf_note = str(payload.get("note") or "")
        try:
            r = await video.generate(
                prov, payload, workspace=workspace,
                max_bytes=max(1, int(cfg["video_max_mb"])) * 1024 * 1024,
                deadline_s=float(cfg["video_timeout"]),
            )
        except video.VideoError as e:
            return str(e), False, []
        # KB below a megabyte: a short 768p clip really can be a few hundred KB, and "0.0 MB"
        # reads like something went wrong.
        size = (f"{r['bytes'] / 1024:.0f} KB" if r["bytes"] < 1024 * 1024
                else f"{r['bytes'] / 1024 / 1024:.1f} MB")
        # "with sound" is read off the provider's own shape rather than hardcoded: H3 renders stereo
        # audio and Ark defaults `generate_audio` to true, while MetaChat's two models document no
        # audio track at all — telling the user there is one would be inventing a feature.
        sound = i18n.pick_now(" with sound", "、带声音") if shape.get("audio") else ""
        # Measured, not assumed. The line below used to repeat the *request* back — "a 9:16 clip" —
        # whether or not that is what came out, and a provider is free to ignore a parameter: the
        # group's own history has four clips that came back 16:9 after a 9:16 request, reported as if
        # honoured, and nobody could tell until the film would not fit. The file is the fact.
        measured = await asyncio.to_thread(self._measure_clip, Path(r["path"]), ratio, seconds)
        real_ratio = measured.pop("ratio", "") if measured else ""
        # For a ComfyUI provider the workflow *is* the model, and now that a group can have several
        # of them, "which one ran" is part of the result rather than trivia — the whole point of the
        # registry is that the user picks one and can check that they got it.
        via = str(prov["name"])
        if wf_name and prov.get("kind") == comfyui.KIND:
            via = f"{via} / {wf_name}"
        lines = [
            i18n.pick_now(
                f"Rendered a {measured.get('seconds', seconds):g}s {real_ratio or ratio} clip{sound} "
                f"using {via}: {r['name']} ({size}, took {r['seconds']:.0f}s).",
                f"用 {via} 生成了一段 {measured.get('seconds', seconds):g} 秒、"
                f"{real_ratio or ratio} 的视频{sound}:{r['name']}({size},用了 {r['seconds']:.0f} 秒)。",
            ),
            i18n.pick_now(f"Saved in this group's workspace: {r['path']}", f"已保存在本群工作目录:{r['path']}"),
        ]
        # The workflow note goes above the measurement: "the one you asked for does not exist" is a
        # bigger thing to know than "the clip came out a different shape than requested".
        for warn in (measured.get("mismatch"), wf_note):
            if warn:
                lines.insert(0, warn)
        try:
            points = int(r.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        if points:
            lines.append(i18n.pick_now(
                f"(The service billed {points} points for it.)",
                f"(这次生成消耗了 {points} 元点。)",
            ))
        usage = r.get("usage") if isinstance(r.get("usage"), dict) else None
        cost = (usage or {}).get("total_tokens")
        if cost:
            lines.append(i18n.pick_now(
                f"(Ark billed {int(cost)} tokens for it.)", f"(这次生成消耗了 {int(cost)} tokens。)"
            ))
        if pinned:
            lines.append(i18n.pick_now(
                f"(Aspect ratio was set to {ratio}: on Ark a first/last frame fixes the output to "
                "the shape of that image.)",
                f"(画幅已设为 {ratio}:在方舟上,给了首帧/尾帧就由那张图的形状决定输出。)"
            ))
        if clamped:
            lo, hi = shape["seconds"]
            lines.append(i18n.pick_now(
                f"(Length was adjusted to {seconds}s: this group's limit is {int(cfg['video_max_seconds'])}s "
                f"and the model itself accepts {lo}-{hi}s.)",
                f"(时长已调整为 {seconds} 秒:本群上限是 {int(cfg['video_max_seconds'])} 秒,模型本身支持 "
                f"{lo}-{hi} 秒。)",
            ))
        # Without this line the model tends to narrate what "happened" in a video it never saw. A
        # media member is not that reader — its message goes straight to the user — so it is spared
        # an instruction addressed to somebody else.
        if not member:
            lines.append(i18n.pick_now(
                "You cannot watch or hear the result, so do not describe what happens in it — tell the "
                "user it is ready and where it is.",
                "你看不到也听不到生成结果,不要描述里面的内容 —— 只要告诉用户已经生成好了、文件在哪里。",
            ))
        return "\n".join(lines), True, [{"kind": "video", "name": r["name"], "bytes": r["bytes"], "seconds": seconds}]

    def _measure_clip(self, path: Path, ratio: str, seconds: float) -> dict:
        """What the clip actually is, plus a line for every way it differs from what was asked for.

        Two things only this can catch. A provider is free to ignore a parameter — this app's own
        history has clips that came back 16:9 after a 9:16 request — so measuring the file is the only
        way to know, and the number that was *requested* is the one thing that must never be reported
        as if it were the outcome. And a group that agreed on 9:16 for the whole film cannot use a
        landscape clip at all, so the mismatch has to be said the moment the clip exists rather than
        discovered when the film will not assemble.

        Runs in a worker thread (ffprobe is a subprocess) and never raises: a tool that cannot measure
        still has a file to hand over, and the answer then falls back to the requested values.
        """
        try:
            info = assemble.probe(path)
        except Exception:  # noqa: BLE001 — no ffprobe, or an unreadable file: not this tool's error
            return {}
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        got = float(info.get("seconds") or 0.0)
        real = ratio_label(w, h) if w and h else ""
        out: dict = {"seconds": round(got, 2) if got else seconds, "ratio": real}
        problems = []
        if real and ratio and real != ratio:
            problems.append(i18n.pick_now(
                f"Careful: {ratio} was asked for and {w}x{h} ({real}) came back. This clip is not the "
                "shape the rest of the film is in — do not build on it: regenerate, or settle the "
                "group's target size first.",
                f"注意:要的是 {ratio},量出来是 {w}×{h}({real})。这一条的形状和成片其余部分不一致 —— "
                "不要接着用:重新生成,或先把本群的目标画幅定下来。"))
        if got and seconds and abs(got - seconds) > max(1.0, seconds * 0.2):
            problems.append(i18n.pick_now(
                f"Careful: {seconds:g}s was asked for and the file is {got:.1f}s.",
                f"注意:要的是 {seconds:g} 秒,文件量出来是 {got:.1f} 秒。"))
        if problems:
            out["mismatch"] = "\n".join(problems)
        return out

    # ----------------------------------------------------------- image generation
    async def _generate_image(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Draw one image through the image provider, into this group's workspace.

        Every refusal names the thing to go and change: a member handed a generic "failed" will
        retry, and then blame itself for the service's answer.
        """
        cfg = self.store.get_settings()
        # Same rule as the video tool: when the caller is a media member, *its* provider and model
        # outrank the group's setting, so "@Seedream" draws with Seedream.
        member = ctx.media or {}
        prov, why = imagegen.pick_provider(self.store, cfg, prefer=str(member.get("provider_id") or ""))
        if prov is None:
            return why, False, []
        blocked = imagegen.blocked_by_offline(prov, cfg)
        if blocked:
            return blocked, False, []
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return i18n.pick_now("The prompt was empty, so nothing was drawn.",
                                 "提示词是空的,没有生成。"), False, []
        size = str(args.get("size") or cfg["image_size"] or imagegen.DEFAULT_SIZE).strip()
        if size not in imagegen.SIZES:
            return i18n.pick_now(
                f"\"{size}\" is not a size this service produces, so nothing was drawn. Use one of: {', '.join(imagegen.SIZES)}.",
                f"「{size}」不是这个服务支持的尺寸,没有生成。可用:{', '.join(imagegen.SIZES)}。",
            ), False, []
        workspace = self.store.workspace_dir(ctx.group["id"])
        model = str(member.get("model") or cfg["image_model"])
        if prov["kind"] == "metachat_media":
            # Fifteen models behind one key, and the request differs per model (which path, and
            # which parameters) — so an unset name is a request that would fail rather than draw.
            if not model.strip():
                return i18n.pick_now(
                    "Drawing is set to MetaChat's media API, which serves many models, but none is "
                    "chosen — so nothing was drawn. Pick one under Permissions & control → Image "
                    "generation.",
                    "绘画用的是 MetaChat 的媒体接口,它有很多模型,但没有选定用哪个,所以没有画。"
                    "请在「权限与操控 → 绘画」里选一个。",
                ), False, []
            payload = imagegen.metachat_payload(prompt, model=model, size=size)
        else:
            payload = imagegen.build_payload(prompt, model=model, size=size)
        try:
            got = await imagegen.generate(
                prov, payload,
                max_bytes=max(1, int(cfg["image_max_mb"])) * 1024 * 1024,
                deadline_s=float(cfg["image_timeout"]),
            )
            path = imagegen.save(got["data"], workspace, prompt)
        except imagegen.ImageError as e:
            return str(e), False, []
        lines = [
            i18n.pick_now(
                f"Drew a {size} image with {prov['name']} ({cfg['image_model']}): {path.name} "
                f"({media.size_label(got['size'])}, took {got['seconds']:.0f}s).",
                f"用 {prov['name']}({cfg['image_model']})画了一张 {size} 的图:{path.name}"
                f"({media.size_label(got['size'])},用了 {got['seconds']:.0f} 秒)。",
            ),
            i18n.pick_now(f"Saved in this group's workspace: {path}", f"已保存在本群工作目录:{path}"),
            # Without this line a model tends to describe the picture it never saw.
            i18n.pick_now(
                "You cannot see the result, so do not describe what is in it — tell the user it is "
                "ready and where it is.",
                "你看不到生成结果,不要描述画面内容 —— 只要告诉用户已经画好了、文件在哪里。",
            ),
        ]
        return "\n".join(lines), True, [{"kind": "image", "name": path.name, "bytes": got["size"]}]

    # ----------------------------------------------------------- teaching pictures
    # ----------------------------------------------------------- looking at the result
    def _can_review(self) -> bool:
        """Whether anything in this app can look at a picture right now.

        Asked before the tool is offered, not after it is called, for the same reason every other tool
        is: a member handed a tool that cannot work keeps trying, and reports a failure that reads like
        its own fault. No router (a test's hub) counts as "cannot".
        """
        if self.router is None:
            return False
        try:
            return bool(vision.pick(self.store, self.router.usable_models()))
        except Exception:  # noqa: BLE001 — an offer must never break a turn
            return False

    async def _review_picture(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Show one picture — or one frame of a video — to the model that can see, and say what it saw.

        Why this tool has to exist: the members that own the words (the writer, the storyboard artist,
        the proofreader) cannot see, and they said so, correctly, in a session that ran for twenty
        turns on a film none of them had ever looked at. Every complaint the user eventually raised —
        "the picture is fake", "that is not what blood looks like" — was a thing the team could not
        have noticed, because there was no way to notice it. This is the missing sense organ, not a
        convenience: without it the workflow has no checkpoint that reads the picture at all.

        A video is a picture too, at a moment: `at_seconds` picks the frame, which is how a claim like
        "the flow moves" gets looked at without watching the whole clip.
        """
        if self.router is None or not self._can_review():
            # The reason, named — the same sentence the attachments path uses. "Nobody can look" on its
            # own sends the reader to check a setting that may already be right.
            why = (vision.reason_missing(self.store, self.router) if self.router is not None else
                   i18n.pick_now("This app has no model router, so nothing can look at a picture.",
                                 "本程序没有可用的模型路由,所以没有东西能看图。"))
            return i18n.pick_now(
                f"The picture was not looked at: {why}",
                f"这张图没有被看:{why}"), False, []
        workspace = self.store.workspace_dir(ctx.group["id"])
        rel = str(args.get("path") or "").strip()
        try:
            target = assemble._local(workspace, rel, i18n.pick_now("No picture given.", "没有给图片。"))
        except assemble.AssembleError as e:
            return str(e), False, []
        if not target.is_file():
            return i18n.pick_now(
                f"There is no file at \"{rel}\" inside this group's workspace. Use the path an earlier "
                "tool reported, or list the workspace first.",
                f"本群工作目录里没有「{rel}」这个文件。请用前面工具报出来的路径,或先列一下工作目录。"), False, []

        cfg = self.store.get_settings()
        limit = int(float(cfg.get("vision_max_mb") or 8) * 1024 * 1024)
        cleanup: list[Path] = []
        try:
            got = await asyncio.to_thread(self._frame_of, target, args, limit, cleanup)
        except (figure.FigureError, OSError) as e:
            return i18n.pick_now(f"Could not read that file: {e}", f"读不到这个文件:{e}"), False, []
        if got is None:
            return i18n.pick_now(
                f"\"{rel}\" is not a picture or a video this app can read.",
                f"「{rel}」不是本程序能读的图片或视频。"), False, []
        mime, data, what = got
        if len(data) > limit:
            return i18n.pick_now(
                f"\"{rel}\" is bigger than the {cfg.get('vision_max_mb')} MB this app will send to a "
                "model, so it was not looked at. Ask for a smaller picture or a single frame.",
                f"「{rel}」超过了本程序允许发给模型的 {cfg.get('vision_max_mb')} MB,所以没有看。"
                "请换一张更小的图,或只要一帧。"), False, []
        ask = str(args.get("question") or "").strip()
        prompt = i18n.pick_now(
            "Describe what is actually in this picture, factually, for someone who cannot see it. "
            "If a question is given below, answer it directly and say plainly where you are unsure. "
            "Do not flatter it: name anything that looks wrong, flat, unfinished, anatomically "
            "implausible or unlike the real thing.\n" + (f"Question: {ask}" if ask else ""),
            "请如实描述这张图里实际有什么,写给一个看不见它的人。如果下面有问题,直接回答,"
            "并在不确定的地方明说。**不要客气**:凡是看起来不对、平淡、没做完、解剖上说不通、"
            "或不像真实影像的地方,都点出来。\n" + (f"要核的问题:{ask}" if ask else ""))
        try:
            seen = await vision.describe(self.store, self.router, [(mime, data)], prompt)
        finally:
            for p in cleanup:
                try:
                    p.unlink()
                except OSError:
                    pass
        if not seen:
            return vision.reason_missing(self.store, self.router), False, []
        model = vision.pick(self.store, self.router.usable_models()) or {}
        lines = [i18n.pick_now(
            f"Looked at {what} with \"{model.get('display_name') or model.get('model_name') or '?'}\":",
            f"用「{model.get('display_name') or model.get('model_name') or '?'}」看了{what}:")]
        lines.append(seen)
        lines.append(i18n.pick_now(
            "This is what the model reported. Where it names a fault, fix that and look again — a "
            "picture nobody has looked at is the one kind of error this app cannot catch for you.",
            "以上是那个模型报回来的。它指出的问题,改完再看一次 —— 没人看过的图,是本程序替你发现不了的那类错。"))
        return "\n".join(lines), True, []

    def _frame_of(self, target: Path, args: dict, limit: int,
                  cleanup: list[Path]) -> tuple[str, bytes, str] | None:
        """`(mime, bytes, what)`: the file's own bytes for a picture, one extracted frame for a video.

        Runs in a worker thread (`asyncio.to_thread`) because it may call ffmpeg, and a member's turn
        must not hold the event loop while a frame is decoded.

        A video is a picture at a moment, and asking for one frame is how a claim like "the flow moves
        down the vessel" gets looked at without watching the clip. The frame is written to a temporary
        file, never into the group's workspace: a review must not leave anything behind that a later
        `assemble_video` could pick up as a shot.
        """
        suffix = target.suffix.lower()
        if suffix in attachments.VIDEO_EXT:
            ok, why = ffmpeg.available()
            if not ok:
                raise figure.FigureError(why)
            probe = assemble.probe(target)
            at = args.get("at_seconds")
            at = float(probe.get("seconds") or 0) / 2 if at is None else max(0.0, float(at))
            fd, name = tempfile.mkstemp(prefix="ta-frame-", suffix=".png")
            os.close(fd)
            frame = Path(name)
            cleanup.append(frame)
            code, detail = ffmpeg.run([ffmpeg.path(), "-hide_banner", "-loglevel", "error", "-y",
                                       "-ss", str(at), "-i", str(target), "-frames:v", "1",
                                       str(frame)], 120)
            if code != 0 or not frame.is_file():
                raise figure.FigureError(i18n.pick_now(
                    f"Could not take a frame from the video: {detail[-200:]}",
                    f"取不到视频里的画面:{detail[-200:]}"))
            what = i18n.pick_now(f"the frame at {at:g}s of \"{target.name}\"",
                                 f"《{target.name}》第 {at:g} 秒那一帧")
            return "image/png", frame.read_bytes(), what
        if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else f"image/{suffix.lstrip('.')}"
            return mime, target.read_bytes(), i18n.pick_now(f"\"{target.name}\"", f"《{target.name}》")
        return None

    def _measure_sound(self, target: Path) -> tuple[float, bool]:
        """`(seconds, has any sound)` for any media file, measured with ffprobe.

        Deliberately not `assemble.probe`: that one is about "can this be a shot in a film" and refuses
        a file with no picture in it, which is exactly what an audio sample is. Returns `(0.0, False)`
        rather than raising, because "could not measure" and "there is no sound" both leave the caller
        with a sentence to write, not an exception to swallow.
        """
        if not ffmpeg.probe_path():
            return 0.0, False
        code, out = ffmpeg.run([ffmpeg.probe_path(), "-v", "error", "-print_format", "json",
                                "-show_format", "-show_streams", str(target)], 60)
        if code != 0:
            return 0.0, False
        try:
            data = json.loads(out)
        except ValueError:
            return 0.0, False
        try:
            seconds = float((data.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        sound = any(s.get("codec_type") == "audio" for s in (data.get("streams") or []))
        return seconds, sound

    async def _study_video(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Watch somebody else's video properly, and leave a brief the whole group can work from.

        The measurement comes first and from ffmpeg — length, frame size, frame rate, every cut and how
        far apart they are, how loud the sound is, what the speech says — and only then are frames shown
        to the model. A model asked to characterise a video unaided produces adjectives; the same model
        holding "42s, 9:16, 9 cuts at 1.8s median, captions burned in" produces a brief a storyboard can
        be built against. That ordering is the whole design of this tool.

        What it leaves behind is a document in the group's workspace. The knowledge base picks it up on
        its own, so a member that never saw this call can still find it — which is what "the team
        understands how the reference was made" has to mean in practice, rather than one agent holding
        it in its context.
        """
        if self.router is None or not self._can_review():
            # Named here rather than reusing `vision.reason_missing`, whose sentence begins "An image
            # is attached" — true on the attachment path, false here. The pointer at the end is the
            # same one, so the reader still ends up in the right place.
            why = i18n.pick_now(
                "no model here can look at pictures, and studying a reference means watching it",
                "这台机器上没有能看图的模型,而研究参考片就是要看它")
            return i18n.pick_now(
                f"The reference was not studied: {why}. Turn on cloud vision under Settings → General, "
                "or set up a local vision model — the same setting the vision model row reports.",
                f"参考视频没有研究:{why}。请在「设置 → 通用」里打开云端视觉,或配置一个本地视觉模型"
                "(和设置页「视觉模型」那一行说的是同一件事)。"), False, []
        ok, why = study.available()
        if not ok:
            return why, False, []

        gid = ctx.group["id"]
        workspace = self.store.workspace_dir(gid)
        url = str(args.get("url") or "").strip()
        rel = str(args.get("path") or "").strip()
        if not url and not rel:
            return i18n.pick_now("Give a link (`url`) or a file in this group's workspace (`path`).",
                                 "请给一个链接(`url`)或本群工作目录里的文件(`path`)。"), False, []
        note = str(args.get("note") or "").strip()

        # Bounded like every other long call here: a reference read is minutes of work, not an hour.
        try:
            want = int(args.get("frames") or study.FRAMES)
        except (TypeError, ValueError):
            want = study.FRAMES
        n = max(4, min(12, want))

        cleanup: list[Path] = []
        meta: dict = {}
        try:
            if url:
                if not study.downloader():
                    return (i18n.pick_now(
                        "This machine has no `yt-dlp`, so a link cannot be fetched. Install it "
                        "(`brew install yt-dlp`) or put a video file in the workspace and pass `path`.",
                        "这台机器没有 `yt-dlp`,取不了链接。装一个(`brew install yt-dlp`),"
                        "或把视频文件放进工作目录再用 `path`。")), False, []
                # Downloaded into the workspace on purpose: a reference is material for the group, not
                # a throwaway of this call, and the user should be able to watch what was studied.
                ref_dir = workspace / study.REF_DIR
                src, meta = await asyncio.to_thread(study.download, url, ref_dir)
            else:
                src = assemble._local(workspace, rel, i18n.pick_now("No video given.", "没有给视频。"))
                if not src.is_file():
                    return (i18n.pick_now(
                        f"There is no file at \"{rel}\" inside this group's workspace.",
                        f"本群工作目录里没有「{rel}」这个文件。")), False, []
        except (study.StudyError, assemble.AssembleError) as e:
            return str(e), False, []
        except OSError as e:
            return i18n.pick_now(f"Could not fetch or open that video: {e}",
                                 f"取不到或打不开这个视频:{e}"), False, []

        try:
            facts = await asyncio.to_thread(study.measure, src)
            window = min(facts["seconds"] or study.MAX_SECONDS, study.MAX_SECONDS)
            shots = await asyncio.to_thread(study.cuts, src, max_seconds=window)
            pics = await asyncio.to_thread(study.frames, src, n, seconds=window)
            said, speech_why = await asyncio.to_thread(study.speech, src)
        except (OSError, study.StudyError) as e:
            return i18n.pick_now(f"Could not read that video: {e}",
                                 f"读不了这个视频:{e}"), False, []

        if not pics:
            return (i18n.pick_now(f"No frames could be taken from \"{src.name}\".",
                                  f"从「{src.name}」里取不到画面。")), False, []

        look = study.look_call(self.store, self.router)
        title = str(meta.get("title") or src.stem)
        head = i18n.pick_now(
            f"Reference: {title}" + (f" (from {meta['uploader']})" if meta.get("uploader") else ""),
            f"参考片:{title}" + (f"(作者 {meta['uploader']})" if meta.get("uploader") else ""))
        brief = study.facts_text(facts, {"shots": shots, "read_seconds": window,
                                        "transcript": said, "speech_why": speech_why})
        if note:
            brief += i18n.pick_now(f"\nWhat matters to us: {note}", f"\n我们最关心:{note}")

        # Awaited, not threaded: `look` is a coroutine (see `study.look_call`). And the frames go in as
        # `(mime, bytes)` — the times travel in the prompt, not in the mime slot.
        shots = study.as_pictures(pics)
        times = study.frame_labels(pics) + "\n\n"
        try:
            looked = await look(shots, times + i18n.pick_now(study.LOOK_EN, study.LOOK_ZH))
            spec = await look(shots, times + i18n.pick_now(study.SPEC_EN, study.SPEC_ZH)
                              + "\n\n" + brief)
        except Exception as e:  # noqa: BLE001 — the model call is the one thing that can fail any way
            return i18n.pick_now(f"Could not look at the reference: {e}",
                                 f"看不了这个参考片:{e}"), False, []

        body = brief + "\n\n## " + i18n.pick_now("Per-frame notes", "逐帧观察") + "\n" + looked.strip() \
            + "\n\n" + spec.strip()
        try:
            doc = await asyncio.to_thread(study.write_spec, workspace, study.doc_name(title),
                                          title=title, url=url or str(meta.get("webpage_url") or ""),
                                          facts=facts, body=body)
        except OSError as e:
            return i18n.pick_now(f"Studied it, but the brief could not be saved: {e}",
                                 f"研究完了,但规格存不下来:{e}"), False, []

        # The knowledge base is what makes this a *team* understanding rather than one agent's memory.
        # The same sync the app runs before a turn (attachments + workspace documents), called here so
        # the brief is searchable *now* rather than from the next turn onward. Failure is not fatal: a
        # document in the workspace is still usable without an index entry.
        try:
            await asyncio.to_thread(self.library.sync_group_material, gid, workspace)
        except Exception as e:  # noqa: BLE001
            print("could not add the reference brief to the group's library:", e)

        rel_doc = doc.relative_to(workspace)
        lines = [
            i18n.pick_now(f"Studied \"{title}\" and wrote the brief to `{rel_doc}`.",
                          f"已研究「{title}」,规格写在 `{rel_doc}`。"),
            i18n.pick_now(
                f"Measured: {facts['seconds']}s, {facts['width']}x{facts['height']} ({facts['aspect']}), "
                f"{facts['fps']} fps, {len(shots)} cuts", 
                f"实测:{facts['seconds']} 秒、{facts['width']}x{facts['height']}({facts['aspect']})、"
                f"{facts['fps']} fps、{len(shots)} 次切点"),
            i18n.pick_now(
                f"It is in this group's knowledge base as \"{doc.stem}\", so every member can search it; "
                f"the source is at `{src.relative_to(workspace)}` if someone wants to watch it.",
                f"它已进本群知识库(标题「{doc.stem}」),每个成员都能搜到;成片参考在原片 "
                f"`{src.relative_to(workspace)}`。"),
        ]
        if speech_why:
            lines.append(i18n.pick_now(f"One thing was not read: {speech_why}",
                                       f"有一项没读到:{speech_why}"))
        files = [{"kind": "file", "name": str(rel_doc), "bytes": doc.stat().st_size, "seconds": 0.0}]
        return "\n".join(lines), True, files

    async def _review_audio(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Say what is in a recording, or say plainly that this machine cannot tell.

        The second half is the important half. A member cannot hear, so the choice is between a real
        transcript and an honest refusal — and a model asked to "check the voice sample" with neither
        will describe the recording anyway. So when there is no transcriber the answer names the gap
        and hands over the one command that closes it, which is also the only way the user learns that
        a whole class of verification was impossible.

        A video is accepted here as well as an audio file: "does the narration match the picture's
        timing" and "what does the sound track actually say" are the same question for a clip.
        """
        workspace = self.store.workspace_dir(ctx.group["id"])
        rel = str(args.get("path") or "").strip()
        try:
            target = assemble._local(workspace, rel, i18n.pick_now("No audio given.", "没有给音频。"))
        except assemble.AssembleError as e:
            return str(e), False, []
        if not target.is_file():
            return i18n.pick_now(
                f"There is no file at \"{rel}\" inside this group's workspace.",
                f"本群工作目录里没有「{rel}」这个文件。"), False, []

        cfg = self.store.get_settings()
        seconds, sound = await asyncio.to_thread(self._measure_sound, target)
        facts = i18n.pick_now(f"\"{target.name}\": {seconds:.1f}s of sound."
                              if sound else f"\"{target.name}\": {seconds:.1f}s, and no sound track in it.",
                              f"《{target.name}》:{seconds:.1f} 秒,有声音。"
                              if sound else f"《{target.name}》:{seconds:.1f} 秒,里面没有音轨。")
        if not sound and seconds > 0:
            return (facts + "\n" + i18n.pick_now(
                "There is nothing to hear in this file — it has no audio track at all. Anything said "
                "about what it sounds like would be invented.",
                "这个文件里没有可听的东西 —— 它根本没有音轨。任何关于它听起来怎样的说法都是编的。")), False, []

        command = attachments.transcriber(cfg)
        if not command:
            one = attachments.suggested_transcriber_install()
            return (facts + "\n" + i18n.pick_now(
                "This machine has no transcriber, so the recording was NOT read — do not guess what it "
                "says."
                + (f" One command installs one here: `{one}` (then the app notices it; nothing else to "
                   "set up). Tell the user that, and carry on with what does not depend on the sound."
                   if one else " Pull a transcriber (for example `uv tool install mlx-whisper`) and it "
                                "will be picked up automatically."),
                "这台机器上没有转写器,所以**没有**读这段录音 —— 不要猜它说了什么。"
                + (f"在本机装上只需要一条命令:`{one}`(装完本程序自己会认出来,不用再设别的)。"
                   "请把这句话转告用户,并继续做不依赖声音的部分。"
                   if one else "可以拉一个转写器(例如 `uv tool install mlx-whisper`),装好后会被自动识别。"))), False, []

        text, why = await asyncio.to_thread(attachments.transcribe_with_reason, target, cfg)
        if not text:
            return (facts + "\n" + i18n.pick_now(
                "The transcriber is installed but this recording was still not read"
                + (f": {why}." if why else ".")
                + " Do not guess what it says. A very common cause is the model download being "
                  "blocked (a proxy returning 502 while reaching huggingface.co); the app retries "
                  "through a mirror by itself, so if it still fails, downloading the model once by "
                  "hand — or pointing `HF_ENDPOINT` somewhere that works — is the fix.",
                "转写器装了,但这段录音仍然没读出来"
                + (f":{why}。" if why else "。")
                + "不要猜它说了什么。最常见的原因是**下载模型被挡住**(代理访问 huggingface.co 返回 502);"
                  "本程序会自己换镜像重试一次,若仍失败,手动把模型下一次,或把 `HF_ENDPOINT` "
                  "指到一个能用的地址,就能解决。")), False, []
        return (facts + "\n" + i18n.pick_now("What is said in it:\n", "里面说的是:\n") + text), True, []

    async def _make_figure(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Draw one teaching picture — from a real picture, a document's own figure, or a schematic.

        The whole point is the third possibility. A group making an explainer for a general audience
        does not need a photograph of an artery; it needs a drawing that reads at a glance, and a
        drawing done here is the one kind of picture that cannot be wrong in a way nobody can check:
        every shape in it was put there by somebody, from parameters that can be read back.
        """
        workspace = self.store.workspace_dir(ctx.group["id"])
        w, h, label = assemble.parse_size(str(args.get("size") or ""))
        chosen = [k for k in ("image", "doc", "schematic") if str(args.get(k) or "").strip()]
        if len(chosen) != 1:
            return i18n.pick_now(
                "Give exactly one source for the picture: `image` (a file in the workspace), `doc` + "
                "`figure` (a picture that came with a document), or `schematic` (draw one here). "
                f"You gave: {', '.join(chosen) or 'none'}.",
                "这幅图只能给一个来源:`image`(工作目录里的文件)、`doc` + `figure`(某篇文档自己带的图)、"
                f"或 `schematic`(在这里画一张)。你给的是:{', '.join(chosen) or '没给'}。"), False, []

        anchors = figure.schematic_geometry((w, h), args.get("params") or {}) \
            if "schematic" in chosen else {}

        marks = args.get("marks") or []
        if not isinstance(marks, list):
            return i18n.pick_now("`marks` has to be a list.", "`marks` 要写成列表。"), False, []
        credit = str(args.get("credit") or "").strip()
        source_path: Path | None = None
        draw = None
        try:
            if "schematic" in chosen:
                draw = figure.schematic(str(args["schematic"]).strip().lower(), (w, h),
                                        args.get("params") or {})
                stem = str(args.get("name") or f"schematic-{args['schematic']}")
                credit = credit or i18n.pick_now("Schematic drawn by this app.",
                                                 "本程序自绘示意图。")
            elif "doc" in chosen:
                doc = self.library.find_by_title(str(args["doc"]))
                if not doc:
                    return i18n.pick_now(
                        f"The library has no document called \"{args['doc']}\". Search the library first, "
                        "or list a document's figures by its exact title.",
                        f"资料库里没有《{args['doc']}》。请先检索,或用准确的标题查一篇文档的配图。"), False, []
                figs, meta = self.library.figures(doc)
                if not figs:
                    return i18n.pick_now(
                        f"\"{doc['title']}\" came with no pictures of its own — only words. So there is "
                        "nothing to show from it: pick another document, use a workspace picture, or "
                        "draw a schematic.",
                        f"《{doc['title']}》没有自带配图,只有文字。所以它这里没有可放的东西:换一篇文档、"
                        "用工作目录里的图,或者自绘一张示意图。"), False, []
                want = str(args.get("figure") or "").strip()
                fig = self.library.find_figure(doc, want) if want else (figs[0] if len(figs) == 1 else None)
                if fig is None:
                    names = "、".join(f"{i}) {f['name']}" for i, f in enumerate(figs[:12], 1))
                    more = i18n.pick_now(f" …and {len(figs) - 12} more", f"…等共 {len(figs)} 张") \
                        if len(figs) > 12 else ""
                    return i18n.pick_now(
                        f"Which figure of \"{doc['title']}\"? Say a name or a number: {names}{more}",
                        f"要《{doc['title']}》的哪一张?给名字或序号:{names}{more}"), False, []
                if fig["kind"] == "clip":
                    return i18n.pick_now(
                        f"\"{fig['name']}\" is a recording, not a still, so it cannot be made into one "
                        "picture. It is a real procedure recording — put it into `assemble_video` as a "
                        f"shot's `clip` instead (the file is {fig['path']}).",
                        f"「{fig['name']}」是录像而不是静图,做不成一张图。它是一段真实手术录像 —— 应该把它作为"
                        f"镜头的 `clip` 交给 `assemble_video`(文件在 {fig['path']})。"), False, []
                source_path = Path(fig["path"])
                stem = str(args.get("name") or source_path.stem)
                if not credit:
                    src = str(meta.get("source") or "").strip()
                    credit = i18n.pick_now(
                        f"From: {doc['title']}" + (f" · {src}" if src else "")
                        + " · terms not recorded — check before publishing",
                        f"取自《{doc['title']}》" + (f" · {src}" if src else "")
                        + " · 未记录使用条件,对外发布前需确认")
            else:
                rel = str(args["image"])
                source_path = assemble._local(workspace, rel, i18n.pick_now(
                    "No picture given.", "没有给图片。"))
                stem = str(args.get("name") or source_path.stem)
                if not credit:
                    credit = i18n.pick_now(f"From this group's workspace: {rel}",
                                           f"取自本群工作目录:{rel}")
        except (figure.FigureError, assemble.AssembleError) as e:
            return str(e), False, []
        except OSError as e:
            return i18n.pick_now(f"Could not read the picture: {e}", f"读不到这张图:{e}"), False, []

        try:
            out = figure.workspace_output(workspace, "figures", stem)
            await asyncio.to_thread(
                figure.compose, source_path, out, (w, h),
                heading=str(args.get("heading") or ""), caption=str(args.get("caption") or ""),
                credit=credit, draw=draw, body_fit=str(args.get("fit") or "full"), marks=marks)
        except figure.FigureError as e:
            return str(e), False, []
        except Exception as e:  # noqa: BLE001 — a frame that failed must read as a sentence
            return i18n.pick_now(f"Drawing the frame failed: {type(e).__name__}: {e}",
                                 f"绘制失败:{type(e).__name__}: {e}"), False, []

        what = (i18n.pick_now(f"a schematic ({args['schematic']})", f"一张示意图({args['schematic']})")
                if "schematic" in chosen else
                i18n.pick_now(f"a real picture ({source_path.name})", f"一张真实图片({source_path.name})")
                if source_path else i18n.pick_now("a picture", "一张图片"))
        lines = [i18n.pick_now(
            f"Made one {label} teaching picture from {what}: {out.name} ({media.size_label(out.stat().st_size)}).",
            f"用{what}做了一张 {label} 的教学图:{out.name}({media.size_label(out.stat().st_size)})。")]
        lines.append(i18n.pick_now(f"Saved in this group's workspace: {out}",
                                   f"已保存在本群工作目录:{out}"))
        if marks:
            lines.append(i18n.pick_now(
                f"With {len(marks)} mark(s) on it. Check each one lands on what you meant — a mark "
                "pointing at the wrong structure is worse than no mark.",
                f"图上有 {len(marks)} 处标注。请逐处确认它指的就是你想指的地方 —— 指错的标注比不标更糟。"))
        lines.append(i18n.pick_now(
            "This is a still: give it to `assemble_video` as a shot's `clip` (with `seconds`), and put "
            "the same credit line in that shot so the film can say where the picture came from. You "
            "cannot see the result, so do not describe it.",
            "这是一张静帧:把它当作镜头的 `clip` 交给 `assemble_video`(配 `seconds`),并把同样的来源行写进"
            "那一镜,成片才说得清画面从哪来。你看不到结果,不要描述它。"))
        if anchors:
            named = ", ".join(f"{k}={v[0]:.2f},{v[1]:.2f}" for k, v in anchors.items()
                              if not k.startswith("_"))
            lines.append(i18n.pick_now(
                f"Where its parts are, as fractions of the frame: {named}. Use these — with "
                "`{\"kind\": \"label\", \"at_part\": \"sac\"}` in a schematic's `params.labels`, or as "
                "`at` in `marks` — instead of estimating a position yourself. A mark placed from a "
                "guess is one that points somewhere else.",
                f"图上各部件的画面比例位置:{named}。请用它们 —— 示意图的 `params.labels` 里写 "
                "`{\"kind\": \"label\", \"at_part\": \"sac\"}`,或 `marks` 里当 `at` 用 —— "
                "不要自己估位置。估出来的标注指的一定是别的地方。"))
        return "\n".join(lines), True, [{"kind": "image", "name": out.name,
                                         "bytes": out.stat().st_size}]

    # ----------------------------------------------------------- documents
    async def _write_document(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Lay a finished draft out as a real file and hand it to the library.

        The whole point is that "the report is written" and "there is a report.docx" stop being two
        different things: the answer names a file that exists, with its size, and the file is indexed
        into this group's library in the same turn — so the member that has to review it can read it
        by title straight away, instead of being handed 3500 characters of truncated text.
        """
        workspace = self.store.workspace_dir(ctx.group["id"])
        try:
            dest, count = await asyncio.to_thread(
                docwrite.write, workspace, args.get("path"), args.get("format"),
                args.get("title") or "", args.get("body") or "")
        except docwrite.DocError as e:
            return str(e), False, []
        except Exception as e:  # noqa: BLE001 — a file that failed to write must read as a sentence
            return i18n.pick_now(f"Writing the file failed: {type(e).__name__}: {e}",
                                 f"写文件失败:{type(e).__name__}: {e}"), False, []
        rel = dest.relative_to(Path(workspace))
        size = dest.stat().st_size
        try:
            await asyncio.to_thread(self.library.sync_group_material, ctx.group["id"], workspace)
        except Exception:  # noqa: BLE001 — indexing is a convenience; the file is the deliverable
            pass
        what = {"docx": i18n.pick_now("a Word document", "一个 Word 文档"),
                "pptx": i18n.pick_now("a slide deck", "一份幻灯片"),
                "xlsx": i18n.pick_now("a workbook", "一个表格文件"),
                "md": i18n.pick_now("a text file", "一个文本文件")}[str(args.get("format")).lower().lstrip(".")]
        lines = [i18n.pick_now(
            f"Wrote {what} at {rel} ({media.size_label(size)}).",
            f"已写出{what}:{rel}({media.size_label(size)})。")]
        if str(args.get("format")).lower().lstrip(".") in ("docx", "pptx", "xlsx"):
            counted = {"docx": i18n.pick_now("blocks", "个段落/标题"),
                       "pptx": i18n.pick_now("content slides", "页内容页"),
                       "xlsx": i18n.pick_now("sheets", "张工作表")}[str(args.get("format")).lower().lstrip(".")]
            lines.append(i18n.pick_now(f"It holds {count} {counted}.", f"里面有 {count} {counted}。"))
        lines.append(i18n.pick_now(
            f"It is in this group's workspace and now searchable in its library by title — hand the "
            f"path to whoever checks it, and use `write_document` with the same name again to deliver "
            f"a revision (that replaces this file).",
            f"它在本群工作目录里,也已经可以用标题在知识库里搜到 —— 把路径交给验收的人;再交一稿就用"
            f"`write_document` 写同一个文件名(会覆盖这一份)。"))
        lines.append(i18n.pick_now(
            "You cannot open the file, so do not describe its layout. Report the path, the size and "
            "what you put in it.", "你打不开这个文件,不要描述版面。只报路径、大小和里面放了什么。"))
        return "\n".join(lines), True, [{"kind": "file", "name": str(rel), "bytes": size, "seconds": 0.0}]

    # ----------------------------------------------------------- watching the process
    async def _process_log(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """The process engineer's ledger: measure the run, record a defect, move it on, read it back.

        The reason this is a tool and not just a file the member writes: `scan` measures the run from
        the app's own record instead of from what the member remembers of the conversation, and the
        ledger's states are what stop "I changed the prompt" being filed as "the defect is gone".
        """
        action = str(args.get("action") or "").strip().lower()
        workspace = self.store.workspace_dir(ctx.group["id"])
        path = proclog.unit(workspace)
        if action == "scan":
            try:
                window = int(args.get("window") or 120)
            except (TypeError, ValueError):
                window = 120
            window = max(20, min(500, window))
            facts = await asyncio.to_thread(self._scan_facts, ctx.group, window)
            text = proclog.render_scan(facts)
            return text + "\n\n" + i18n.pick_now(
                "Everything above was measured by this app, not recalled. Write what is wrong with "
                "the *flow* as an entry (`action=report`), quoting one of these lines as the evidence.",
                "以上都是本程序量出来的,不是回忆。**流程本身**的毛病请用 `action=report` 记一条,"
                "依据就引用上面某一行。"), True, []
        try:
            entries = await asyncio.to_thread(proclog.read, path)
        except Exception as e:  # noqa: BLE001 — an unreadable ledger must read as a sentence
            return i18n.pick_now(f"The process log could not be read: {type(e).__name__}: {e}",
                                 f"读不了流程日志:{type(e).__name__}: {e}"), False, []
        if action == "report":
            if not str(args.get("title") or "").strip() or not str(args.get("symptom") or "").strip():
                return i18n.pick_now("A report needs at least `title` and `symptom`.",
                                     "记一条至少要给 `title` 和 `symptom`。"), False, []
            entry = proclog.report(
                entries, title=args.get("title"), symptom=args.get("symptom"),
                evidence=args.get("evidence") or "", severity=str(args.get("severity") or "minor"),
                stage=str(args.get("stage") or "other"), cause=args.get("cause") or "",
                fix=args.get("fix") or "")
            return await self._save_log(ctx, path, entries, workspace, [
                i18n.pick_now(f"Logged {entry.sentence()}", f"已记录 {entry.sentence()}"),
                i18n.pick_now(
                    f"{len(entries)} entries in the ledger now. When something is changed because of "
                    f"this, move it with `action=update` — and only mark it `verified` after running "
                    f"the same thing again and seeing the defect gone.",
                    f"账本现在有 {len(entries)} 条。针对它做了改动之后用 `action=update` 推进状态 —— "
                    f"而且要**重跑一次、确认问题消失**之后才标 `verified`。")])
        if action == "update":
            entry_id = str(args.get("id") or "").strip()
            if not entry_id:
                return i18n.pick_now("`update` needs the entry `id` (see `action=list`).",
                                     "`update` 要给条目 `id`(可以先 `action=list`)。"), False, []
            try:
                entry = proclog.update(
                    entries, entry_id, status=str(args.get("status") or ""),
                    note=str(args.get("note") or ""), verify=str(args.get("verify") or ""))
            except ValueError as e:
                return str(e), False, []
            if entry is None:
                have = ", ".join(e.id for e in entries) or i18n.pick_now("none", "无")
                return i18n.pick_now(f"There is no entry {entry_id}. The ledger holds: {have}",
                                     f"账本里没有 {entry_id} 这一条。现有的是:{have}"), False, []
            extra = []
            if entry.status == "verified" and not entry.verify:
                # Not a refusal — a `verified` with nothing behind it is the one thing this ledger
                # must not contain, so it is called out rather than silently accepted.
                extra.append(i18n.pick_now(
                    "Marked `verified` with no re-check recorded. Say what the re-run showed, or the "
                    "mark means nothing.", "标了 `verified` 却没有记录复核结果。请补上重跑看到了什么,"
                                          "否则这个标记没有意义。"))
            return await self._save_log(ctx, path, entries, workspace,
                                        [i18n.pick_now(f"Updated {entry.sentence()}",
                                                       f"已更新 {entry.sentence()}")] + extra)
        if action == "list":
            wanted = str(args.get("status_filter") or "").strip()
            # Default view is the unfinished business: what is open, and what was changed but never
            # re-checked. `verified` and `wontfix` are history, and asking for them is `status_filter`.
            keep = (lambda e: e.status in ("open", "fixed")) if not wanted else (lambda e: e.status == wanted)
            shown = [e for e in entries if keep(e)]
            lines = [i18n.pick_now(f"Process log: {len(entries)} entries in total; showing {len(shown)}.",
                                   f"流程日志:一共 {len(entries)} 条,这里显示 {len(shown)} 条。")]
            for e in shown:
                lines.append("  " + e.sentence())
                if e.evidence:
                    lines.append(i18n.pick_now(f"     evidence: {e.evidence[:200]}",
                                               f"     依据:{e.evidence[:200]}"))
                if e.status == "fixed":
                    lines.append(i18n.pick_now(
                        "     changed but never re-checked — run the same thing again, then `update` "
                        "it to verified (or back to open).",
                        "     改了但没复核过 —— 把同样的事再跑一次,再 `update` 成 verified(或退回 open)。"))
            if not entries:
                lines.append(i18n.pick_now(
                    "  (nothing yet — start with `action=scan`, then `report` what it shows.)",
                    "  (还没有记录 —— 先用 `action=scan`,再按它显示的记 `report`。)"))
            return "\n".join(lines), True, []
        return i18n.pick_now(
            f"`action` must be one of: scan, report, update, list (got {action or 'nothing'})",
            f"`action` 只能是:scan、report、update、list(收到的是 {action or '空'})"), False, []

    async def _save_log(self, ctx: ToolContext, path: Path, entries: list, workspace: Path,
                        notes: list[str]) -> tuple[str, bool, list[dict]]:
        """Write the ledger and put it in the group's library, exactly as a delivered document is.

        Indexing is what makes the log part of the group's memory rather than a text file nobody
        opens: the next round can `library_search` for a defect that was already written down.
        """
        try:
            await asyncio.to_thread(proclog.write, path, entries, ctx.group.get("name") or "")
        except Exception as e:  # noqa: BLE001 — a failed write must read as a sentence
            return i18n.pick_now(f"Writing the process log failed: {type(e).__name__}: {e}",
                                 f"写流程日志失败:{type(e).__name__}: {e}"), False, []
        rel = path.relative_to(Path(workspace))
        size = path.stat().st_size
        try:
            await asyncio.to_thread(self.library.sync_group_material, ctx.group["id"], workspace)
        except Exception:  # noqa: BLE001 — indexing is a convenience; the file is the record
            pass
        notes.append(i18n.pick_now(f"The ledger is at {rel} ({media.size_label(size)}).",
                                   f"账本在 {rel}({media.size_label(size)})。"))
        return "\n".join(notes), True, [{"kind": "file", "name": str(rel), "bytes": size, "seconds": 0.0}]

    def _scan_facts(self, group: dict, window: int) -> dict:
        """Everything the app itself recorded about the recent run — counted, not interpreted.

        Read by `proclog.render_scan`. Nothing here decides whether something is a defect: it
        reports "system note ×3", "write_document failed ×2", "交付/报告.docx is promised and not on
        disk", and leaves the judgement to the member that asked.
        """
        gid = group["id"]
        msgs = self.store.list_messages(gid, limit=window)
        kinds: dict[str, int] = {}
        speakers: dict[str, int] = {}
        calls = {"ok": 0, "failed": 0, "denied": 0, "other": 0}
        failed_by: dict[str, int] = {}
        notes: dict[str, int] = {}
        call_notes: list[str] = []
        tasks: list[dict] = []
        for m in msgs:
            kind = str(m.get("sender_type") or "?")
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "agent":
                name = str(m.get("sender_name") or "?")
                speakers[name] = speakers.get(name, 0) + 1
            elif kind == "system":
                text = " ".join(str(m.get("content") or "").split())[:160]
                if text:
                    notes[text] = notes.get(text, 0) + 1
            meta = m.get("meta") or {}
            for c in (meta.get("tools") or []):
                status = str(c.get("status") or "other")
                calls[status if status in calls else "other"] += 1
                if status in ("failed", "denied"):
                    name = str(c.get("name") or "?")
                    failed_by[name] = failed_by.get(name, 0) + 1
                    preview = " ".join(str(c.get("preview") or "").split())[:120]
                    line = f"{name}({status})" + (f": {preview}" if preview else "")
                    if line not in call_notes:
                        call_notes.append(line)
            if isinstance(meta.get("tasks"), list):
                tasks = meta["tasks"]
        members = [str(m["name"]) for m in self.store.group_members(gid)]
        silent = [n for n in members if n not in speakers]

        workspace = self.store.workspace_dir(gid)
        files: list[dict] = []
        names: set[str] = set()
        try:
            for p in sorted(workspace.rglob("*")):
                rel = p.relative_to(workspace)
                if not p.is_file() or any(part.startswith(".") for part in rel.parts):
                    continue
                stat = p.stat()
                files.append({"path": str(rel), "size": int(stat.st_size), "age": _age_label(stat.st_mtime)})
                names.add(p.name)
        except OSError:
            pass   # a workspace that cannot be walked is reported by its (empty) file list
        missing: list[str] = []
        for t in tasks:
            for name in proclog.FILE_IN_TEXT.findall(str(t.get("deliverable") or "")):
                if name and name not in names and name not in missing:
                    missing.append(name)
        log = proclog.read(proclog.unit(workspace))
        by_state: dict[str, int] = {}
        for e in log:
            by_state[e.status] = by_state.get(e.status, 0) + 1
        return {
            "window": len(msgs), "messages": kinds,
            "speakers": sorted(speakers.items(), key=lambda x: -x[1]), "silent": silent,
            "tool_calls": {**calls, "failed_by": sorted(failed_by.items(), key=lambda x: -x[1]),
                           "messages": call_notes},
            "tasks": [{"id": t.get("id"), "title": t.get("title"), "owner": t.get("owner"),
                       "status": t.get("status"), "error": t.get("error"),
                       "deliverable": t.get("deliverable")} for t in tasks],
            "files": files, "file_count": len(files), "older_files": max(0, len(files) - 12),
            "missing": missing,
            "system_notes": sorted(notes.items(), key=lambda x: -x[1]),
            "log": by_state,
        }

    async def _ask_advisor(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Put one narrow question to an outside model, read-only, in this group's workspace.

        The answer is passed through as it came, with the name of the program that gave it, and with
        a closing line that says what it is: a hypothesis from something that cannot see this chat.
        Anything else — paraphrasing it into the group's own voice, or trimming the caveats — would
        be the app inventing authority for somebody else's guess.
        """
        cfg = self.store.get_settings()
        question = str(args.get("question") or "").strip()
        if not question:
            return i18n.pick_now("There is no question to ask.", "没有问题可问。"), False, []
        workspace = self.store.workspace_dir(ctx.group["id"])
        ok, why = advisor.available(cfg, folder=workspace)
        if not ok:
            return why, False, []
        timeout = float(cfg.get("advisor_timeout") or 600)
        answer = await asyncio.to_thread(
            advisor.ask, cfg, question=question, folder=workspace,
            group=str(ctx.group.get("name") or ""), timeout=timeout,
            extra=str(args.get("evidence") or ""))
        if not answer.ok:
            return answer.text, False, []
        head = i18n.pick_now(
            f"{answer.label} answered in {int(answer.seconds)}s (read-only, in this group's "
            f"workspace):", f"{answer.label} 用了 {int(answer.seconds)} 秒回答(只读,在本群工作目录里):")
        tail = i18n.pick_now(
            "This came from a model outside this group: it cannot see this conversation, this app's "
            "settings or the other members, so treat each claim as a hypothesis to check before it "
            "is acted on — and quote it as the outside model's view, not as a fact.",
            "这条回答来自本群之外的模型:它看不到这段对话、本程序的设置,也看不到其他成员 —— "
            "每条说法都要先核对再动手,引用时写明是「外部模型认为」,不要说成结论。")
        text = f"{head}\n\n{answer.text}\n\n{tail}"
        clip = int(cfg["tool_output_limit"])
        if len(text) > clip:
            text = text[:clip] + i18n.pick_now(
                f"\n\n[cut at {clip} characters — the full answer was {len(answer.text)}]",
                f"\n\n[已截到 {clip} 字 —— 完整回答是 {len(answer.text)} 字]")
        return text, True, []

    # ----------------------------------------------------------- animation
    async def _make_animation(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Draw one real animation into the workspace: a clip, plus a poster frame.

        Offered to every member for the same reason `assemble_video` is — it needs nothing but ffmpeg,
        and a group that can only produce stills makes a slide show with narration over it. The poster
        frame is not a convenience: a reviewer can look at one frame and ask for a change, and the
        cheapest moment to change a drawing is before the other hundred frames exist.
        """
        ok, why = assemble.available()
        if not ok:
            return why, False, []
        cfg = self.store.get_settings()
        workspace = self.store.workspace_dir(ctx.group["id"])
        try:
            spec = animate.spec_of({"kind": args.get("kind"), **(args.get("params") or {})})
            seconds = max(2.0, min(float(args.get("seconds") or 5.0), animate.MAX_SECONDS))
            w, h, label = assemble.parse_size(str(args.get("size") or ""))
            clip = figure.workspace_output(workspace, "anim", str(args.get("name") or spec["kind"]),
                                           ".mp4")
            out = await animate.render(
                clip, spec, size=(w, h), seconds=seconds,
                heading=str(args.get("heading") or ""), credit=str(args.get("credit") or ""),
                still=clip.with_suffix(".png"),
                # Read from the setting rather than through `timeout_budget`: the budget belongs to the
                # call site, and this is the deadline ffmpeg itself is held to.
                timeout=float(cfg.get("assemble_timeout") or 1800),
            )
        except figure.FigureError as e:
            return str(e), False, []
        except Exception as e:  # noqa: BLE001 — a failed run must come back as a sentence
            return i18n.pick_now(f"Drawing the animation failed: {type(e).__name__}: {e}",
                                 f"画动画失败:{type(e).__name__}: {e}"), False, []

        size_bytes = Path(out["path"]).stat().st_size
        lines = [i18n.pick_now(
            f"Drew a {out['seconds']:g}s {label} animation — {out['kind']}, {out['frames']} frames "
            f"drawn here: {out['name']} ({media.size_label(size_bytes)}).",
            f"画了一段 {out['seconds']:g} 秒的 {label} 动画 —— {out['kind']},{out['frames']} 帧都是本机画的:"
            f"{out['name']}({media.size_label(size_bytes)})。")]
        lines.append(i18n.pick_now(
            f"Saved in this group's workspace: {out['path']}",
            f"已保存在本群工作目录:{out['path']}"))
        lines.append(i18n.pick_now(
            f"The frame kept for review is {Path(out['still']).name} beside it — that is what to look "
            "at, or to hand to somebody who can see, before this goes into a film.",
            f"旁边那张 {Path(out['still']).name} 是留给审阅的关键帧 —— 进成片之前,该看的是它,"
            "或者把它交给看得见的人。"))
        lines.append(i18n.pick_now(
            "Give this to `assemble_video` as that shot's `clip` (with `seconds`), or ask for the same "
            "shot as an `anim` there and the film draws it at the length the narration needs. You "
            "cannot watch the clip, so do not describe how it looks — say what it shows and how long "
            "it is.",
            "把它当作那一镜的 `clip` 交给 `assemble_video`(配好 `seconds`),或者干脆在 `assemble_video` 里"
            "直接给那一镜写 `anim`,成片会按旁白需要的长度自己画。你看不到这段动画,所以不要描述画面 —— "
            "只说它画的是什么、有多长。"))
        anchors = {k: v for k, v in figure.schematic_geometry((w, h),
                                                              (args.get("params") or {})).items()
                   if not k.startswith("_")}
        named = ", ".join(f"{k}={v[0]:.2f},{v[1]:.2f}" for k, v in anchors.items())
        lines.append(i18n.pick_now(
            f"Where its parts are, as fractions of the frame: {named}. Use these as `at` in `labels` "
            "instead of estimating a position — a label placed from a guess points somewhere else.",
            f"各部件在画面里的比例位置:{named}。`labels` 里的 `at` 请用它们,不要自己估 —— "
            "估出来的标注指的一定是别的地方。"))
        return "\n".join(lines), True, [{"kind": "video", "name": out["name"],
                                          "bytes": size_bytes, "seconds": out["seconds"]}]

    # ----------------------------------------------------------- assembly
    async def _assemble_video(self, ctx: ToolContext, args: dict) -> tuple[str, bool, list[dict]]:
        """Join the group's shots into one film, with narration and subtitles.

        The reason this is a tool and not advice: it is the only step of making a video that does
        not need a model. Left to members, every one of them writes its own ffmpeg command line, and
        they disagree about frame rate, aspect ratio and how the audio is joined — so the pieces
        cannot be put together afterwards, which is the state this replaces.

        It is generous about *when* it runs and strict about *what* it reports: the film is made
        whatever the shot list looks like, and what could not be done (a target length with nothing
        to stretch, a narration longer than its slot) is written into the answer and into the shot
        sheet rather than smoothed over.
        """
        ok, why = assemble.available()
        if not ok:
            return why, False, []
        cfg = self.store.get_settings()
        workspace = self.store.workspace_dir(ctx.group["id"])
        # The layout decides the look; an explicitly given argument still wins over it, because a
        # caller who named both meant both. Only `args` can tell the two apart — by the time this
        # reaches `render` every field has a value and the distinction is gone.
        try:
            style = layouts.style(str(args.get("layout") or ""))
        except ValueError as e:
            return str(e), False, []
        # The layout owns **where the words go**; these three own **what they say**. A layout may
        # carry a house line of its own (the unit's name, a standing disclaimer) and an argument
        # overrides it — but a layout is never allowed to invent a title, because the title of a
        # film is the one thing the caller always knows and the layout never does.
        style = dict(style)
        style["opening"] = {"title": str(args.get("title") or "") or style["opening"]["title"],
                            "subtitle": style["opening"]["subtitle"]}
        style["closing"] = {"title": str(args.get("closing") or "") or style["closing"]["title"],
                            "subtitle": style["closing"]["subtitle"]}
        style["credit"] = {**style["credit"],
                           "text": str(args.get("credit") or "") or style["credit"]["text"]}
        try:
            out = await assemble.render(
                workspace, args.get("shots"),
                size=str(args.get("size") or "") or str(style["size"] or ""),
                total=float(args.get("total_seconds") or 0),
                fps=int(args.get("fps") or style["fps"] or assemble.DEFAULT_FPS),
                voice=str(args.get("voice") or ""),
                burn=str(args.get("subtitles") or "burn") != "off",
                fit=str(args.get("fit") or "") or str(style["fit"] or assemble.DEFAULT_FIT),
                still=float(style["pace"]["still"] or assemble.STILL_SECONDS),
                name=str(args.get("name") or ""),
                style=style,
                # Read straight from the setting rather than through `timeout_budget`: the budget
                # belongs to the call site (`_dispatch` has already enforced it), and this is the
                # deadline ffmpeg itself is held to.
                timeout=float(cfg.get("assemble_timeout") or 1800),
            )
        except assemble.AssembleError as e:
            return str(e), False, []
        except Exception as e:  # noqa: BLE001 — a broken run must come back as a sentence, not a stack
            return i18n.pick_now(f"Assembling failed: {type(e).__name__}: {e}",
                                 f"装配失败:{type(e).__name__}: {e}"), False, []

        minutes = f"{int(out['seconds']) // 60}:" + f"{int(out['seconds']) % 60:02d}"
        lines = [i18n.pick_now(
            f"Assembled {out['shots']} shots into one {out['size']} film, {out['seconds']:g}s "
            f"({minutes}) at {out['fps']}fps: {out['name']} ({media.size_label(out['bytes'])}, "
            f"took {out['took']:g}s).",
            f"把 {out['shots']} 个镜头装配成一条 {out['size']} 的成片,共 {out['seconds']:g} 秒"
            f"({minutes}),{out['fps']}fps:{out['name']}({media.size_label(out['bytes'])},"
            f"用了 {out['took']:g} 秒)。")]
        # Said out loud because it is the difference between a film and a slide show, and because a
        # group that has just been told "every shot is a still" needs to know which lever to pull.
        drawn = out["animated"]
        real = sum(1 for e in out["plan"] if e["kind"] == "video")
        lines.append(i18n.pick_now(
            f"Shots that move: {drawn} drawn here, {real} from real footage"
            + (f", {out['shots'] - drawn - real} held still." if out["shots"] > drawn + real else "."),
            f"会动的镜头:{drawn} 个是本程序画的,{real} 个来自真实录像"
            + (f",{out['shots'] - drawn - real} 个是静帧撑住的。" if out["shots"] > drawn + real else "。")))
        lines.append(i18n.pick_now(
            f"Narration: {out['narrated']} shot(s). Words on screen: {out['subtitled']} shot(s), "
            + ("burned into the picture." if out["burned"] else "kept in the .srt only."),
            f"配音:{out['narrated']} 个镜头。字幕:{out['subtitled']} 个镜头,"
            + ("已烧进画面。" if out["burned"] else "只在 .srt 里。")))
        lines.append(i18n.pick_now(
            f"Saved in this group's workspace: {out['path']}", f"已保存在本群工作目录:{out['path']}"))
        # The three files are named because they are the handles for the next round: the sheet is
        # what the group edits, and the .srt is what a platform or an editor wants.
        lines.append(i18n.pick_now(
            f"Beside it: {Path(out['sheet']).name} (the shot list, edit this and assemble again) and "
            f"{Path(out['srt']).name} (the subtitles as a file).",
            f"旁边还有:{Path(out['sheet']).name}(分镜表,改它再装配一次)和 {Path(out['srt']).name}"
            "(字幕文件)。"))
        for n in out["notes"]:
            lines.append(f"· {n}")
        plan = out["plan"]
        # A picture in a film that cannot say where it came from is one nobody may publish, and the
        # question arrives after the work is done. So it is named now, not discovered later.
        uncredited = [str(e["no"]) for e in plan if e["kind"] != "card" and not e.get("credit")]
        if uncredited:
            lines.append(i18n.pick_now(
                f"({len(uncredited)} shot(s) have no recorded source for their picture: "
                f"{', '.join(uncredited)}. Give those shots a `credit` — where the picture came from "
                "and under what terms — or the film cannot be published. The sheet lists them.)",
                f"({len(uncredited)} 个镜头没记录画面来源:{', '.join(uncredited)}。请给这些镜头补 `credit`"
                "(画面从哪来、以什么条件使用),否则这条片子不能发布。分镜表里已列出。)"))
        stretched = plan[0].get("stretched") if plan else None
        if stretched:
            lines.append(i18n.pick_now(
                f"(To reach the length asked for, every card and still was held {stretched:g}s longer.)",
                f"(为了对上你要求的总时长,每张卡片和静帧都多停留了 {stretched:g} 秒。)"))
        short = plan[0].get("short") if plan else None
        if short:
            lines.append(i18n.pick_now(
                f"(This is {short:g}s short of the length asked for, and there was no still picture to "
                "hold longer — add shots rather than stretching a clip, which would change its speed.)",
                f"(比要求的总时长还差 {short:g} 秒,而没有静帧可以延长 —— 请增加镜头,不要拉长已有的片段,"
                "那会改变它的速度。)"))
        over = plan[0].get("over") if plan else None
        if over:
            lines.append(i18n.pick_now(
                f"(This runs {over:g}s longer than the length asked for. Nothing was cut to fit: say "
                "which shots to drop.)",
                f"(比要求的总时长长了 {over:g} 秒。没有为了凑时长删掉任何内容:请说明要减去哪几个镜头。)"))
        if plan:
            chosen = next((e.get("voice") for e in plan if e.get("voice")), "")
            if chosen:
                lines.append(i18n.pick_now(
                    f"(The narration was read by this machine's own \"{chosen}\" voice, so treat it as "
                    "a draft read-through: replace the audio with a recorded voice track once the cut "
                    "is right. Everything else here is final.)",
                    f"(旁白用本机语音「{chosen}」合成,当作一版小样试听:画面定稿后把音轨换成真人录音。"
                    "其余部分就是成品。)"))
        lines.append(i18n.pick_now(
            "You cannot watch or hear it, so do not describe what happens in it — say it is ready, "
            "how long it is, and where the files are.",
            "你看不到也听不到它,不要描述里面发生了什么 —— 只说已经做好、多长、文件在哪。"))
        return "\n".join(lines), True, [
            {"kind": "video", "name": out["name"], "bytes": out["bytes"], "seconds": out["seconds"]},
            {"kind": "file", "name": Path(out["srt"]).name, "bytes": 0, "seconds": 0},
        ]

    MEDIA_INDEX_TITLES = 24      # names listed when a search came back empty

    def _library_index(self, group: dict) -> str:
        """What this group can search, named — appended to a search that found nothing.

        A search that comes back empty used to be a dead end, and on the knowledge bases this app
        is most often pointed at (English atlases, papers, manuals) it is the *usual* end: BM25
        matches words, and a question asked in Chinese shares none with an English document. Naming
        what is in there turns that into a next step — search again with the subject's English name,
        or open the document that obviously is the one — instead of the member concluding the group
        has nothing and answering from memory.
        """
        docs = self.library.scope_docs(group["ext"]["library"], group["id"])
        if not docs:
            return i18n.pick_now("(This group can reach no knowledge base at all, so there is nothing to search.)",
                                 "(本群没有任何可检索的知识库,所以没有可搜的内容。)")
        names = "、".join(f"《{d['title']}》" for d in docs[: self.MEDIA_INDEX_TITLES])
        more = i18n.pick_now(f" …and {len(docs) - self.MEDIA_INDEX_TITLES} more",
                             f"…等共 {len(docs)} 份") if len(docs) > self.MEDIA_INDEX_TITLES else ""
        return i18n.pick_now(
            f"The {len(docs)} document(s) this group can search are: {names}{more}."
            " The knowledge base may be in another language than this conversation — if the words you"
            " used found nothing, put the subject's English term into the query and search again"
            " (mixing it into one query works better than translating the whole sentence:"
            " \\\"脑血管痉挛 angioplasty\\\"), or read one of these by title with library_read.",
            f"这个群能搜的 {len(docs)} 份文档是:{names}{more}。"
            "知识库的语言可能和这段对话不同 —— 如果刚才那组词什么都没命中,把主题的**英文术语混进"
            "同一条查询**再搜一次(整句翻译不如混着写:「脑血管痉挛 angioplasty」),"
            "或者直接用 library_read 按标题打开上面某一份。")

    async def _builtin(self, ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
        group, agent = ctx.group, ctx.agent
        if name == "current_time":
            return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %A (%z)"), True
        if name == "library_search":
            k = max(1, min(int(args.get("top_k") or self.store.get_settings()["library_top_k"]), 10))
            # The query vector is fetched here, on the async path, and handed to the synchronous
            # search: getting one is an HTTP call to the local model, and doing it inside `search`
            # would block a thread pool worker and — worse — block silently. When there is none
            # (the model is not running, or this build has no numpy) the search still runs on
            # keywords and the reader is told, because a mixture that quietly became keywords-only
            # reads exactly like a library that had nothing to say.
            qvec, note = await embed.query_vector(self.store.get_settings(), str(args["query"]))
            hits = await asyncio.to_thread(
                self.library.search, str(args["query"]), k,
                self.library.scope_ids(group["ext"]["library"], group["id"]), qvec
            )
            note = f"\n\n{note}" if note else ""
            if not hits:
                return i18n.pick_now("Nothing relevant was found in the library.", "资料库里没有找到相关内容。") \
                    + " " + self._library_index(group) + note, True
            return "\n\n".join(i18n.pick_now(f"[{h['title']} · passage {h['idx'] + 1}]\n{h['text'][:900]}", f"[《{h['title']}》第 {h['idx'] + 1} 段]\n{h['text'][:900]}") for h in hits) + note, True
        if name == "library_read":
            doc = self.library.find_by_title(str(args["doc"]))
            if not doc or not doc["enabled"]:
                return i18n.pick_now(f"The library has no document called {args['doc']}.", f"资料库里没有《{args['doc']}》。") \
                    + " " + self._library_index(group), False
            allowed = self.library.scope_ids(group["ext"]["library"], group["id"])
            if allowed is not None and doc["id"] not in allowed:
                return i18n.pick_now("This document is not enabled for this group.", "本群没有启用这份文档。"), False
            r = await asyncio.to_thread(self.library.read, doc["id"], max(0, int(args.get("start") or 0)), 3000)
            tail = i18n.pick_now(f"\n(read up to character {r['end']} of {r['total']}; pass start={r['end']} to carry on)", f"\n(已读到第 {r['end']} 字,共 {r['total']} 字;继续读请用 start={r['end']})") if r["end"] < r["total"] else ""
            return i18n.pick_now(f"{doc['title']}\n{r['text']}{tail}", f"《{doc['title']}》\n{r['text']}{tail}"), True
        if name == "list_figures":
            doc = self.library.find_by_title(str(args.get("doc") or ""))
            if not doc or not doc["enabled"]:
                return i18n.pick_now(f"The library has no document called {args.get('doc')}.",
                                     f"资料库里没有《{args.get('doc')}》。") + " " + self._library_index(group), False
            allowed = self.library.scope_ids(group["ext"]["library"], group["id"])
            if allowed is not None and doc["id"] not in allowed:
                return i18n.pick_now("This document is not enabled for this group.",
                                     "本群没有启用这份文档。"), False
            figs, meta = await asyncio.to_thread(self.library.figures, doc)
            origin = str(meta.get("source") or "").strip()
            head = i18n.pick_now(
                f"\"{doc['title']}\" came with {len(figs)} picture(s)/recording(s)"
                + (f", from {origin}" if origin else "") + ".",
                f"《{doc['title']}》自带 {len(figs)} 张图/段影像" + (f",来源 {origin}" if origin else "") + "。")
            if not figs:
                # The honest answer is also the useful one: it says what to do instead, and it names
                # the documents that *would* have something — a dead end that names an exit.
                have = []
                for d in self.library.scope_docs(group["ext"]["library"], group["id"])[:40]:
                    f2, _m = await asyncio.to_thread(self.library.figures, d)
                    if f2:
                        have.append(f"{d['title']}({len(f2)})")
                tail = i18n.pick_now(
                    " So there is nothing real to show from it — draw a schematic (`make_figure` with "
                    "`schematic`), or use a picture from the workspace.",
                    " 所以它这里没有可放的真实画面 —— 请自绘示意图(`make_figure` 给 `schematic`),"
                    "或用工作目录里的图片。")
                if have:
                    tail += i18n.pick_now(
                        " Documents that do have pictures: " + "、".join(have),
                        " 确实带图的文档:" + "、".join(have))
                return head + tail, True
            lines = []
            for i, f in enumerate(figs, 1):
                kind = i18n.pick_now("recording", "录像") if f["kind"] == "clip" \
                    else i18n.pick_now("picture", "图片")
                lines.append(f"{i}) {f['name']} — {kind}, {media.size_label(f['bytes'])}")
            body = "\n".join(lines)
            how = i18n.pick_now(
                "To use one: `make_figure` with this document's title as `doc` and the name or the "
                "number as `figure`. A recording is not a still — give it to `assemble_video` as a "
                "shot's `clip` instead. Whichever you use, the film should carry the credit line: "
                + (f"the recorded source is {origin}" if origin
                   else "this document records no source, so confirm the terms before publishing"),
                "要用某一张:`make_figure` 里把本文档标题填 `doc`,把名字或序号填 `figure`。"
                "录像不是静图 —— 应该作为镜头的 `clip` 交给 `assemble_video`。无论用哪个,成片都要带来源行:"
                + (f"记录下来的来源是 {origin}" if origin else "这篇文档没有记录来源,对外发布前须确认条件"))
            return f"{head}\n{body}\n\n{how}", True
        if name == "memory_search":
            mems = self.memory.recall(group["id"], agent["id"], str(args["query"]), 8)
            return (self.memory.block(mems) or i18n.pick_now("No relevant memories.", "没有相关记忆。")), True
        if name == "memory_save":
            content = str(args["content"]).strip()
            if not content or len(content) > 120:
                return i18n.pick_now("The content is empty, or longer than 120 characters.", "内容为空或超过 120 字。"), False
            if looks_sensitive(content):
                return i18n.pick_now("The content looks like a key, a password or a long digit string, so it was not saved.", "内容像是密钥/密码/长数字串,出于安全没有保存。"), False
            kind = args.get("kind") if args.get("kind") in ("preference", "fact", "decision", "lesson") else "fact"
            self.memory.save_manual(content, "group", group["id"], kind, "auto")
            return i18n.pick_now("Saved to this group's memory.", "已记入本群记忆。"), True
        if name == "run_code":
            cfg = self.store.get_settings()
            # Each group runs in its own workspace, so one project's files are never in reach of
            # another's code.
            workspace = self.store.workspace_dir(group["id"])
            cwd, why = coderun.resolve_cwd(workspace, str(args.get("cwd") or ""))
            if cwd is None:
                return why, False
            limit = max(500, int(cfg["tool_output_limit"]))
            return await coderun.run(
                str(args.get("language") or "python"), str(args.get("code") or ""),
                cwd, workspace, max(1.0, float(cfg["code_timeout"])), limit,
            )
        return i18n.pick_now(f"Built-in tool {name} is not implemented", f"未实现的内置工具 {name}"), False
