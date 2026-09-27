"""Zones — the places in the sidebar where something is **made**.

Everything else in the sidebar is about *configuring* this app: skills, plugins, hooks, MCP,
agents and local tools, chat channels. Those are set up once and then kept an eye on, and they
moved out of the sidebar into the user panel (2026-09-27) for exactly that reason. What is left
is the other kind of thing: a **zone**, which is a place you go to make something — pictures, a
film, a written piece, a story — and where the pieces you made are kept.

What a zone *is*, concretely, is these four things and nothing else, because those are the four
the user named:

  library      what this zone keeps — its own material, and what it produced
  templates    the starting points you pick from, so a piece begins from something and not blank
  workflows    the pipelines that can run here, **with the truth about whether they can**
  roles        the division of labour: which seat gets which tools and which skills

The last one is the part that was scattered across four screens before: a seat's tools come from
`toolhub`, its skills from the skills folder, and the roster from a group template. A zone states
all three together, so "who does what here, with what" is answerable in one look.

⚠️⚠️ **`state` is a promise, and this file is the only place that makes it.** `ready` means the
zone can be used now; `planned` means it is declared and the page says what is missing. Filling a
planned zone in by writing plausible-looking content under it is how a person ends up clicking a
button that does nothing — so every item carries a state of its own (`ready` / `partial` /
`blocked` / `planned`) and `blocked` items say *what* is blocking them.

⚠️ The workflow states here are **measurements, not probes**. Probing ComfyUI or a cloud account
on every page open would make opening a page cost a network round trip that can hang, and the
answer would still be stale a minute later. Each entry therefore records what was found when it
was measured, and the zone that owns a live indicator (the video zone's composer) shows the live
one where it matters. Where a status can be read cheaply — "is a local ComfyUI configured at
all" — it is read live, and the note says which of the two it is.

Zones come in two layers, the same shape as layouts, voices, music and workflows:

  * the ones this app ships, in `BUILTIN` below;
  * a folder of the user's own, `<data dir>/zones/*.json`, one zone per file, for a zone that
    needs no new engine — a fixed library, some templates, a roster. A zone that needs a new
    *generator* cannot be a JSON file, and that is a fact about generators rather than a rule
    invented here: the file format deliberately describes only what can be described.

The three rules that govern that folder are the ones `layouts.py` already established, and they
are all about the same failure — a zone that is quietly not the one you asked for:

  * a file that cannot be used is **named with its reason**, never skipped;
  * a bad value is an **error**, not a default;
  * a file whose name collides with a shipped zone is **refused**, because silently shadowing a
    shipped zone would change a page the user did not touch.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import i18n

__all__ = ["BUILTIN", "SURFACES", "ITEM_STATES", "ZONE_SUFFIX", "MAX_BYTES",
           "use_folder", "reload", "available", "row", "errors", "detail", "catalog"]

ZONE_SUFFIX = ".json"
# A zone is a page of declarations. A quarter of a megabyte is a mistake, not a big zone.
MAX_BYTES = 256_000

# The four surfaces a zone declares, in the order they are drawn. Fixed rather than free-form: a
# fifth key in a file would be a section nothing ever shows.
SURFACES = ("library", "templates", "workflows", "roles")

# Which of them the zone page **draws**. Three, not four, and `roles` is the one left out — the
# user's call (2026-09-27): 在视频专区里面的各个面板里不需要放分工、工具与技能.
#
# It is not a gap and not an oversight, so it is written down rather than left to be rediscovered:
# `roles` is still declared, still validated, and still what a group started from this zone would
# get — it is simply not a thing that belongs on the page. The block was the same four-row shape on
# all four zones while the workbench above it already showed what the zone can actually do, so on
# the one zone that works it read as a second answer to a question nobody had asked. If it comes
# back, it comes back as a *roster* attached to starting a group, not as a section here.
DRAWN = ("library", "templates", "workflows")

# What an item can be. `blocked` is separate from `planned` on purpose: "not built yet" and
# "built, but something on this machine is in the way" have different next steps, and one word
# for both would send a person to the wrong one.
ITEM_STATES = ("ready", "partial", "blocked", "planned")

# The icons the sidebar can draw. A whitelist rather than "any name the file likes", because an
# unknown name would render as a fallback icon and nobody would ever find out.
ICONS = ("Clapperboard", "PenLine", "BookOpen", "Palette", "Sparkles", "Compass", "Layers", "Film")


def _text(value: object, field: str, *, required: bool = False) -> str:
    got = str(value or "").strip()
    if required and not got:
        raise ValueError(i18n.pick_now(f"`{field}` cannot be empty", f"`{field}` 不能是空的"))
    return got


def _pair(body: dict, key: str, what: str) -> tuple[str, str]:
    """An English/Chinese pair. English is required; Chinese falls back to it rather than to "".

    Falling back is deliberate here and only here: a user-written zone in one language is still a
    usable zone, while an empty label is a row with no name.
    """
    en = _text(body.get(key), key, required=True)
    zh = _text(body.get(key + "_zh"), key + "_zh") or en
    return en, zh


def _item(raw: object, surface: str, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(i18n.pick_now(
            f"`{surface}[{index}]` must be an object",
            f"`{surface}[{index}]` 必须是一个对象"))
    label, label_zh = _pair(raw, "label", surface)
    state = _text(raw.get("state"), "state").lower() or "planned"
    if state not in ITEM_STATES:
        raise ValueError(i18n.pick_now(
            f"`{surface}[{index}].state` is \"{state}\"; use one of: {', '.join(ITEM_STATES)}",
            f"`{surface}[{index}].state` 是「{state}」;可用:{'、'.join(ITEM_STATES)}"))
    out = {
        "id": _text(raw.get("id"), "id") or f"{surface}-{index + 1}",
        "label": label, "label_zh": label_zh,
        "note": _text(raw.get("note"), "note"), "note_zh": _text(raw.get("note_zh"), "note_zh"),
        "state": state,
    }
    if surface == "roles":
        out["member"] = _text(raw.get("member"), "member")
        out["in_template"] = bool(raw.get("in_template"))
        out["skills"] = [_text(s, "skills") for s in (raw.get("skills") or []) if _text(s, "skills")]
        out["tools"] = [_text(t, "tools") for t in (raw.get("tools") or []) if _text(t, "tools")]
    return out


def _surface(raw: object, name: str) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(i18n.pick_now(f"`{name}` must be an object", f"`{name}` 必须是一个对象"))
    items = raw.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError(i18n.pick_now(f"`{name}.items` must be a list",
                                       f"`{name}.items` 必须是一个列表"))
    return {
        "note": _text(raw.get("note"), name + ".note"),
        "note_zh": _text(raw.get("note_zh"), name + ".note_zh"),
        "items": [_item(x, name, i) for i, x in enumerate(items)],
    }


def _zone(body: dict, *, zid: str) -> dict:
    """One zone, checked. Raises `ValueError` whose text is the whole diagnosis."""
    name, name_zh = _pair(body, "name", "name")
    blurb, blurb_zh = _pair(body, "blurb", "blurb")
    icon = _text(body.get("icon"), "icon") or "Sparkles"
    if icon not in ICONS:
        raise ValueError(i18n.pick_now(
            f"`icon` is \"{icon}\"; this app can draw: {', '.join(ICONS)}",
            f"`icon` 是「{icon}」;本程序能画的有:{'、'.join(ICONS)}"))
    state = _text(body.get("state"), "state").lower() or "planned"
    if state not in ("ready", "planned"):
        raise ValueError(i18n.pick_now(
            f"`state` is \"{state}\"; a zone is either ready or planned",
            f"`state` 是「{state}」;一个专区要么 ready 要么 planned"))
    out = {
        "id": zid,
        "name": name, "name_zh": name_zh,
        "blurb": blurb, "blurb_zh": blurb_zh,
        "icon": icon, "state": state,
        # Which group template starts a team here. Empty means this zone has no roster yet, and
        # the page says so instead of offering a button onto nothing.
        "template": _text(body.get("template"), "template"),
    }
    for s in SURFACES:
        out[s] = _surface(body.get(s), s)
    return out


# --------------------------------------------------------------------------- what we ship

BUILTIN: dict[str, dict] = {}


def _add(zid: str, body: dict) -> None:
    BUILTIN[zid] = _zone(body, zid=zid)


# ---- 视频专区 -----------------------------------------------------------------------------
_add("video", {
    "name": "Video zone", "name_zh": "视频专区", "icon": "Clapperboard", "state": "ready",
    "blurb": "Make the pieces a film is built from — pictures, narration, music, captions — here, "
             "by hand, and keep them. A group chat uses the same engines through its tools.",
    "blurb_zh": "把一条片子需要的零件在这里做出来 —— 画面、旁白、音乐、字幕 —— 由你自己做、自己留着。"
                "群聊里的成员通过各自的工具用的是同一批引擎。",
    "template": "video",
    "library": {
        "note": "Kept on this machine. The material you upload yourself stays private: not in any "
                "group's workspace, not in a knowledge base, not in an export.",
        "note_zh": "都存在本机。你自己上传的素材是私有的:不进任何群的工作目录、不进知识库、不参与导出。",
        "items": [
            {"id": "music-shelf", "label": "Music shelf", "label_zh": "音乐库", "state": "ready",
             "note": "Every track composed here, by you or by a member. A film names one and is "
                     "scored with it.",
             "note_zh": "这里作过的每一首曲子,你作的或成员作的。片子点它的名字就能拿去配乐。"},
            {"id": "my-material", "label": "My material", "label_zh": "我的素材", "state": "ready",
             "note": "Photos, footage and recordings of your own that the other blocks generate "
                     "from — the digital human starts with a photo of you.",
             "note_zh": "你自己的照片、录像与录音 —— 另外几块都是从它们生成的,数字人就是从你的一张照片开始的。"},
        ],
    },
    "templates": {
        "note": "Pick one and the faders are set for you. They are the same six the composer "
                "offers — this is the index of them, not a second list.",
        "note_zh": "挑一个,推子就替你摆好了。它们就是作曲台上那六个 —— 这里是它们的索引,不是第二份列表。",
        "items": [],   # 由 `detail()` 从 `musicprompt.PRESETS` 实时填,避免清单写两份
    },
    "workflows": {
        "note": "What can actually run on this machine today. The ones that cannot say what is in "
                "the way — read that line before promising a shot in a plan.",
        "note_zh": "这台机器今天真正能跑的东西。跑不了的点名说明卡在哪 —— 在计划里承诺一个镜头之前,先读那一行。",
        "items": [
            {"id": "mechanism", "label": "Mechanism animation", "label_zh": "机制动画（推荐）",
             "state": "ready",
             "note": "Drawn frame by frame on this machine: 6 s at 1080x1920 in about two seconds, "
                     "any aspect. Flow, aneurysm growth, coil packing, catheter advance, contrast. "
                     "This is what a mechanism shot should use.",
             "note_zh": "本机逐帧画出来的:6 秒 1080×1920 约两秒,画幅随你要。血流、瘤囊鼓出、弹簧圈填塞、"
                        "微导管推进、造影显影。机制镜头就该用它。"},
            {"id": "music", "label": "Music (ACE-Step)", "label_zh": "音乐（ACE-Step）",
             "state": "ready",
             "note": "Composed on this machine's ComfyUI from style tags. Minutes per track, not "
                     "seconds — the shelf shows a live readiness check.",
             "note_zh": "在本机 ComfyUI 上按风格标签作曲。一首要几分钟不是几秒 —— 曲库那边有实时的就绪检查。"},
            {"id": "motion", "label": "Motion from a still", "label_zh": "静图转动作",
             "state": "partial",
             "note": "wan2.2 ti2v 5B is on disk and the graph exists. Silent, and slow: tens of "
                     "minutes per clip. Not lip-synced — narration is added separately.",
             "note_zh": "wan2.2 ti2v 5B 在盘上,工作流也在。无声,而且慢:一条几十分钟。"
                        "**不对口型** —— 旁白是另外叠上去的。"},
            {"id": "lipsync", "label": "Presenter lip sync (InfiniteTalk)", "label_zh": "口播口型（InfiniteTalk）",
             "state": "blocked",
             "note": "All the weights are in place — the 14B base, the patch, the Chinese audio "
                     "encoder — and the one thing missing is the node pack "
                     "`ComfyUI-WanVideoWrapper`. Nothing else is.",
             "note_zh": "权重全齐（14B 底座、补丁、中文音频编码器）,唯独缺一个节点包"
                        "`ComfyUI-WanVideoWrapper`。除此以外不缺东西。"},
            {"id": "scene-stills", "label": "Scene stills (text to image)", "label_zh": "场景静帧（文生图）",
             "state": "blocked",
             "note": "The cloud service is configured with fifteen image models and its account is "
                     "out of credit — its own words: \"You exceeded your current API quota. Please "
                     "purchase the API points.\" The local ComfyUI has no image checkpoint.",
             "note_zh": "云端服务商配好了、有十五个图像模型,但账号额度用尽 —— 它自己的原话:"
                        "「You exceeded your current API quota. Please purchase the API points.」。"
                        "本机 ComfyUI 没有图像模型。"},
            {"id": "html-renderers", "label": "HTML-driven renderers", "label_zh": "HTML 渲染链",
             "state": "blocked",
             "note": "Remotion, HyperFrames and video-shotcraft need an HTML project file, and "
                     "nothing on this machine writes `.html`. Do not put them in a plan: they run, "
                     "waste the round and produce nothing.",
             "note_zh": "Remotion、HyperFrames、video-shotcraft 都要一个 HTML 工程文件,"
                        "而本机**没有任何工具能写 `.html`**。别把它们写进计划:它们会跑、会白耗一轮、什么都不产出。"},
        ],
    },
    "roles": {
        "note": "Who sits where, and what that seat gets. in template means the zone's group "
                "template already has that seat; the others are seats to add when the work needs "
                "them.",
        "note_zh": "谁坐哪个位子、那个位子拿到什么。标着「模板里有」的,是本专区群模板已经带的位子;"
                   "其余的等活需要时再加。",
        "items": [
            {"id": "producer", "label": "Producer", "label_zh": "制片", "member": "Aide",
             "in_template": True, "skills": [],
             "tools": ["list_workspace_files", "find_team_resources", "invite_team_resource"],
             "note": "Sees the workspace, hands the work out, keeps the board honest.",
             "note_zh": "看工作目录、派活、盯着任务板不说假话。"},
            {"id": "writer", "label": "Script and narration", "label_zh": "脚本与旁白",
             "member": "Copywriter", "in_template": True,
             "skills": ["Deliver a file, not a monologue"],
             "tools": ["write_document", "synthesize_speech", "review_audio"],
             "note": "Writes the script, then records it in the cloned voice. Hears the result; "
                     "the tool says outright that nobody else can.",
             "note_zh": "写脚本,然后用克隆音色录出来。它听得到结果 —— 工具里明说了别人听不到。"},
            {"id": "storyboard", "label": "Storyboard and pictures", "label_zh": "分镜与画面",
             "member": "Storyboard", "in_template": True,
             "skills": ["Short video storyboards", "Work from a reference instead of from memory"],
             "tools": ["make_figure", "make_animation", "review_picture"],
             "note": "Looks at the device reference library before drawing, and draws mechanisms "
                     "frame by frame rather than stills.",
             "note_zh": "动手画之前先看器械参考图库,机制用逐帧画而不是静图。"},
            {"id": "reviewer", "label": "Review", "label_zh": "审校", "member": "Proofreader",
             "in_template": True,
             "skills": ["Check the result before you sign it off"],
             # Looking is what a reviewer does; it holds no tool that writes.
             "tools": ["review_picture", "review_audio", "list_workspace_files"],
             "note": "Opens the actual frames and listens to the actual audio, and reports what is "
                     "wrong with them.",
             "note_zh": "打开真实的帧、听真实的音轨,并把不对的地方报出来。"},
            {"id": "editor", "label": "Assembly", "label_zh": "剪辑与成片", "member": "Editor",
             "in_template": False,
             "skills": ["Long-form video: from shots to a finished film"],
             "tools": ["assemble_video", "make_music"],
             "note": "Turns shots into one film: subtitles, the music bed, the credit line.",
             "note_zh": "把镜头拼成一条片子:字幕、配乐、来源行。"},
            {"id": "librarian", "label": "Reference", "label_zh": "资料", "member": "Librarian",
             "in_template": False, "skills": ["Work from a reference instead of from memory"],
             "tools": ["library_search", "library_read", "search_literature"],
             "note": "Finds the paper, the guideline, the picture that the shot has to agree with.",
             "note_zh": "把镜头必须对得上的那篇文献、那份指南、那张图找出来。"},
        ],
    },
})

# ---- 写作专区 -----------------------------------------------------------------------------
_add("writing", {
    "name": "Writing zone", "name_zh": "写作专区", "icon": "PenLine", "state": "planned",
    "blurb": "Long pieces and finished documents: the material, the outline, the draft, the check "
             "— and one document at the end that can be handed over as it is.",
    "blurb_zh": "长文与成稿:素材、提纲、初稿、核对 —— 最后交出一份能直接拿去用的文档。",
    "template": "report",
    "library": {
        "note": "Where the drafts and the source material for one piece live. Not built yet: the "
                "knowledge base holds documents today, but a piece's own drafts have no shelf.",
        "note_zh": "一篇东西的草稿和它的材料放在哪。还没建:今天文档进的是知识库,而一篇东西自己的草稿没有可放的地方。",
        "items": [
            {"id": "drafts", "label": "Drafts", "label_zh": "草稿", "state": "planned",
             "note": "Each piece keeps its versions, so a paragraph that was cut is still there.",
             "note_zh": "每篇留着自己的版本,删掉的一段还找得回来。"},
            {"id": "sources", "label": "Sources", "label_zh": "材料", "state": "planned",
             "note": "The papers, data and pictures the piece cites, kept beside it.",
             "note_zh": "这篇引用的文献、数据和图,就存在它旁边。"},
        ],
    },
    "templates": {
        "note": "The group templates that already fit this zone — they exist today and can be "
                "started from the home page.",
        "note_zh": "已经合用的群模板 —— 今天就能用,从首页起手。",
        "items": [
            {"id": "report", "label": "Research report", "label_zh": "研究报告", "state": "ready",
             "note": "Host, researcher, librarian, analyst, proofreader.",
             "note_zh": "主持、研究、资料、分析、校对。"},
            {"id": "proposal", "label": "Proposal writing", "label_zh": "方案撰写", "state": "ready",
             "note": "Host, planner, copywriter, risk, editor.",
             "note_zh": "主持、方案、文案、风险、编辑。"},
            {"id": "office", "label": "Office documents", "label_zh": "办公文档", "state": "ready",
             "note": "Notices, reports, proposals — the shorter sibling.",
             "note_zh": "通知、汇报、方案 —— 这是短的那一支。"},
            {"id": "outline", "label": "Outline first", "label_zh": "先出提纲", "state": "planned",
             "note": "A starting point that is an outline rather than a blank page: sections, what "
                     "each one has to establish, and the material each one needs.",
             "note_zh": "起手不是空白页而是一份提纲:分几节、每节要立住什么、每节要哪些材料。"},
        ],
    },
    "workflows": {
        "note": "Writing itself works today — a member writes a document and it lands in the "
                "workspace. What has no pipeline is turning a set of documents into one piece.",
        "note_zh": "写这件事今天就能做 —— 成员写文档,落在工作目录里。没有的是「把一堆文档收成一篇」这条流水线。",
        "items": [
            {"id": "document", "label": "Write a document", "label_zh": "写文档", "state": "ready",
             "note": "write_document, section by section — a reply longer than one model output "
                     "is dropped whole, so long pieces are written in parts.",
             "note_zh": "write_document,一节一节写 —— 比模型输出上限还长的一整段会被整个丢掉,"
                        "所以长文必须分节写。"},
            {"id": "assemble", "label": "Collect into one piece", "label_zh": "收成一篇",
             "state": "planned",
             "note": "Take the sections and the sources a group produced and produce one document "
                     "with a consistent voice and one reference list.",
             "note_zh": "把一群成员产出的分节和材料收成一份文档:口径统一、参考文献只有一份。"},
            {"id": "cite", "label": "Check every citation", "label_zh": "逐条核对引用",
             "state": "planned",
             "note": "Every claim traced back to the source that is actually on disk.",
             "note_zh": "每一条说法都追回到盘上真实存在的那份来源。"},
        ],
    },
    "roles": {
        "note": "Seats for long-form writing. in template = already in the zone's group template.",
        "note_zh": "长文写作的位子。标着「模板里有」的 = 本专区的群模板已经带了。",
        "items": [
            {"id": "host", "label": "Host", "label_zh": "主持", "member": "Aide",
             "in_template": True, "skills": [],
             "tools": ["list_workspace_files", "find_team_resources"], "note": "", "note_zh": ""},
            {"id": "planner", "label": "Outline and argument", "label_zh": "提纲与论证",
             "member": "Planner", "in_template": False,
             "skills": ["Research report structure"],
             "tools": ["write_document", "library_search"],
             "note": "Decides what the piece has to establish, in what order, before anyone writes.",
             "note_zh": "在有人动笔之前,先定这篇要立住什么、按什么顺序。"},
            {"id": "researcher", "label": "Drafting", "label_zh": "撰稿", "member": "Researcher",
             "in_template": True,
             "skills": ["Relay writing rules", "Office writing conventions"],
             "tools": ["write_document", "library_search", "library_read"],
             "note": "Writes the sections, in parts, against the outline.",
             "note_zh": "按提纲分节把稿子写出来。"},
            {"id": "librarian", "label": "Sources", "label_zh": "资料", "member": "Librarian",
             "in_template": True,
             "skills": ["Research findings write-up"],
             "tools": ["library_search", "library_read", "search_literature"],
             "note": "Finds the material and says where it came from.",
             "note_zh": "把材料找出来,并说清它是从哪来的。"},
            {"id": "proofreader", "label": "Language and facts", "label_zh": "语言与事实",
             "member": "Proofreader", "in_template": True,
             "skills": ["Check the result before you sign it off"],
             "tools": ["write_document", "library_read"],
             "note": "Reads the whole thing and reports what does not hold up.",
             "note_zh": "从头到尾读一遍,把立不住的地方报出来。"},
            {"id": "editor", "label": "Final cut", "label_zh": "定稿", "member": "Editor",
             "in_template": False,
             "skills": ["Office writing conventions"],
             "tools": ["write_document", "render_document"],
             "note": "One voice across the sections, then the deliverable.",
             "note_zh": "把各节口径统一,然后出交付件。"},
        ],
    },
})

# ---- 故事专区 -----------------------------------------------------------------------------
_add("story", {
    "name": "Story zone", "name_zh": "故事专区", "icon": "BookOpen", "state": "planned",
    "blurb": "A narrative held together: who it is about, what changes, and why the next beat "
             "follows. The pieces a story is made of, kept in one place while it is being told.",
    "blurb_zh": "一条立得住的叙事:讲的是谁、什么变了、以及下一段为什么接得上。故事是由哪些件拼成的,"
                "在讲的过程中都在一处。",
    "template": "writing",
    "library": {
        "note": "Not built yet. A story needs its cast and its beats kept as material, which is a "
                "different shelf from a document drafts folder.",
        "note_zh": "还没建。故事要把人物和节拍当材料存着,这和「文档草稿」不是同一个架子。",
        "items": [
            {"id": "cast", "label": "Cast and settings", "label_zh": "人物与设定", "state": "planned",
             "note": "Who they are, what they want, and what they cannot say out loud.",
             "note_zh": "他是谁、要什么、以及他那句说不出口的话。"},
            {"id": "beats", "label": "Beats", "label_zh": "节拍", "state": "planned",
             "note": "The turns, in order, with what each one changes.",
             "note_zh": "转折按顺序排好,每一转改变了什么。"},
        ],
    },
    "templates": {
        "note": "The creative-writing template exists today; the story-shaped starting points do not.",
        "note_zh": "创作写作那个群模板今天就有;故事形状的起手件还没有。",
        "items": [
            {"id": "writing", "label": "Creative writing", "label_zh": "创作写作", "state": "ready",
             "note": "Host, planner, copywriter, proofreader.",
             "note_zh": "主持、策划、文案、校对。"},
            {"id": "three-act", "label": "Three-act skeleton", "label_zh": "三幕骨架", "state": "planned",
             "note": "What has to be true by the end of each act, before a word of prose.",
             "note_zh": "在写一个字的正文之前,每一幕结束时必须成立什么。"},
            {"id": "beats-template", "label": "Beat sheet", "label_zh": "节拍表", "state": "planned",
             "note": "A row per turn: what changes, who pays for it, what it sets up.",
             "note_zh": "一转一行:改变了什么、谁付了代价、给后面埋了什么。"},
        ],
    },
    "workflows": {
        "note": "None of this is built. It is written down so the zone is not a blank page that "
                "looks finished.",
        "note_zh": "这些一个都还没建。写在这里,是为了让这个专区不是一张看起来已经做完的空白页。",
        "items": [
            {"id": "agreement", "label": "Keep the story straight", "label_zh": "不让故事自相矛盾",
             "state": "planned",
             "note": "Everything written has to agree with the cast sheet and the beats that came "
                     "before — checked mechanically, not by whoever remembers.",
             "note_zh": "写下去的每一段都要和人物表、和前面已经发生的节拍对得上 —— 由程序核,不靠谁记得住。"},
            {"id": "voice", "label": "One voice throughout", "label_zh": "通篇一个声音",
             "state": "planned",
             "note": "Several members write sections; the text still has to read as one narrator.",
             "note_zh": "分节由几个成员写,读起来仍要是同一个人在讲。"},
        ],
    },
    "roles": {
        "note": "Seats for a narrative. in template = already in the zone's group template.",
        "note_zh": "叙事的位子。标着「模板里有」的 = 本专区的群模板已经带了。",
        "items": [
            {"id": "host", "label": "Host", "label_zh": "主持", "member": "Aide",
             "in_template": True, "skills": [],
             "tools": ["list_workspace_files"], "note": "", "note_zh": ""},
            {"id": "planner", "label": "Structure", "label_zh": "结构与节奏", "member": "Planner",
             "in_template": True,
             "skills": ["Brainstorming rules"],
             "tools": ["write_document"],
             "note": "Owns the beats: what changes, and when the change lands.",
             "note_zh": "管节拍:什么变了、什么时候变。"},
            {"id": "copywriter", "label": "Prose", "label_zh": "执笔", "member": "Copywriter",
             "in_template": True,
             "skills": ["Relay writing rules"],
             "tools": ["write_document"],
             "note": "Writes it, section by section, against the beats.",
             "note_zh": "按节拍一节一节写出来。"},
            {"id": "reviewer", "label": "Continuity", "label_zh": "通读与连续性",
             "member": "Proofreader", "in_template": True,
             "skills": ["Check the result before you sign it off", "Debate rules"],
             "tools": ["write_document", "library_read"],
             "note": "Catches the character who changed eye colour and the motive that arrived late.",
             "note_zh": "抓那种「眼睛颜色变了」和「动机来得太晚」的问题。"},
        ],
    },
})

# ---- 创作专区 -----------------------------------------------------------------------------
_add("creation", {
    "name": "Creation zone", "name_zh": "创作专区", "icon": "Palette", "state": "planned",
    "blurb": "Where something that does not exist yet gets started: ideas, angles, a first "
             "sketch, and the material they are built on.",
    "blurb_zh": "还不在世上的东西在这里起头:想法、角度、第一版草图,以及它们所依据的材料。",
    "template": "brainstorm",
    "library": {
        "note": "Not built yet. This zone would own the illustration material — the device "
                "reference library and the pictures drawn from it — which today has no shelf of "
                "its own; it is copied into each group's workspace instead.",
        "note_zh": "还没建。这个专区该管的是绘图材料 —— 器械参考图库和照着它画出来的图 —— "
                   "而这些今天没有自己的架子:它们是拷进每个群的工作目录的。",
        "items": [
            {"id": "references", "label": "Reference library", "label_zh": "参考图库",
             "state": "partial",
             "note": "Already on this machine: 513 device photographs in 43 categories, synced "
                     "into every group's workspace. Third-party material — internal reference "
                     "only, not for a published film.",
             "note_zh": "本机已经有了:43 类 513 张器械实物照,同步进了每个群的工作目录。"
                        "**第三方素材** —— 只作内部参考,不进对外发布的成片。"},
            {"id": "authored", "label": "Drawn here", "label_zh": "这里画出来的图", "state": "planned",
             "note": "Every picture made in this zone, kept with the palette and the terms "
                     "measured from the author's own work.",
             "note_zh": "这个专区画出来的每一张图,连同从作者自己稿子里量出来的色板和术语一起存着。"},
        ],
    },
    "templates": {
        "note": "Brainstorming is the one that exists; the rest are what a creation zone needs.",
        "note_zh": "头脑风暴今天是有的;其余的是创作专区需要、但还没有的。",
        "items": [
            {"id": "brainstorm", "label": "Brainstorming", "label_zh": "头脑风暴", "state": "ready",
             "note": "Facilitator, planner, reviewer, scribe.",
             "note_zh": "主持、策划、评审、记录。"},
            {"id": "moodboard", "label": "Mood board", "label_zh": "灵感板", "state": "planned",
             "note": "A wall of references and colours decided before anything is drawn.",
             "note_zh": "在动笔之前先把参考和颜色定下来的一面墙。"},
            {"id": "motifs", "label": "Motifs and imagery", "label_zh": "母题与意象", "state": "planned",
             "note": "The few images a piece returns to, so it has a look of its own.",
             "note_zh": "一篇东西反复回到的那几个意象,这样它才有自己的样子。"},
        ],
    },
    "workflows": {
        "note": "The drawing engine exists (mechanism animation draws frame by frame, and the "
                "illustration expert is in place). What is missing is the picture generator: the "
                "cloud image account has no credit and this machine has no image checkpoint.",
        "note_zh": "绘图引擎是有的（机制动画逐帧画,绘图专家也已就位）。缺的是出图的生成器:"
                   "云端图像账号没有额度,本机没有图像模型。",
        "items": [
            {"id": "mechanism", "label": "Mechanism drawings", "label_zh": "机制图", "state": "ready",
             "note": "Frame by frame, from the five presets plus the drawing reference library.",
             "note_zh": "逐帧画,五个预设加上绘图参考图库。"},
            {"id": "expert", "label": "Illustration expert", "label_zh": "神经介入绘图专家",
             "state": "ready",
             "note": "A member whose rule is to look at a real device photograph before drawing one.",
             "note_zh": "一个成员,它写死的规矩是:画任何器械之前先看真实产品照。"},
            {"id": "generate", "label": "Generated pictures", "label_zh": "生成图", "state": "blocked",
             "note": "Blocked the same way the video zone's scene stills are: no cloud credit, no "
                     "local image checkpoint.",
             "note_zh": "和视频专区的场景静帧卡在同一处:云端没额度,本机没图像模型。"},
        ],
    },
    "roles": {
        "note": "Seats for making something new. in template = already in the zone's group template.",
        "note_zh": "从零做出点东西的位子。标着「模板里有」的 = 本专区的群模板已经带了。",
        "items": [
            {"id": "facilitator", "label": "Facilitation", "label_zh": "主持", "member": "Facilitator",
             "in_template": True,
             "skills": ["Brainstorming rules"],
             "tools": ["list_workspace_files", "find_team_resources"],
             "note": "Keeps the divergence open long enough, then closes it.",
             "note_zh": "让发散开得够久,然后收口。"},
            {"id": "planner", "label": "Direction", "label_zh": "方向", "member": "Planner",
             "in_template": True, "skills": [],
             "tools": ["write_document"],
             "note": "Turns a pile of ideas into one that can be made.",
             "note_zh": "把一堆想法收成一个做得出来的。"},
            {"id": "reviewer", "label": "Pressure", "label_zh": "评审", "member": "Reviewer",
             "in_template": True,
             "skills": ["Debate rules"],
             "tools": ["library_read", "review_picture"],
             "note": "Says which of them does not survive contact with the audience.",
             "note_zh": "说哪一个经不起受众那一下。"},
            {"id": "illustrator", "label": "Drawing", "label_zh": "绘图",
             "member": "Neurointerventional illustrator", "in_template": False,
             "skills": ["Work from a reference instead of from memory"],
             "tools": ["make_figure", "make_animation", "review_picture", "list_workspace_files"],
             "note": "Its own rule is to look at the reference library before drawing.",
             "note_zh": "它自己的规矩是:画之前先看参考图库。"},
        ],
    },
})


# --------------------------------------------------------------------------- the user's own

_USER: dict[str, dict] = {}
_USER_ERRORS: list[dict] = []
_FOLDER: "Path | None" = None
_SIGN: tuple = ()


def use_folder(folder: "str | Path | None") -> dict:
    """Where the user's own zones live. Called once at startup; safe to call again."""
    global _FOLDER, _SIGN
    _FOLDER = Path(folder) if folder else None
    _SIGN = ()
    if _FOLDER is not None:
        try:
            _FOLDER.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _note_folder_error(i18n.pick_now(
                f"the folder could not be created ({type(e).__name__}: {e}), so none of your own "
                "zones can be loaded",
                f"这个目录建不出来({type(e).__name__}: {e}),所以你自己写的专区一个都读不到"))
    return reload(force=True)


def _note_folder_error(why: str) -> None:
    global _USER_ERRORS
    _USER_ERRORS = [e for e in _USER_ERRORS if e["file"] != "zones/"] + \
                   [{"file": "zones/", "why": why}]


def _sign() -> tuple:
    if _FOLDER is None or not _FOLDER.is_dir():
        return ()
    out = []
    for p in sorted(_FOLDER.glob("*" + ZONE_SUFFIX)):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def reload(*, force: bool = False) -> dict:
    """Re-read the folder if it changed. `{loaded: [ids], errors: [{file, why}]}`."""
    global _USER, _USER_ERRORS, _SIGN
    sign = _sign()
    if not force and sign == _SIGN:
        return {"loaded": sorted(_USER), "errors": list(_USER_ERRORS)}
    _SIGN = sign
    rows: dict[str, dict] = {}
    errors: list[dict] = [e for e in _USER_ERRORS if e["file"] == "zones/"]
    for path in (sorted(_FOLDER.glob("*" + ZONE_SUFFIX)) if _FOLDER else []):
        try:
            raw = path.read_bytes()
        except OSError as e:
            errors.append({"file": path.name, "why": f"{type(e).__name__}: {e}"})
            continue
        if len(raw) > MAX_BYTES:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"the file is {len(raw) // 1024} KB; a zone is a page of declarations",
                f"这个文件有 {len(raw) // 1024} KB,而一个专区只有一页声明")})
            continue
        try:
            body = json.loads(raw)
        except ValueError as e:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"not valid JSON: {e}", f"不是合法的 JSON:{e}")})
            continue
        zid = path.stem
        if zid in BUILTIN:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"the id \"{zid}\" is already used by a zone this app ships — rename the file",
                f"「{zid}」这个 id 已经被本程序自带的专区占了 —— 请给文件改个名")})
            continue
        if not isinstance(body, dict):
            errors.append({"file": path.name, "why": i18n.pick_now(
                "the file must contain a JSON object", "文件内容必须是一个 JSON 对象")})
            continue
        try:
            rows[zid] = _zone(body, zid=zid)
        except ValueError as e:
            errors.append({"file": path.name, "why": str(e)})
    _USER = rows
    _USER_ERRORS = errors
    return {"loaded": sorted(rows), "errors": errors}


def available() -> dict[str, dict]:
    """Every zone: the ones we ship, plus the user's own folder."""
    reload()
    return {**BUILTIN, **_USER}


def row(zone_id: str) -> "dict | None":
    return available().get((zone_id or "").strip())


def errors() -> list[dict]:
    reload()
    return list(_USER_ERRORS)


def _order(z: dict) -> tuple:
    """Shipped zones in the order they were declared, ready ones first; then the user's own."""
    shipped = list(BUILTIN)
    rank = shipped.index(z["id"]) if z["id"] in shipped else len(shipped)
    return (0 if z["state"] == "ready" else 1, rank, z["id"])


def _say(body: dict, key: str) -> str:
    """One bilingual field, in the request language — the boundary where the two collapse into one.

    Everything above keeps both languages because that is what a registry is *for*; the client gets
    one, the same way `templates.member_view` and `musicprompt.vocabulary` do it. A page that
    chose between an `_zh` and a non-`_zh` field for itself would be a second answer to "which
    language is this".
    """
    en = str(body.get(key) or "")
    return i18n.pick_now(en, str(body.get(key + "_zh") or en))


def catalog() -> list[dict]:
    """The list the sidebar draws: identity only.

    Deliberately not the full body. The sidebar is drawn on every render, and the full body carries
    live counts and a resolved preset list — neither of which belongs to a navigation entry.
    """
    return [{"id": z["id"], "name": _say(z, "name"), "icon": z["icon"], "state": z["state"],
             "blurb": _say(z, "blurb")}
            for z in sorted(available().values(), key=_order)]


# Which shelves a zone's library reads from. A map rather than a name inside the JSON on purpose:
# a file that could name an arbitrary counter would be a file that makes this module import
# anything it likes. Both shelves are read **lazily**, so importing `zones` stays cheap and cannot
# form a cycle with `store`.
_SHELVES: dict[str, str] = {"music-shelf": "music", "my-material": "studio"}


def _counts() -> dict[str, int]:
    from . import music, studio
    out: dict[str, int] = {}
    for item_id, shelf in _SHELVES.items():
        try:
            out[item_id] = len(music.available()) if shelf == "music" else len(studio.assets())
        except Exception:  # noqa: BLE001 — one unreadable shelf must not blank the whole page
            continue
    return out


def detail(zone_id: str) -> "dict | None":
    """One zone, localised, with the surfaces the page draws filled in as far as they can be
    truthfully.

    Live counts where a shelf exists (music, my material); the starting points read from the one
    module that defines them; everything else exactly as declared. What is *not* done here is
    inventing content for a surface that has none: an empty list stays empty, and the page says so.
    """
    got = row(zone_id)
    if got is None:
        return None
    # The second language is dropped at the boundary, not carried along: a page that received both
    # would pick between them, and that pick is a second answer to "which language is this".
    # So is a surface the page does not draw — handing over `roles` anyway would be data nothing
    # renders, which is the failure this project keeps finding (`Plan.dropped` rode into a message
    # body for weeks and no page ever read it). `roles` stays in the registry, where it is
    # validated and where the group this zone will start can find it.
    out = {k: v for k, v in got.items()
           if k not in ("name", "blurb") + SURFACES and not k.endswith("_zh")}
    out["name"] = _say(got, "name")
    out["blurb"] = _say(got, "blurb")
    for surface in DRAWN:
        body = got[surface]
        out[surface] = {
            "note": _say(body, "note"),
            "items": [{k: v for k, v in item.items() if not k.endswith("_zh")}
                      | {"label": _say(item, "label"), "note": _say(item, "note")}
                      for item in body["items"]],
        }
    counts = _counts()
    for item in out["library"]["items"]:
        if item["id"] in counts:
            item["count"] = counts[item["id"]]
    if got["id"] == "video":
        # The composer's own starting points, listed from `musicprompt` rather than retyped — a
        # second copy is how a page ends up offering a preset the composer does not have.
        from . import musicprompt
        out["templates"] = {
            "note": out["templates"]["note"],
            "items": [{"id": p["id"], "label": i18n.pick_now(p["en"], p["zh"]), "note": p["note"],
                       "state": "ready"} for p in musicprompt.PRESETS] + out["templates"]["items"],
        }
    return out
