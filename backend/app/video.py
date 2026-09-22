"""Video generation through a self-hosted MiniMax H3 server **or** MetaChat's open media API.

Two shapes live here because they disagree about every parameter, and a request validated
against the other shape's list would be rejected locally for a value the server accepts:

    self-hosted H3 (kind `minimax_video`)
        POST {base}/v1/videos              -> {"id": "..."}          submit
        GET  {base}/v1/videos/{id}         -> {"status": "..."}      poll
        GET  {base}/v1/videos/{id}/content -> the mp4 bytes          download

    MetaChat open media API (kind `metachat_video`; `{base}` is `https://api.mmchat.xyz/open/v1`)
        POST {base}/video/generate         -> {"data": {"id": ...}}  submit
        GET  {base}/video/result/{id}      -> {"data": {...}}        poll
        GET  the `video_url` it reports    -> the mp4 bytes          download

Three things about the MetaChat one are worth stating up front, because they are the API's
properties and not ours to paper over:

- **Both of its models are image-to-video.** `grok-imagine-video-1.5-preview` says so in as many
  words ("必须提交 1 张首帧参考图"), and `mj-video-v1` generates from a keyframe too. There is no
  text-only path to offer, so the tool is not going to pretend there is one.
- **The reference image must be an http(s) URL**, because MetaChat's servers fetch it. A path
  inside the workspace cannot be sent — H3 reads local files, this one cannot — so a local frame
  is refused with the reason rather than uploaded somewhere behind the user's back.
- **It has no model-listing endpoint** (see `media.BUILTIN_MEDIA_MODELS`), so the model name is a
  setting rather than something discovered.

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
import contextlib
import json
import re
import time
import urllib.parse
from pathlib import Path

import httpx

from . import i18n, media
from .coderun import inside as _inside     # one implementation of "is this still inside the workspace"

# Every provider kind that is a generator rather than a chat model, re-exported from `media`:
# `store.list_models()` filters by this, so a member can never be pointed at one.
MEDIA_KINDS: tuple[str, ...] = media.MEDIA_KINDS
# ...while this module only drives its own kinds. The two lists were the same thing until a
# second generator existed; picking a provider by the union would let the video tool select an
# image provider, which surfaces as a broken server rather than a wrong lookup.
KINDS: tuple[str, ...] = ("minimax_video", "metachat_video")

H3_KIND = "minimax_video"
META_KIND = "metachat_video"

# What each shape accepts. Kept per kind rather than as one shared list: the three parameters
# disagree, and a single union would let the tool send MetaChat a pixel count it does not take,
# or H3 a named resolution it has never heard of.
SHAPES: dict[str, dict] = {
    "minimax_video": {
        "seconds": (4, 15),                                             # H3's own range
        "ratios": ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
        "default_ratio": "16:9",
        "model": False,      # the server has one checkpoint; no model id in the request
    },
    "metachat_video": {
        "seconds": (1, 15),                                             # documented as 1-15, 5 by default
        "ratios": ("adaptive", "16:9", "9:16", "4:3", "3:4", "1:1", "3:2", "2:3"),
        "default_ratio": "adaptive",   # follows the reference image, which is the only input it has
        "model": True,       # every call names the model: the same host serves two of them
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
DEFAULT_ASPECT = "16:9"     # what both shapes accept, and what H3 defaults to

# MetaChat's open media API. `{base}` is the API prefix MetaChat's documentation gives for it —
# `https://api.mmchat.xyz/open/v1` — so these paths are relative to that and not to the host, which
# is what the user is told to paste into the provider's address field.
#
# `video/result/{id}` is the poll, and `/video/fetch` (which reads like the obvious name) answers 404.
META_GENERATE = "video/generate"
META_RESULT = "video/result/{vid}"
# The only two values that API takes. A named resolution rather than a pixel count, so the user's
# "output short edge" is mapped onto them instead of duplicated as a second setting — at or below
# this pivot it is the smaller file.
META_RESOLUTIONS = ("480p", "720p")
META_RESOLUTION_PIVOT = 640
# MetaChat also reports what a job cost, in its own points. Worth showing: the price is real.
META_MODELS: tuple[str, ...] = media.BUILTIN_MEDIA_MODELS[META_KIND]

# Status words. Anything that is neither done nor failed counts as "still working" and the
# deadline decides when to stop, so a server that invents a new word for "queued" does not make
# us fail instantly.
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
def pick_provider(store, cfg: dict) -> tuple[dict | None, str]:
    """(the provider to generate with, why there is none).

    A configured id wins; otherwise the first usable one. An id that no longer exists is
    reported rather than silently replaced, because quietly generating on a different machine
    than the user asked for is exactly the kind of surprise this file should not produce.
    """
    # Not `media.providers_of_kind`: a gateway whose kind is "chat" still belongs here when its
    # own model list says it serves video models (see Store.providers_for_use).
    rows = store.providers_for_use("video", KINDS)
    wanted = str(cfg.get("video_provider_id") or "").strip()
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
            "\"MetaChat video (open media API)\" and paste your key — its two models are "
            "image-to-video, no GPU involved — or add \"MiniMax H3 (self-hosted video)\" and point "
            "it at your SGLang / vLLM server.",
            "视频生成已开启,但还没有添加视频服务商。要么添加「MetaChat 视频(开放媒体接口)」并填上密钥"
            "(它的两个模型都是图生视频,不需要显卡),要么添加「MiniMax H3(自建视频生成)」"
            "并填上你的 SGLang / vLLM 服务地址。",
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


def metachat_payload(
    prompt: str, *, model: str, ratio: str, duration_seconds: int, short_edge: int, frame: str = "",
) -> dict:
    """The body `POST open/v1/video/generate` documents.

    `prompt`, `model` and `params` are all it defines; a reference image goes in `images` as a URL
    the service fetches itself. One image, not two: both of its models generate *from a keyframe*,
    so there is no last-frame slot — `_generate_video` refuses a `last_frame` rather than dropping
    it, because a member that asked for one and silently did not get it would draw the wrong
    conclusion from the result.

    Of the fields the documentation's example shows, only `url` is sent: `type`, `size`, `w` and
    `h` describe the caller's own copy of the picture, and filling them in for a URL we were
    handed would be making up numbers.
    """
    return {
        "prompt": prompt,
        "model": model,
        "params": {
            "duration": int(duration_seconds),
            "ratio": ratio,
            "resolution": resolution_for(short_edge),
        },
        "images": [{"url": frame}] if frame else [],
    }


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
async def _client(client: httpx.AsyncClient | None, timeout: float):
    """The client for one request, when the caller did not bring its own.

    `trust_env=False` on the one we make: a video server usually sits on the local network, and a
    system proxy would quietly send that traffic somewhere else.
    """
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
        yield c


async def submit(prov: dict, payload: dict, *, client: httpx.AsyncClient | None = None) -> str:
    if prov.get("kind") == META_KIND:
        return await _meta_submit(prov, payload, client=client)
    url = media.api_url(prov["base_url"], "v1/videos")
    async with _client(client, SUBMIT_TIMEOUT) as c:
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


async def status_of(prov: dict, vid: str, *, client: httpx.AsyncClient | None = None) -> tuple[str, dict]:
    if prov.get("kind") == META_KIND:
        return await _meta_status(prov, vid, client=client)
    url = media.api_url(prov["base_url"], f"v1/videos/{vid}")
    async with _client(client, STATUS_TIMEOUT) as c:
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

    Which address they come from differs: H3 serves them off its own task id, while MetaChat
    returns a link into its object store — so `url` is the caller's answer for that shape, and
    guessing any other endpoint would be inventing one. The checks afterwards are the same for
    both, which is the part worth not writing twice.
    """
    if prov.get("kind") == META_KIND:
        if not url:
            raise VideoError(i18n.pick_now(
                "MetaChat reported the job as finished but gave no video address, so nothing was "
                "saved. This is worth reporting to them rather than retrying.",
                "MetaChat 报告任务已完成,但没有给出视频地址,所以没有保存。这种情况建议反馈给 MetaChat,重试没有意义。",
            ))
        # Deliberately no Authorization header: the link points at MetaChat's object storage
        # (an aliyuncs host), and the key is for MetaChat alone. Sending it there would hand the
        # credential to a third party for a file that does not need it.
        headers: dict[str, str] = {}
        target = url
        timeout = DOWNLOAD_TIMEOUT
    else:
        headers = media.auth_headers(prov.get("api_key", ""))
        target = media.api_url(prov["base_url"], f"v1/videos/{vid}/content")
        timeout = DOWNLOAD_TIMEOUT
    async with _client(client, timeout) as c:
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
def _meta_said(body: object) -> str:
    """The sentence out of MetaChat's envelope.

    It answers 200 even when it refuses, putting the verdict in `status` and the reason in
    `message`, so `_why` (which reads a status code) has nothing to report and the body is the
    only place the real reason exists.
    """
    if isinstance(body, dict):
        said = " ".join(str(body.get("message") or "").split())
        return said[:300] or str(body)[:200]
    return str(body)[:200]


def _meta_ok(body: object) -> bool:
    """`status: "Success"` means the call worked. An absent one is treated as success: refusing to
    parse a body that simply does not carry the field would fail over a cosmetic difference."""
    if not isinstance(body, dict):
        return False
    return str(body.get("status") or "").strip().lower() in ("", "success")


async def _meta_submit(prov: dict, payload: dict, *, client: httpx.AsyncClient | None = None) -> str:
    url = media.api_url(prov["base_url"], META_GENERATE)
    async with _client(client, SUBMIT_TIMEOUT) as c:
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
    if not _meta_ok(body):
        raise VideoError(i18n.pick_now(
            f"MetaChat did not accept the job ({_meta_said(body)})",
            f"MetaChat 没有接受这次生成({_meta_said(body)})",
        ))
    data = (body or {}).get("data") or {}
    vid = str((data or {}).get("id") or "").strip()
    if not vid:
        raise VideoError(i18n.pick_now(
            f"MetaChat accepted the job but returned no task id ({str(body)[:200]})",
            f"MetaChat 收下了请求但没有返回任务 id({str(body)[:200]})",
        ))
    return vid


async def _meta_status(prov: dict, vid: str, *, client: httpx.AsyncClient | None = None) -> tuple[str, dict]:
    """(state word, the inner `data` object).

    Returning `data` rather than the envelope is what lets the shared polling loop read a failure
    reason without knowing which service it is talking to: MetaChat calls it `fail_reason` where
    H3 calls it `error`, and `_generate` looks for both.
    """
    path = META_RESULT.format(vid=urllib.parse.quote(vid, safe=""))
    url = media.api_url(prov["base_url"], path)
    async with _client(client, STATUS_TIMEOUT) as c:
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
    if not _meta_ok(body):
        raise VideoError(i18n.pick_now(
            f"MetaChat could not report on the job ({_meta_said(body)})",
            f"MetaChat 无法汇报任务状态({_meta_said(body)})",
        ))
    data = (body or {}).get("data") or {}
    return str(data.get("status") or "").strip().lower(), (data if isinstance(data, dict) else {})


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
    c = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
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


async def _probe_meta(prov: dict, c: httpx.AsyncClient) -> tuple[bool, str]:
    """Is MetaChat's media API there, and does it accept this key? Renders nothing.

    Asking about a task id that cannot exist: that API has no `/health` and no listing to read, and
    it answers a missing task in its own 200-with-`Fail` envelope. A 401/403 is the one answer that
    means something is really wrong (the key), and anything else proves the service answered.
    """
    base = (prov.get("base_url") or "").strip()
    url = media.api_url(base, META_RESULT.format(vid="team-agent-probe"))
    try:
        r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")))
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"Could not reach {base}: {type(e).__name__}: {e}", f"连不上 {base}:{type(e).__name__}: {e}"
        )
    if r.status_code in (401, 403):
        return False, i18n.pick_now(
            f"{base} is up but rejected the key ({r.status_code}). MetaChat's key is created under "
            "your avatar → API management; the media API uses the same one.",
            f"{base} 是活的,但密钥被拒绝了({r.status_code})。MetaChat 的密钥在头像 → API 管理里创建,"
            "媒体接口用的是同一把。",
        )
    if r.status_code == 404:
        return True, i18n.pick_now(
            f"{base} is reachable: it answered 404 for a task id that does not exist.",
            f"{base} 可以联通:对不存在的任务 id 返回了 404。",
        )
    if r.status_code < 400:
        return True, i18n.pick_now(
            f"{base} answered the media API with {r.status_code}.", f"{base} 的媒体接口有响应({r.status_code})。"
        )
    return False, i18n.pick_now(f"{base} answered {_why(r)}", f"{base} 返回了 {_why(r)}")


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
    # both wasteful and slow.
    c = client or httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, trust_env=False)
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
    vid = await submit(prov, payload, client=client)
    deadline = time.monotonic() + max(MIN_DEADLINE, deadline_s)
    interval = POLL_START
    detail: dict = {}
    while True:
        raw, detail = await status_of(prov, vid, client=client)
        if raw in _DONE:
            break
        if raw in _FAILED:
            # `fail_reason` is MetaChat's name for it, `error` is H3's; both end up in the message
            # rather than being flattened into a bare "failed".
            why = " ".join(str(detail.get("error") or detail.get("fail_reason")
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
    data = await download(prov, vid, max_bytes=max_bytes, client=client,
                          url=str(detail.get("video_url") or ""))
    path = save(data, workspace, str(payload.get("prompt", "")), vid)
    return {
        "id": vid, "path": path, "name": path.name, "bytes": len(data),
        "seconds": round(time.time() - t0, 1), "status": detail,
        # MetaChat bills in its own points and reports the total; passing it along lets the tool
        # tell the user what the clip actually cost, which is not something to hide.
        "points": detail.get("total_points"),
    }
