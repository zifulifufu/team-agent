"""Video generation through a self-hosted MiniMax H3 server, MetaChat's open media API, or
Volcengine's Ark (Doubao Seedance).

Three shapes live here because they disagree about every parameter, and a request validated
against another shape's list would be rejected locally for a value the server accepts:

    self-hosted H3 (kind `minimax_video`)
        POST {base}/v1/videos              -> {"id": "..."}          submit
        GET  {base}/v1/videos/{id}         -> {"status": "..."}      poll
        GET  {base}/v1/videos/{id}/content -> the mp4 bytes          download

    MetaChat open media API (kind `metachat_media`; `{base}` is `https://api.mmchat.xyz/open/v1`)
        POST {base}/video/generate         -> {"data": {"id": ...}}  submit
        GET  {base}/video/result/{id}      -> {"data": {...}}        poll
        GET  the `video_url` it reports    -> the mp4 bytes          download

    Volcengine Ark (kind `ark_video`; `{base}` is `https://ark.cn-beijing.volces.com/api/v3`)
        POST {base}/contents/generations/tasks      -> {"id": "cgt-..."}   submit
        GET  {base}/contents/generations/tasks/{id} -> {"status": ...}     poll
        GET  the `content.video_url` it reports      -> the mp4 bytes      download

Its drawing endpoints (`image/generate`, `midjourney/imagine`) are reached through
`app/imagegen.py` — one key, two APIs, and a job is a job in both.

Four things about the MetaChat one are worth stating up front, because they are the API's
properties and not ours to paper over:

- **Both of its models are image-to-video.** `grok-imagine-video-1.5-preview` says so in as many
  words ("必须提交 1 张首帧参考图"), and `mj-video-v1` generates from a keyframe too. There is no
  text-only path to offer, so the tool is not going to pretend there is one.
- **The reference image must be an http(s) URL**, because MetaChat's servers fetch it. A path
  inside the workspace cannot be sent — H3 reads local files, this one cannot — so a local frame
  is refused with the reason rather than uploaded somewhere behind the user's back.
- **It has no model-listing endpoint** (see `media.BUILTIN_MEDIA_MODELS`), so the model name is a
  setting rather than something discovered.
- **It takes no `seed`** where H3 does, and no duration on the Midjourney path.

Three properties of Ark shape this file, and each is a documented constraint rather than a choice:

- **The request is a `content` array, not a prompt string.** Text goes in as one item; every
  reference picture, video and audio clip is another, each carrying a `role`. Which role you use
  changes the *task type* the model runs (first_frame/last_frame is keyframe interpolation,
  reference_* is the omni-reference path), and the task type in turn constrains the parameters —
  a first_frame pins the ratio to `adaptive`. `ark_ratio` applies that rule and reports it.
- **`generate_audio` defaults to true**, so a silent clip is the thing you have to ask for.
  Nothing here sends it unless the caller said something: an omitted field leaves Ark's own default
  in place, which is also the compatible answer for a model name we do not ship a description for.
- **No model-listing endpoint either** (`media.BUILTIN_MEDIA_MODELS["ark_video"]` owns the id), and
  the finished clip is fetched from a signed object-storage URL — **without the key**, since that
  link points at TOS and the key belongs to Ark alone.

Two things the H3 server will *not* do for us, and which therefore shape this file:

- H3-Context-IR (the prompt-shaping module) and H3-Regenerate-2K are not open source, so the
  prompt goes out exactly as written and 768p is the best a local deployment produces. Saying
  otherwise in the UI would be a lie.
- `conditions[].uri` is resolved by the *server* process, not by us. A local file therefore only
  works when the server can read that path — same machine, or a mounted directory. Anything
  else has to be an http(s) URL.

Nothing here decides whether generation is allowed: that is the tool's job (the offline switch
and the approval flow), because only it knows the group and the settings.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
import urllib.parse
from pathlib import Path

import httpx

from . import i18n, media, net
from .coderun import inside as _inside     # one implementation of "is this still inside the workspace"

# Every provider kind that is a generator rather than a chat model, re-exported from `media`:
# `store.list_models()` filters by this, so a member can never be pointed at one.
MEDIA_KINDS: tuple[str, ...] = media.MEDIA_KINDS
# ...while this module only drives its own kinds. The two lists were the same thing until a
# second generator existed; picking a provider by the union would let the video tool select an
# image provider, which surfaces as a broken server rather than a wrong lookup.
KINDS: tuple[str, ...] = ("minimax_video", "metachat_media", "ark_video")

H3_KIND = "minimax_video"
META_KIND = "metachat_media"
ARK_KIND = "ark_video"

# What each shape accepts. Kept per kind rather than as one shared list: the three parameters
# disagree, and a single union would let the tool send MetaChat a pixel count it does not take,
# or H3 a named resolution it has never heard of.
SHAPES: dict[str, dict] = {
    "minimax_video": {
        "seconds": (4, 15),                                             # H3's own range
        "ratios": ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
        "default_ratio": "16:9",
        "model": False,      # the server has one checkpoint; no model id in the request
        "audio": True,       # H3 renders stereo sound as part of the clip
    },
    "metachat_media": {
        "seconds": (1, 15),                                             # documented as 1-15, 5 by default
        "ratios": ("adaptive", "16:9", "9:16", "4:3", "3:4", "1:1", "3:2", "2:3"),
        "default_ratio": "adaptive",   # follows the reference image, which is the only input it has
        "model": True,       # every call names the model: the same host serves each of them
        "audio": False,      # neither of its models documents an audio track
    },
    "ark_video": {
        "seconds": (4, 30),                                             # Seedance 2.5 documents 4-30
        "ratios": ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"),
        "default_ratio": "16:9",        # Ark's own default; `adaptive` is forced by a keyframe
        "model": True,
        "audio": True,       # `generate_audio` defaults to true, which is why `sound` reads from here
    },
}


def shape_of(kind: str) -> dict:
    """The parameter set of one video provider, so a caller never has to know which kind it holds.
    An unknown kind gets H3's shape, which is what an unset/garbled kind meant before this existed."""
    return SHAPES.get(kind) or SHAPES[H3_KIND]


# The tool's own schema is built before any provider is known, so it advertises the union and the
# runtime checks against the chosen provider's list — a superset in the schema never forbids a
# value that some provider accepts, which is the failure that would be hardest to explain.
MIN_SECONDS = min(s["seconds"][0] for s in SHAPES.values())
MAX_SECONDS = max(s["seconds"][1] for s in SHAPES.values())
ASPECT_RATIOS: tuple[str, ...] = tuple(
    dict.fromkeys(r for s in SHAPES.values() for r in s["ratios"])
)

# MetaChat's open media API. `{base}` is the API prefix MetaChat's documentation gives for it —
# `https://api.mmchat.xyz/open/v1` — so every path is relative to that and not to the host, which is
# what the user is told to paste into the provider's address field.
#
# Which path a job goes to, and what it may carry, is per model and lives in `media.MEDIA_MODELS`:
# Grok takes `video/generate`, Midjourney has its own `midjourney/video` and polls on
# `midjourney/result/{id}`, and Midjourney accepts none of the duration/ratio knobs. Asking the
# table is what keeps this module from sending one shape to all of them.
#
# The only two resolutions that API takes. A named resolution rather than a pixel count, so the
# user's "output short edge" is mapped onto them instead of duplicated as a second setting — at or
# below this pivot it is the smaller file.
META_RESOLUTIONS = ("480p", "720p")
META_RESOLUTION_PIVOT = 640

# Volcengine Ark's own video API. `{base}` is the API prefix its documentation gives —
# `https://ark.cn-beijing.volces.com/api/v3` — so these paths are relative to that and not to the
# host, which is what the user is told to paste into the provider's address field.
#
# The task object is the *whole* reply (`id`, `model`, `status`, `content.video_url`, `usage`,
# `error`), which is why the finished clip needs a nested lookup — see `link_of`.
ARK_SUBMIT = "contents/generations/tasks"
ARK_TASK = "contents/generations/tasks/{vid}"

# Ark takes a named resolution, so the user's "output short edge" is mapped onto its two values
# rather than duplicated as a second setting — the same pivot MetaChat uses, since both take the
# same pair and neither takes a pixel count. (1080p exists in the API but not in the Seedance 2.5
# launch build, so offering it would be offering something that fails.)
ARK_RESOLUTIONS = ("480p", "720p")

# One request may carry a lot of reference material — 30 images, 10 videos, 10 audio clips, 50
# items in total — but the *body* is capped at 64 MB, and the documentation says in as many words
# not to base64-encode large files. So a workspace file is only inlined below this, and anything
# bigger has to be handed over as a URL the service can fetch.
ARK_INLINE_MB = 20

# Status words. Anything that is neither done nor failed counts as "still working" and the
# deadline decides when to stop, so a server that invents a new word for "queued" does not make
# us fail instantly. Ark's six (`queued`, `running`, `succeeded`, `failed`, `expired`, `cancelled`)
# are covered by the two sets below without needing a word of their own.
_DONE = {"completed", "complete", "succeeded", "success", "done", "finished"}
_FAILED = {"failed", "failure", "error", "cancelled", "canceled", "expired", "rejected"}

POLL_START, POLL_MAX = 2.0, 10.0
# A floor on the wait, so a mistyped setting cannot cut a render off after a second. Tests lower
# it to keep the polling loop quick.
MIN_DEADLINE = 5.0
SUBMIT_TIMEOUT = 60.0
STATUS_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 300.0


class VideoError(Exception):
    """A failure worth showing the user: the message is already in the request language."""


# ------------------------------------------------------------------ providers
def pick_provider(store, cfg: dict, prefer: str = "") -> tuple[dict | None, str]:
    """(the provider to generate with, why there is none).

    `prefer` is the provider a *media member* was created from, and it outranks the setting: a
    member called "Seedance" must run Seedance whatever the group's last global choice was. It is
    the same lookup either way, because a member's provider is checked exactly as strictly — an id
    that no longer exists is reported rather than silently replaced, since quietly generating on a
    different machine than the user asked for is exactly the kind of surprise this file should not
    produce.
    """
    # Not `media.providers_of_kind`: a gateway whose kind is "chat" still belongs here when its
    # own model list says it serves video models (see Store.providers_for_use).
    rows = store.providers_for_use("video", KINDS)
    wanted = str(prefer or cfg.get("video_provider_id") or "").strip()
    if wanted:
        p = next((x for x in rows if x["id"] == wanted), None)
        if p is None:
            return None, i18n.pick_now(
                f"Video generation is set to use \"{wanted}\", but no video provider with that id exists any more.",
                f"视频生成指定使用「{wanted}」,但已经找不到这个 id 的视频服务商了。",
            )
        if not p["enabled"]:
            return None, i18n.pick_now(
                f"Video provider \"{p['name']}\" is switched off.", f"视频服务商「{p['name']}」已停用。"
            )
        if not (p["base_url"] or "").strip():
            return None, i18n.pick_now(
                f"Video provider \"{p['name']}\" has no address configured.", f"视频服务商「{p['name']}」没有填地址。"
            )
        return p, ""
    usable = [p for p in rows if p["enabled"] and (p["base_url"] or "").strip()]
    if not usable:
        return None, i18n.pick_now(
            "Video generation is on, but no video provider has been added yet. Either add "
            "\"Doubao Seedance (Volcengine Ark)\" and paste your key — its model is the one "
            "Seedance 2.5, no GPU involved — or add \"MetaChat video (open media API)\", or add "
            "\"MiniMax H3 (self-hosted video)\" and point it at your SGLang / vLLM server.",
            "视频生成已开启,但还没有添加视频服务商。要么添加「Doubao Seedance(火山方舟)」并填上密钥"
            "(模型就是 Seedance 2.5,不需要显卡),要么添加「MetaChat 视频(开放媒体接口)」,"
            "要么添加「MiniMax H3(自建视频生成)」并填上你的 SGLang / vLLM 服务地址。",
        )
    return usable[0], ""


def blocked_by_offline(provider: dict, cfg: dict) -> str:
    """The same rule the routing layer uses: a non-local provider is out of bounds while
    outbound calls are off. Empty string = allowed."""
    if provider["is_local"] or cfg.get("external_calls_enabled"):
        return ""
    return i18n.pick_now(
        f"\"{provider['name']}\" is not marked as local and outbound calls are switched off, so "
        "no video was generated. Mark it local if it really runs on your own machine, or turn "
        "outbound calls back on.",
        f"「{provider['name']}」不是本地服务,而「允许外呼」是关的,所以没有生成视频。"
        "如果它确实跑在你自己的机器上,请把它标为本地;否则请打开「允许外呼」。",
    )


# ------------------------------------------------------------------ helpers
def _why(r: httpx.Response) -> str:
    """The most useful sentence a failed response carries, whatever shape it uses."""
    body = (r.text or "").strip()
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            err = d.get("error")
            if isinstance(err, dict) and err.get("message"):
                body = str(err["message"])
            elif isinstance(err, str) and err:
                body = err
            elif d.get("message"):
                body = str(d["message"])
            elif d.get("detail"):
                body = str(d["detail"])
    except ValueError:
        pass
    body = " ".join(body.split())
    return f"HTTP {r.status_code}{': ' + body[:300] if body else ''}"


def slug(text: str, limit: int = 40) -> str:
    """A short, filesystem-safe name from a prompt. ASCII only, so a mostly-Chinese prompt
    yields little and the caller falls back to the video id."""
    out = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return out[:limit].rstrip("-")


def build_payload(
    prompt: str, *, short_edge: int, aspect_ratio: str, duration_seconds: int, seed: int,
    first_frame: str = "", last_frame: str = "",
) -> dict:
    """The request body, exactly as the released server documents it (see the repository's
    `scripts/readme/reproducible-768p-*.sh`). `task` follows the conditions: no image is
    text-to-video, one or two images is the first/last-frame mode.

    `conditions[].uri` may be an http(s) URL, a `file://` URL, or a path we have already turned
    into one — resolving it is the server's job.
    """
    conds = []
    if first_frame:
        conds.append({"type": "image", "uri": first_frame, "role": "keyframe", "frame_index": 0})
    if last_frame:
        conds.append({"type": "image", "uri": last_frame, "role": "keyframe", "frame_index": -1})
    task = "fl2va" if conds else "t2va"
    return {
        "task": task,
        "prompt": prompt,
        "conditions": conds,
        "target": {"short_edge": int(short_edge), "aspect_ratio": aspect_ratio,
                   "duration_seconds": int(duration_seconds)},
        "seed": int(seed),
    }


def resolution_for(short_edge: int) -> str:
    """The named resolution MetaChat's API takes, from the pixel count the user configured.

    Two values exist and neither of them is a pixel count, so something has to choose. The
    alternative — a second setting that means almost the same thing as "output short edge" — is a
    control to keep in sync for no gain, and it would still have to decide which one applies.
    """
    try:
        n = int(short_edge)
    except (TypeError, ValueError):
        return META_RESOLUTIONS[1]
    return META_RESOLUTIONS[1] if n > META_RESOLUTION_PIVOT else META_RESOLUTIONS[0]


def metachat_params(model: str, *, ratio: str, seconds: int, short_edge: int) -> dict:
    """Only the parameters *this* model's documentation lists, filled from what the user configured.

    The values that mean something different per family are mapped here (our pixel count becomes the
    named resolution; our aspect ratio becomes theirs), and everything else is left out on purpose:
    each model's own defaults are documented, and a parameter a model does not recognise comes back
    as a failure the user has paid for.
    """
    values = {"ratio": ratio, "aspect": ratio, "duration": int(seconds),
              "resolution": resolution_for(short_edge), "num": 1}
    want = media.job_of(model, "video")["params"]
    return {k: values[k] for k in want if k in values}


def metachat_payload(
    prompt: str, *, model: str, ratio: str, duration_seconds: int, short_edge: int, frame: str = "",
) -> dict:
    """The body `POST {base}/<this model's submit path>` documents.

    `prompt`, `model` and `params` are all it defines; a reference image goes in `images` as a URL
    the service fetches itself. One image, not two: these models generate *from a keyframe*, so there
    is no last-frame slot — `_generate_video` refuses a `last_frame` rather than dropping it, because
    a member that asked for one and silently did not get it would draw the wrong conclusion.

    Of the fields the documentation's example shows, only `url` is sent: `type`, `size`, `w` and `h`
    describe the caller's own copy of the picture, and filling them in for a URL we were handed would
    be making up numbers.
    """
    return {
        "prompt": prompt,
        "model": model,
        "params": metachat_params(model, ratio=ratio, seconds=duration_seconds, short_edge=short_edge),
        "images": [{"url": frame}] if frame else [],
    }


ARK_ADAPTIVE = "adaptive"

# What each provider's own documentation asks a prompt to look like.
#
# This is the same knowledge the tool description carries, kept separately because the two are read
# by different models: the tool spec is handed to a model that is *calling* the tool, while this is
# handed to the one that is *writing the prompt* out of a group's conversation
# (`orchestrator._media_prompt`). Folding them together would make the second read a lot of
# parameter documentation it has no use for.
PROMPT_NOTES: dict[str, tuple[str, str]] = {
    "minimax_video": (
        "Write it the way MiniMax H3 is trained to read it: name the shots, then the sound, in the "
        "form \"[Shot 1] … [Shot 2] … overall_soundscape: … non_diegetic_music: …\".",
        "按 MiniMax H3 被训练来读的格式写:先分镜,再说声音,用「[Shot 1] … [Shot 2] … "
        "overall_soundscape: … non_diegetic_music: …」这种形式。",
    ),
    "metachat_media": (
        "That service generates only from a single reference image and its videos carry no audio, so "
        "describe the motion and the camera; do not write dialogue.",
        "那个服务只按一张参考图生成,视频也没有音轨,所以要描述动作和运镜,不要写对白。",
    ),
    "ark_video": (
        "Seedance 2.5 reads a description of the shots and generates sound by default, so say what "
        "should be heard as well as what should be seen. If the material it is given should be used, "
        "refer to it by position (@图片1, @视频1, @音频1).",
        "Seedance 2.5 读的是对镜头的描述,而且默认生成声音,所以既要写看到的,也要写听到的。"
        "如果要用到随附的素材,按位置指代它们(@图片1、@视频1、@音频1)。",
    ),
}


def prompt_note(kind: str, lang: str = "en") -> str:
    """What to tell the prompt writer about one provider. Empty for a kind we know nothing about —
    better a missing sentence than an invented convention."""
    en, zh = PROMPT_NOTES.get(kind or "", ("", ""))
    return zh if lang == "zh" else en


def ark_ratio(ratio: str, *, keyframe: bool) -> tuple[str, bool]:
    """(ratio to send, whether it had to be changed).

    A `first_frame`/`last_frame` pins the output to the source's shape — the documentation says the
    ratio "必须为 adaptive" for that task type — so a request that names both a keyframe and a ratio
    is *adjusted* rather than refused, exactly as a too-long clip is, and the caller is told.
    Refusing would be defensible and annoying at the same time: the ratio people type is the one
    they always type.
    """
    if keyframe and ratio != ARK_ADAPTIVE:
        return ARK_ADAPTIVE, True
    return ratio, False


def ark_asset(value: str, workspace: Path, *, what: str, what_zh: str) -> str:
    """One reference for Ark's `content` array.

    Ark accepts three forms: a public URL it fetches itself, a `data:` URL carrying the bytes, and
    an `asset://` id from its own asset library. The middle one is what makes this work for a group
    at all — the pictures a group has are files in its own workspace, and there is nowhere public
    to put them — so a workspace path is read and inlined. Above `ARK_INLINE_MB` the body would be
    closing on Ark's own 64 MB cap, and its documentation says outright not to base64-encode large
    files, so the caller is told to host that one instead of having a request rejected for a reason
    nobody could guess.

    A file outside the workspace is refused for the same reason `frame_uri` refuses one: these
    bytes leave this machine, and a member must not be able to post arbitrary local files to a
    third party.
    """
    from . import attachments          # local import: this module is imported by the store

    v = (value or "").strip()
    if not v:
        return ""
    if re.match(r"^(https?://|data:|asset://)", v, re.I):
        return v
    root = workspace.resolve()
    if v.lower().startswith("file://"):
        raw = urllib.parse.unquote(v[7:])
        if raw.startswith("localhost/"):
            raw = raw[len("localhost"):]
        target = Path(raw)
        if not target.is_absolute():
            target = root / target
    else:
        target = root / v.lstrip("/")
    target = target.resolve()
    if not _inside(root, target):
        raise VideoError(i18n.pick_now(
            f"\"{v}\" is outside this group's workspace, so it was not sent. Put the {what} in the "
            "workspace and refer to it by a path inside it, or pass an http(s) URL the service can "
            "fetch.",
            f"「{v}」在本群工作目录之外,没有发出去。请把{what_zh}放进工作目录并用目录内的路径引用,"
            "或改用服务方取得到的 http(s) 地址。",
        ))
    if not target.is_file():
        # A leading "/" means "from the workspace root" rather than a path on this machine (the same
        # rule `frame_uri` uses, so the two cannot disagree about what a reference means), and the
        # wording says so rather than claiming a file that may well exist elsewhere does not.
        raise VideoError(i18n.pick_now(
            f"There is no file at \"{v}\" in this group's workspace, so it was not sent. A path "
            "starting with / is read from the workspace root; anything outside the workspace has to "
            "be an http(s) URL the service can fetch.",
            f"本群工作目录里没有「{v}」这个文件,没有发出去。以 / 开头的路径按「工作目录根」来读;"
            "工作目录之外的东西必须换成服务方取得到的 http(s) 地址。",
        ))
    data = target.read_bytes()
    limit = ARK_INLINE_MB * 1024 * 1024
    if len(data) > limit:
        raise VideoError(i18n.pick_now(
            f"\"{target.name}\" is {len(data) / 1024 / 1024:.0f} MB, over the {ARK_INLINE_MB} MB this "
            f"app will send inline as a {what} — Ark caps a whole request at 64 MB and its "
            "documentation says not to base64-encode large files. Put it somewhere with an http(s) "
            "URL and pass that instead.",
            f"「{target.name}」有 {len(data) / 1024 / 1024:.0f} MB,超过本程序内联发送{what_zh}的 "
            f"{ARK_INLINE_MB} MB 上限 —— Ark 整个请求上限是 64 MB,它的文档也明确说大文件不要用 "
            "Base64。请把它放到一个有 http(s) 地址的地方再传进来。",
        ))
    _kind, mime, _ext = attachments.classify(data, target.name)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def ark_payload(
    prompt: str, *, model: str, ratio: str, duration_seconds: int, short_edge: int,
    first_frame: str = "", last_frame: str = "", reference_images: tuple[str, ...] = (),
    reference_videos: tuple[str, ...] = (), reference_audios: tuple[str, ...] = (),
    seed: int = 0, audio: bool | None = None,
) -> dict:
    """The body `POST {base}/contents/generations/tasks` documents.

    Text first, then every reference with its `role`: the role is not decoration, it is what makes
    Ark pick the task type (keyframe interpolation versus the omni-reference path), and the prompt
    refers to the items by position — `@图片1`, `@视频1` — so the order here is what the member's
    own sentence is counting on. Text, then pictures, then videos, then audio is the order the
    documentation's examples use.

    `resolution` is Ark's own pair, mapped from the pixel count the user configured (see
    `resolution_for`); `watermark: false` is sent because the default is the thing you would have
    to notice and undo, and the user's own example asks for it off. `generate_audio` is sent *only*
    when the caller said something, since Ark already defaults it to true and an omitted field is
    also the compatible answer for a model name this app ships no description for.
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    for url, role in ((first_frame, "first_frame"), (last_frame, "last_frame")):
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}, "role": role})
    for url in reference_images:
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})
    for url in reference_videos:
        content.append({"type": "video_url", "video_url": {"url": url}, "role": "reference_video"})
    for url in reference_audios:
        content.append({"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"})
    body: dict = {
        "model": model,
        "content": content,
        "ratio": ratio,
        "duration": int(duration_seconds),
        "resolution": resolution_for(short_edge),
        "watermark": False,
    }
    if seed:
        body["seed"] = int(seed)
    if audio is not None:
        body["generate_audio"] = bool(audio)
    return body


def link_of(kind: str, detail: dict, field: str) -> str:
    """Where the finished clip lives, in whichever reply shape this provider uses.

    MetaChat and H3 report it at the top of the job's own object (`video_url`); Ark nests it one
    level down, inside `content`. One lookup here beats a branch at each of the three call sites.
    """
    if kind == ARK_KIND:
        inner = detail.get("content")
        return str((inner or {}).get("video_url") or "") if isinstance(inner, dict) else ""
    return str(detail.get(field) or "")


def image_url(value: str) -> str:
    """An image the *provider* will fetch, for the MetaChat `images` array.

    http(s) only, and that is the whole point: MetaChat downloads the picture itself, so a file on
    this machine is not something it can be handed — there is no upload endpoint in that API. H3 is
    the opposite (its server resolves `file://`), which is why this is a separate function rather
    than a flag on `frame_uri`. Saying so plainly beats sending a URL the other side cannot open
    and reporting whatever error comes back.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if re.match(r"^https?://", v, re.I):
        return v
    raise VideoError(i18n.pick_now(
        f"\"{v}\" is a file on this machine, and MetaChat's video API only accepts reference images "
        "as http(s) URLs — it downloads the picture itself, and it has no upload endpoint. Put the "
        "image somewhere it can reach (or use a provider whose server reads local files, such as "
        "the self-hosted H3 one) and pass that URL.",
        f"「{v}」是本机文件,而 MetaChat 的视频接口只接受 http(s) 的参考图地址——它自己去下载图片,"
        "也没有上传接口。请把图片放到它取得到的地方(或改用能读本地文件的 H3 自建服务),再填那个地址。",
    ))


def frame_uri(value: str, workspace: Path) -> str:
    """An image reference for `conditions[].uri`.

    Either an http(s) URL — the server fetches it, nothing local is read — or a path inside this
    group's workspace. A `file://` URL is resolved here and confined to the workspace as well:
    it is the *server* that resolves a file URL, so allowing an arbitrary one would let a member
    point that server at any file on this machine. To reference anything else, copy it into the
    workspace first.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if re.match(r"^https?://", v, re.I):
        return v
    root = workspace.resolve()
    if v.lower().startswith("file://"):
        raw = urllib.parse.unquote(v[7:])
        if raw.startswith("localhost/"):
            raw = raw[len("localhost"):]
        target = Path(raw)
        if not target.is_absolute():
            target = root / target
    else:
        target = root / v.lstrip("/")
    target = target.resolve()
    if not _inside(root, target):
        raise VideoError(i18n.pick_now(
            f"\"{v}\" is outside this group's workspace. Put the image in the workspace and "
            "refer to it by a path inside it (a file:// URL is checked the same way), or pass an "
            "http(s) URL the server can fetch.",
            f"「{v}」在本群工作目录之外。请把图片放进工作目录并用目录内的路径引用(file:// 地址同样按这个规矩检查),"
            "或改用服务器能取到的 http(s) 地址。",
        ))
    if not target.is_file():
        raise VideoError(i18n.pick_now(
            f"There is no file at \"{v}\" in this group's workspace.",
            f"本群工作目录里没有「{v}」这个文件。",
        ))
    return f"file://{target}"


def clamp_seconds(want: object, cap: int, kind: str | None = None) -> tuple[int, bool]:
    """(seconds to use, whether the request had to be clamped).

    The range is the provider's own: H3 takes 4-15 seconds and MetaChat's API 1-15, so a
    3-second clip is a perfectly good request on one of them and impossible on the other.
    """
    lo_kind, hi_kind = shape_of(kind or H3_KIND)["seconds"]
    try:
        n = int(want) if want not in (None, "") else hi_kind
    except (TypeError, ValueError):
        n = hi_kind
    lo, hi = lo_kind, max(lo_kind, min(hi_kind, int(cap)))
    used = max(lo, min(hi, n))
    return used, used != n


# ------------------------------------------------------------------ HTTP
@contextlib.asynccontextmanager
async def _client(client: httpx.AsyncClient | None, timeout: float, url: str = ""):
    """The client for one request, when the caller did not bring its own.

    The **address decides**, exactly as `net.client` does everywhere else in this app. A video
    server on this machine or the LAN connects directly — a system proxy would quietly send that
    traffic somewhere else, which is how a perfectly healthy local server came to report 502. But
    MetaChat's media API is on the internet, and forcing *that* one direct would break exactly the
    networks that need a proxy. Getting this wrong is visible in the UI as two "test the service"
    buttons on the same host disagreeing with each other.
    """
    if client is not None:
        yield client
        return
    async with net.client(url, timeout=timeout) as c:
        yield c


async def submit(prov: dict, payload: dict, *, client: httpx.AsyncClient | None = None,
                 job: dict | None = None) -> str:
    if prov.get("kind") == META_KIND:
        return await _meta_submit(prov, payload, client=client, job=job)
    if prov.get("kind") == ARK_KIND:
        return await _ark_submit(prov, payload, client=client)
    url = media.api_url(prov["base_url"], "v1/videos")
    async with _client(client, SUBMIT_TIMEOUT, url) as c:
        try:
            r = await c.post(url, headers=media.auth_headers(prov.get("api_key", "")), json=payload,
                             timeout=SUBMIT_TIMEOUT)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Could not reach the video server at {prov['base_url']}: {type(e).__name__}: {e}",
                f"连不上视频服务 {prov['base_url']}:{type(e).__name__}: {e}",
            )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"The video server refused the request ({_why(r)})", f"视频服务拒绝了这次请求({_why(r)})"
        ))
    try:
        data = r.json()
    except ValueError:
        raise VideoError(i18n.pick_now(
            "The video server did not return JSON. Is this address really a video-generation server?",
            "视频服务没有返回 JSON。这个地址真的是视频生成服务吗?",
        )) from None
    vid = str((data or {}).get("id") or (data or {}).get("video_id") or "").strip()
    if not vid:
        raise VideoError(i18n.pick_now(
            f"The video server accepted the request but returned no task id ({str(data)[:200]})",
            f"视频服务收下了请求但没有返回任务 id({str(data)[:200]})",
        ))
    return vid


async def status_of(prov: dict, vid: str, *, client: httpx.AsyncClient | None = None,
                    job: dict | None = None) -> tuple[str, dict]:
    if prov.get("kind") == META_KIND:
        return await _meta_status(prov, vid, client=client, job=job)
    if prov.get("kind") == ARK_KIND:
        return await _ark_status(prov, vid, client=client)
    url = media.api_url(prov["base_url"], f"v1/videos/{vid}")
    async with _client(client, STATUS_TIMEOUT, url) as c:
        try:
            r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")), timeout=STATUS_TIMEOUT)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Lost contact with the video server while waiting: {type(e).__name__}: {e}",
                f"等待期间与视频服务失去联系:{type(e).__name__}: {e}",
            )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"Could not read the generation status ({_why(r)})", f"读取生成状态失败({_why(r)})"
        ))
    try:
        data = r.json()
    except ValueError:
        data = {}
    return str((data or {}).get("status") or "").strip().lower(), (data or {})


async def download(prov: dict, vid: str, *, max_bytes: int, client: httpx.AsyncClient | None = None,
                   url: str = "") -> bytes:
    """The mp4 bytes.

    Which address they come from differs: H3 serves them off its own task id, while MetaChat and
    Ark return a link into their own object storage — so `url` is the caller's answer for those
    shapes, and guessing any other endpoint would be inventing one. The checks afterwards are the
    same for all three, which is the part worth not writing twice.
    """
    if prov.get("kind") in (META_KIND, ARK_KIND):
        if not url:
            name = prov.get("name") or prov.get("kind") or "the service"
            raise VideoError(i18n.pick_now(
                f"{name} reported the job as finished but gave no video address, so nothing was "
                "saved. This is worth reporting to them rather than retrying.",
                f"{name} 报告任务已完成,但没有给出视频地址,所以没有保存。这种情况建议反馈给该服务商,重试没有意义。",
            ))
        # Deliberately no Authorization header: the link points at that service's object storage
        # (an aliyuncs host for MetaChat, a volces.com one for Ark), and the key is for the API
        # alone. Sending it there would hand the credential to a third party for a file that does
        # not need it.
        headers: dict[str, str] = {}
        target = url
        timeout = DOWNLOAD_TIMEOUT
    else:
        headers = media.auth_headers(prov.get("api_key", ""))
        target = media.api_url(prov["base_url"], f"v1/videos/{vid}/content")
        timeout = DOWNLOAD_TIMEOUT
    async with _client(client, timeout, target) as c:
        try:
            r = await c.get(target, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Could not download the video: {type(e).__name__}: {e}",
                f"下载视频失败:{type(e).__name__}: {e}",
            )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"Could not download the video ({_why(r)})", f"下载视频失败({_why(r)})"
        ))
    # Content-Length is a promise the server may not keep, so it is a cheap early refusal and
    # the real check happens again on the bytes we actually received.
    declared = r.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise VideoError(too_big(int(declared), max_bytes))
    data = r.content
    if not data:
        raise VideoError(i18n.pick_now("The video server returned an empty file.", "视频服务返回了空文件。"))
    if len(data) > max_bytes:
        raise VideoError(too_big(len(data), max_bytes))
    return data


# ------------------------------------------------------------------ MetaChat's open media API
#
# Reading the reply envelope (`media.meta_ok` / `meta_said`) and deciding which job a model is
# (`media.job_of`) live in `media`, because the image tool talks to the same service in the same
# words. What stays here is the video half: submit, poll, and fetch the clip.
async def _meta_submit(prov: dict, payload: dict, *, client: httpx.AsyncClient | None = None,
                       job: dict | None = None) -> str:
    job = job or media.job_of(str(payload.get("model") or ""), "video")
    url = media.api_url(prov["base_url"], job["submit"])
    async with _client(client, SUBMIT_TIMEOUT, url) as c:
        try:
            r = await c.post(url, headers=media.auth_headers(prov.get("api_key", "")), json=payload,
                             timeout=SUBMIT_TIMEOUT)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Could not reach MetaChat's video service at {prov['base_url']}: {type(e).__name__}: {e}",
                f"连不上 MetaChat 的视频服务 {prov['base_url']}:{type(e).__name__}: {e}",
            )) from None
    if r.status_code in (401, 403):
        raise VideoError(i18n.pick_now(
            "MetaChat rejected the key, so nothing was generated. Check the API key on that provider.",
            "MetaChat 认为密钥无效,没有生成。请检查那个服务商里的 API Key。",
        ))
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"MetaChat refused the request ({_why(r)})", f"MetaChat 拒绝了这次请求({_why(r)})"
        ))
    try:
        body = r.json()
    except ValueError:
        raise VideoError(i18n.pick_now(
            "MetaChat did not return JSON, so the job could not be read. Check that the address is "
            "https://api.mmchat.xyz/open/v1.",
            "MetaChat 没有返回 JSON,读不到任务。请检查地址是不是 https://api.mmchat.xyz/open/v1。",
        )) from None
    if not media.meta_ok(body):
        raise VideoError(i18n.pick_now(
            f"MetaChat did not accept the job ({media.meta_said(body)})",
            f"MetaChat 没有接受这次生成({media.meta_said(body)})",
        ))
    data = (body or {}).get("data") or {}
    vid = str((data or {}).get("id") or "").strip()
    if not vid:
        raise VideoError(i18n.pick_now(
            f"MetaChat accepted the job but returned no task id ({str(body)[:200]})",
            f"MetaChat 收下了请求但没有返回任务 id({str(body)[:200]})",
        ))
    return vid


async def _meta_status(prov: dict, vid: str, *, client: httpx.AsyncClient | None = None,
                       job: dict | None = None) -> tuple[str, dict]:
    """(state word, the inner `data` object).

    Returning `data` rather than the envelope is what lets the shared polling loop read a failure
    reason without knowing which service it is talking to: MetaChat calls it `fail_reason` where
    H3 calls it `error`, and `_generate` looks for both.

    `job` carries the model's own result path: a Grok task is polled on `video/result/{id}` and a
    Midjourney one on `midjourney/result/{id}`. Without it the generic video path is used, which is
    the right answer for a model name the user typed by hand.
    """
    job = job or media.job_of("", "video")
    path = job["result"].format(vid=urllib.parse.quote(vid, safe=""))
    url = media.api_url(prov["base_url"], path)
    async with _client(client, STATUS_TIMEOUT, url) as c:
        try:
            r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")), timeout=STATUS_TIMEOUT)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Lost contact with MetaChat while waiting: {type(e).__name__}: {e}",
                f"等待期间与 MetaChat 失去联系:{type(e).__name__}: {e}",
            )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"Could not read the generation status ({_why(r)})", f"读取生成状态失败({_why(r)})"
        ))
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not media.meta_ok(body):
        raise VideoError(i18n.pick_now(
            f"MetaChat could not report on the job ({media.meta_said(body)})",
            f"MetaChat 无法汇报任务状态({media.meta_said(body)})",
        ))
    data = (body or {}).get("data") or {}
    return str(data.get("status") or "").strip().lower(), (data if isinstance(data, dict) else {})


# ------------------------------------------------------------------ Volcengine Ark (Doubao Seedance)
async def _ark_submit(prov: dict, payload: dict, *, client: httpx.AsyncClient | None = None) -> str:
    url = media.api_url(prov["base_url"], ARK_SUBMIT)
    async with _client(client, SUBMIT_TIMEOUT, url) as c:
        try:
            r = await c.post(url, headers=media.auth_headers(prov.get("api_key", "")), json=payload,
                             timeout=SUBMIT_TIMEOUT)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Could not reach Ark at {prov['base_url']}: {type(e).__name__}: {e}",
                f"连不上火山方舟 {prov['base_url']}:{type(e).__name__}: {e}",
            )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"Ark refused the request ({_why(r)})", f"火山方舟拒绝了这次请求({_why(r)})"
        ))
    try:
        body = r.json()
    except ValueError:
        raise VideoError(i18n.pick_now(
            "Ark did not return JSON, so the job could not be read. Check that the address is "
            "https://ark.cn-beijing.volces.com/api/v3.",
            "火山方舟没有返回 JSON,读不到任务。请检查地址是不是 https://ark.cn-beijing.volces.com/api/v3。",
        )) from None
    vid = str((body or {}).get("id") or "").strip()
    if not vid:
        raise VideoError(i18n.pick_now(
            f"Ark accepted the job but returned no task id ({str(body)[:200]})",
            f"火山方舟收下了请求但没有返回任务 id({str(body)[:200]})",
        ))
    return vid


async def _ark_status(prov: dict, vid: str, *, client: httpx.AsyncClient | None = None) -> tuple[str, dict]:
    """(state word, the whole task object).

    The whole object, not a plucked field, because the finished clip is *inside* it
    (`content.video_url`) and so is what the job cost (`usage`). `link_of` knows where to look.
    """
    path = ARK_TASK.format(vid=urllib.parse.quote(vid, safe=""))
    url = media.api_url(prov["base_url"], path)
    async with _client(client, STATUS_TIMEOUT, url) as c:
        try:
            r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")), timeout=STATUS_TIMEOUT)
        except httpx.HTTPError as e:
            raise VideoError(i18n.pick_now(
                f"Lost contact with Ark while waiting: {type(e).__name__}: {e}",
                f"等待期间与火山方舟失去联系:{type(e).__name__}: {e}",
            )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"Could not read the generation status ({_why(r)})", f"读取生成状态失败({_why(r)})"
        ))
    try:
        body = r.json()
    except ValueError:
        body = {}
    return str((body or {}).get("status") or "").strip().lower(), (body if isinstance(body, dict) else {})


def too_big(size: int, max_bytes: int) -> str:
    return i18n.pick_now(
        f"The video is {size / 1024 / 1024:.0f} MB, over the {max_bytes / 1024 / 1024:.0f} MB cap "
        "in Permissions & control, so it was not saved.",
        f"视频有 {size / 1024 / 1024:.0f} MB,超过「权限与操控」里设定的 {max_bytes / 1024 / 1024:.0f} MB 上限,没有保存。",
    )


async def probe(prov: dict, *, timeout: float = 5.0, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Is something speaking the video API at this address? Renders nothing.

    `/health` first (that is what SGLang answers), then a request for a task id that cannot
    exist — a live server replies 404, which still proves it is up. Any HTTP answer counts as
    reachable; only a connection failure is a failure. This costs one request where a real
    generation costs minutes of GPU time, which is why the UI offers it before anything runs.
    """
    own = client is None
    c = client or net.client(prov.get("base_url") or "", timeout=timeout)
    try:
        return await _probe(prov, c)
    finally:
        if own:
            await c.aclose()


async def _probe(prov: dict, c: httpx.AsyncClient) -> tuple[bool, str]:
    base = (prov.get("base_url") or "").strip()
    if not base:
        return False, i18n.pick_now("This provider has no address configured.", "这个服务商没有填地址。")
    if prov.get("kind") == META_KIND:
        return await _probe_meta(prov, c)
    if prov.get("kind") == ARK_KIND:
        return await _probe_ark(prov, c)
    key = prov.get("api_key", "")
    try:
        r = await c.get(media.api_url(base, "health"), headers=media.auth_headers(key))
        if r.status_code < 400:
            return True, i18n.pick_now(
                f"{base} answered /health ({r.status_code}).", f"{base} 的 /health 有响应({r.status_code})。"
            )
    except httpx.HTTPError:
        pass
    try:
        r = await c.get(media.api_url(base, "v1/videos/team-agent-probe"), headers=media.auth_headers(key))
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"Could not reach {base}: {type(e).__name__}: {e}", f"连不上 {base}:{type(e).__name__}: {e}"
        )
    if r.status_code in (401, 403):
        return False, i18n.pick_now(
            f"{base} is up but rejected the key ({r.status_code}).", f"{base} 是活的,但密钥被拒绝了({r.status_code})。"
        )
    if r.status_code == 404:
        return True, i18n.pick_now(
            f"{base} is reachable: it answered 404 for a task id that does not exist.",
            f"{base} 可以联通:对不存在的任务 id 返回了 404。",
        )
    if r.status_code < 400:
        return True, i18n.pick_now(
            f"{base} answered the video API with {r.status_code}.", f"{base} 的视频接口有响应({r.status_code})。"
        )
    return False, i18n.pick_now(f"{base} answered {_why(r)}", f"{base} 返回了 {_why(r)}")


async def _probe_ark(prov: dict, c: httpx.AsyncClient) -> tuple[bool, str]:
    """Is Ark there, and does it accept this key? Renders nothing.

    Asking about a task id that cannot exist. Measured against the live service, a key Ark will not
    accept comes back **HTTP 401** with `{"error": {"code": "AuthenticationError", "message": "The
    API key format is incorrect..."}}`, so 401/403 is the one answer that means "something is
    really wrong". Anything else — a 404 for a task that does not exist, a 400 for a malformed id —
    proves the request was authenticated and processed, which is all this probe claims.

    The same lesson as `media.probe_meta`, learned the hard way there: when the verdict depends on
    the service's failure convention, assert the convention actually observed rather than assuming
    that "4xx means broken".
    """
    base = (prov.get("base_url") or "").strip()
    url = media.api_url(base, ARK_TASK.format(vid="team-agent-probe"))
    try:
        r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")))
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"Could not reach {base}: {type(e).__name__}: {e}", f"连不上 {base}:{type(e).__name__}: {e}"
        )
    if r.status_code in (401, 403):
        return False, i18n.pick_now(
            f"{base} is up but rejected the key ({r.status_code}). Create the key in the Ark console "
            "(API Key management) and make sure it belongs to the same region as this address.",
            f"{base} 是活的,但密钥被拒绝了({r.status_code})。请在方舟控制台的「API Key 管理」里创建密钥,"
            "并确认它与这个地址属于同一个区域。",
        )
    said = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            err = body.get("error")
            said = " ".join(str(err.get("message") if isinstance(err, dict) else err or "").split())[:160]
    except ValueError:
        said = " ".join((r.text or "").split())[:160]
    tail = f" · {said}" if said else ""
    if r.status_code >= 400:
        return True, i18n.pick_now(
            f"{base} accepted the key: it answered {r.status_code} for a task that does not exist{tail}.",
            f"{base} 接受了密钥:对不存在的任务返回了 {r.status_code}{tail}。",
        )
    return True, i18n.pick_now(
        f"{base} accepted the key and answered {r.status_code}{tail}.",
        f"{base} 接受了密钥,返回 {r.status_code}{tail}。",
    )


async def _probe_meta(prov: dict, c: httpx.AsyncClient) -> tuple[bool, str]:
    """Is MetaChat's media API there and does it take this key? `media.probe_meta` owns the answer.

    It lives next to the envelope readers because the *image* tool asks exactly the same question
    about exactly the same host, and one key serving two APIs should not produce two verdicts.
    """
    base = (prov.get("base_url") or "").strip()
    return await media.probe_meta(base, prov.get("api_key", ""), client=c)


def save(data: bytes, workspace: Path, prompt: str, vid: str) -> Path:
    """Write the mp4 into the group's workspace, and nowhere else.

    The writing itself lives in `media.save_bytes`, shared with image generation: it is the
    part with the three symlink-safety properties, and two copies of that would be two chances
    to get it subtly wrong.
    """
    try:
        return media.save_bytes(data, workspace, subdir="video", ext=".mp4",
                                stem_source=prompt, fallback=vid or "clip",
                                what="clip", what_zh="视频")
    except ValueError as e:
        raise VideoError(str(e)) from None


async def generate(
    prov: dict, payload: dict, *, workspace: Path, max_bytes: int, deadline_s: float,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Submit, wait, download, save. Raises VideoError with a message fit to show the user."""
    own = client is None
    # One client for the whole run: polling is many requests, and a fresh pool per poll would be
    # both wasteful and slow. Its proxy policy is chosen from the provider's address, which is where
    # every request in the run goes except the last one (MetaChat's object-store link, also remote).
    c = client or net.client(prov.get("base_url") or "", timeout=DOWNLOAD_TIMEOUT)
    try:
        return await _generate(prov, payload, workspace=workspace, max_bytes=max_bytes,
                               deadline_s=deadline_s, client=c)
    finally:
        if own:
            await c.aclose()


async def _generate(
    prov: dict, payload: dict, *, workspace: Path, max_bytes: int, deadline_s: float,
    client: httpx.AsyncClient,
) -> dict:
    t0 = time.time()
    # Which job this is — paths, parameters, where the file ends up — is per model on MetaChat and
    # uniform on H3, so it is resolved once here and handed to every step of the run.
    job = media.job_of(str(payload.get("model") or ""), "video") if prov.get("kind") == META_KIND else None
    vid = await submit(prov, payload, client=client, job=job)
    deadline = time.monotonic() + max(MIN_DEADLINE, deadline_s)
    interval = POLL_START
    detail: dict = {}
    while True:
        raw, detail = await status_of(prov, vid, client=client, job=job)
        if raw in _DONE:
            break
        if raw in _FAILED:
            # `fail_reason` is MetaChat's name for it, `error` is H3's, and Ark nests an
            # `{"code","message"}` object under `error`; all of them end up in the message rather
            # than being flattened into a bare "failed".
            err = detail.get("error")
            nested = err.get("message") if isinstance(err, dict) else ""
            why = " ".join(str(nested or detail.get("error") or detail.get("fail_reason")
                               or detail.get("message") or "").split())[:300]
            raise VideoError(i18n.pick_now(
                f"The video server reported failure{': ' + why if why else ''}",
                f"视频服务报告生成失败{':' + why if why else ''}",
            ))
        if time.monotonic() >= deadline:
            last = raw or i18n.pick_now("no status", "没有状态")
            raise VideoError(i18n.pick_now(
                f"Gave up after {int(deadline_s)}s: the video was still not ready (last status "
                f"\"{last}\"). Raise the limit under Permissions & control if this machine is "
                "just slow.",
                f"等了 {int(deadline_s)} 秒仍未完成(最后状态「{last}」),已放弃。如果只是机器慢,"
                "可以在「权限与操控」里把时限调大。",
            ))
        await asyncio.sleep(interval)
        interval = min(POLL_MAX, interval * 1.5)
    # The field the finished clip is in comes from the model's own job description: `video_url`
    # for every MetaChat family today, but a table that says so is cheaper than a guess that
    # breaks. Ark nests it, and `link_of` is the one place that knows.
    field = (job or {}).get("file", "video_url")
    data = await download(prov, vid, max_bytes=max_bytes, client=client,
                          url=link_of(prov.get("kind") or "", detail, field))
    path = save(data, workspace, str(payload.get("prompt", "")), vid)
    return {
        "id": vid, "path": path, "name": path.name, "bytes": len(data),
        "seconds": round(time.time() - t0, 1), "status": detail,
        # MetaChat bills in its own points and reports the total; passing it along lets the tool
        # tell the user what the clip actually cost, which is not something to hide. Ark reports
        # token usage in the same place — a different unit, so the tool names it separately.
        "points": detail.get("total_points"),
        "usage": detail.get("usage"),
    }
