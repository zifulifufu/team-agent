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

from . import assemble, coderun, i18n, imagegen, media, video

import asyncio
import json
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
        "description": "Search the library for passages relevant to a question (returns document "
                       "titles and the original text). Reach for this first whenever you need to "
                       "cite a fact, a figure or a rule. The knowledge base may be in another "
                       "language than this conversation: if a search comes back empty, try the "
                       "subject's name in English before concluding nothing is there.",
        "description_zh": "在资料库里检索与问题相关的片段(返回文档标题和原文)。需要引用事实、数据、"
                          "规定时先用它。注意资料库的语言可能和这段对话不同:一次没命中就换用主题的"
                          "英文名再搜一次,别直接下结论说没有。",
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
                       "into the group's workspace. Three kinds of provider exist and they differ. A "
                       "self-hosted MiniMax H3 makes 4-15 second clips with sound and is prompted "
                       "as shots and then sound (\"[Shot 1] ... overall_soundscape: ... "
                       "non_diegetic_music: ...\"); a workspace image can be first_frame/last_frame. "
                       "MetaChat's video API (Grok Video, Midjourney Video) makes 1-15 second clips "
                       "and generates ONLY from a reference image, which must be an http(s) URL "
                       "because that service downloads it itself. Volcengine's Ark (Doubao Seedance "
                       "2.5) makes 4-30 second clips, has sound on by default, takes a workspace "
                       "file or a URL for every kind of reference (pictures, video, audio) and is "
                       "the only one that accepts reference_videos/reference_audios; its prompt "
                       "refers to the references by position (\"@图片1\", \"@视频1\"). Rendering "
                       "takes minutes. You cannot watch or hear the result: say what you asked for, "
                       "never describe what came out.",
        "description_zh": "用本群配置的视频服务商生成一段短视频,存进本群工作目录。有三类服务商,规矩不同:"
                          "自建的 MiniMax H3 生成 4-15 秒、带声音的片段,提示词要写成「先分镜、再说声音」"
                          "(如「[Shot 1] … overall_soundscape: … non_diegetic_music: …」),可以用工作目录里的图片"
                          "当 first_frame/last_frame;MetaChat 的视频接口(Grok Video、Midjourney Video)"
                          "生成 1-15 秒的片段,而且只能「按参考图生成」,参考图必须是 http(s) 地址——它自己去下载;"
                          "火山方舟(Doubao Seedance 2.5)生成 4-30 秒的片段,默认带声音,参考素材(图、视频、音频)"
                          "既可以是工作目录里的文件也可以是公网地址,而且是三者中唯一支持 reference_videos/"
                          "reference_audios 的;它的提示词用序号指代素材(「@图片1」「@视频1」)。"
                          "渲染需要等几分钟。你看不到也听不到结果:只说你要求了什么,绝不要描述生成出来的画面。",
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
                            "description": "Reference image to start from: a path inside the workspace on a self-hosted H3 server or on Ark, an http(s) URL on MetaChat's video API (which fetches it itself, and where it is required). On Ark a keyframe pins the aspect ratio to adaptive",
                            "description_zh": "起始参考图:自建 H3 或火山方舟可填工作目录内的路径;MetaChat 的视频接口必须是 http(s) 地址(它自己去取),而且是必填的。方舟上用了关键帧就会把画幅锁成 adaptive"},
            "last_frame": {"type": "string",
                           "description": "Image to end on, same forms as first_frame. Self-hosted H3 and Ark only — MetaChat's API generates from a single keyframe and refuses this",
                           "description_zh": "结束图片,写法同 first_frame。仅自建 H3 与火山方舟支持;MetaChat 的接口只按一张参考图生成,填了会被拒绝"},
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
            "seed": {"type": "integer", "description": "0 for a random result; self-hosted H3 and Ark only",
                     "description_zh": "填 0 表示随机;仅自建 H3 与火山方舟支持"}},
            "required": ["prompt"]},
    },
    "assemble_video": {
        "description": "Turn the shots this group already has into ONE film: the pieces are joined, "
                       "the narration is recorded, and the words are put on screen. This is the "
                       "step between \"a few clips\" and \"a video somebody can watch\", and it is "
                       "local and free — nothing is generated and nothing is billed. Give a shot "
                       "list in order; each entry is {\"clip\": \"video/x.mp4\", \"seconds\": 6, "
                       "\"say\": \"narration to record\", \"text\": \"words on screen\"}, or "
                       "{\"title\": \"...\", \"subtitle\": \"...\"} for a card with no footage. "
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
                          "{\"title\": \"...\", \"subtitle\": \"...\"}。图片也能当镜头(会缓慢推近,并按住"
                          "你给的时长)。不写时长时,镜头长度跟着旁白走 —— 话被截断比超时长更糟。产物是一个"
                          "烧好字幕的 .mp4,旁边还有 .srt 和一份分镜表,便于改一行重新装配。"
                          "你看不到也听不到结果:只说你装配了什么,不要描述画面。",
        # It runs `ffmpeg` and `say` on this machine, so it asks like the other runners do.
        "risk": "exec",
        # Its own budget: a three-minute film is thousands of frames, and `tool_timeout` is sized for
        # a call that answers quickly.
        "timeout_key": "assemble_timeout",
        "parameters": {"type": "object", "properties": {
            "shots": {"type": "array", "items": {"type": "object"}, "description":
                      "The shots, in order. Each: {clip: a path inside the workspace, seconds: how long, "
                      "say: narration to record, text: words on screen, audio: voice|clip|mix|silent, "
                      "motion: true|false for a still} — or {title, subtitle} for a card with no footage",
                      "description_zh":
                      "按顺序的镜头。每条形如 {clip: 工作目录内的路径, seconds: 时长, say: 要录的旁白, "
                      "text: 屏幕字幕, audio: voice|clip|mix|silent, motion: 静帧是否缓慢推近} —— "
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
                      "description": "The voice to narrate with. Left out, one is chosen by the language "
                                     "on screen (Tingting for Chinese). The result names the voice used",
                      "description_zh": "配旁白用的语音。留空则按屏幕上的语言挑一个(中文用 Tingting)。"
                                        "结果里会写明用的是哪个"},
            "subtitles": {"type": "string", "enum": ["burn", "off"],
                          "description": "burn (default) puts the words into the picture; off leaves them "
                                         "in the .srt only",
                          "description_zh": "burn(默认)把字烧进画面;off 只留 .srt 文件"},
            "name": {"type": "string", "description": "What to call the film; left out, it is stamped with the time",
                     "description_zh": "成片的名字;留空则用时间戳"}},
            "required": ["shots"]},
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
                 memory: MemoryService, hooks: Any = None):
        self.store, self.registry, self.mcp, self.library, self.memory = store, registry, mcp, library, memory
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
        lines = [
            i18n.pick_now(
                f"Rendered a {seconds}s {ratio} clip{sound} using {prov['name']}: {r['name']} "
                f"({size}, took {r['seconds']:.0f}s).",
                f"用 {prov['name']} 生成了一段 {seconds} 秒、{ratio} 的视频{sound}:{r['name']}"
                f"({size},用了 {r['seconds']:.0f} 秒)。",
            ),
            i18n.pick_now(f"Saved in this group's workspace: {r['path']}", f"已保存在本群工作目录:{r['path']}"),
        ]
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
        try:
            out = await assemble.render(
                workspace, args.get("shots"),
                size=str(args.get("size") or ""),
                total=float(args.get("total_seconds") or 0),
                fps=int(args.get("fps") or assemble.DEFAULT_FPS),
                voice=str(args.get("voice") or ""),
                burn=str(args.get("subtitles") or "burn") != "off",
                fit=str(args.get("fit") or assemble.DEFAULT_FIT),
                name=str(args.get("name") or ""),
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
            " used found nothing, search again with the subject's English name, or read one of these"
            " by title with library_read.",
            f"这个群能搜的 {len(docs)} 份文档是:{names}{more}。"
            "知识库的语言可能和这段对话不同 —— 如果刚才那组词什么都没命中,换用主题的英文名再搜一次,"
            "或者直接用 library_read 按标题打开上面某一份。")

    async def _builtin(self, ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
        group, agent = ctx.group, ctx.agent
        if name == "current_time":
            return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %A (%z)"), True
        if name == "library_search":
            k = max(1, min(int(args.get("top_k") or self.store.get_settings()["library_top_k"]), 10))
            # BM25 scoring is pure CPU work, and the first search over a large library also rebuilds the
# index; run it in a thread pool so it cannot block the event loop (which would slow down
# everyone's streaming output)
            hits = await asyncio.to_thread(
                self.library.search, str(args["query"]), k, self.library.scope_ids(group["ext"]["library"], group["id"])
            )
            if not hits:
                return i18n.pick_now("Nothing relevant was found in the library.", "资料库里没有找到相关内容。") \
                    + " " + self._library_index(group), True
            return "\n\n".join(i18n.pick_now(f"[{h['title']} · passage {h['idx'] + 1}]\n{h['text'][:900]}", f"[《{h['title']}》第 {h['idx'] + 1} 段]\n{h['text'][:900]}") for h in hits), True
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
