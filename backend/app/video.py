"""Video-and-audio generation through a self-hosted MiniMax H3 server.

H3-Base is a diffusion transformer, not a language model. It has no chat completions endpoint
and cannot be a member's model, so it never appears in the model list (see `MEDIA_KINDS`); it
answers a video API instead, and this module is the only place that talks to it:

    POST {base}/v1/videos            -> {"id": "..."}   submit
    GET  {base}/v1/videos/{id}       -> {"status": "..."}   poll
    GET  {base}/v1/videos/{id}/content -> the mp4 bytes     download

Two things the server will *not* do for us, and which therefore shape this file:

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
KINDS: tuple[str, ...] = ("minimax_video",)

# H3's own output range. A request outside it is clamped rather than refused, and the clamping is
# reported back so the caller is not surprised by a 4-second clip.
MIN_SECONDS, MAX_SECONDS = 4, 15
ASPECT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
DEFAULT_ASPECT = "16:9"

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
            "Video generation is on, but no video provider has been added yet. Add "
            "\"MiniMax H3 (self-hosted video)\" under model providers and point it at your "
            "SGLang / vLLM server.",
            "视频生成已开启,但还没有添加视频服务商。请在「模型服务商」里添加"
            "「MiniMax H3(自建视频生成)」并填上你的 SGLang / vLLM 服务地址。",
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


def clamp_seconds(want: object, cap: int) -> tuple[int, bool]:
    """(seconds to use, whether the request had to be clamped)."""
    try:
        n = int(want) if want not in (None, "") else MAX_SECONDS
    except (TypeError, ValueError):
        n = MAX_SECONDS
    lo, hi = MIN_SECONDS, max(MIN_SECONDS, min(MAX_SECONDS, int(cap)))
    used = max(lo, min(hi, n))
    return used, used != n


# ------------------------------------------------------------------ HTTP
async def submit(prov: dict, payload: dict, *, client: httpx.AsyncClient | None = None) -> str:
    url = media.api_url(prov["base_url"], "v1/videos")
    try:
        if client is not None:
            r = await client.post(url, headers=media.auth_headers(prov.get("api_key", "")), json=payload,
                                  timeout=SUBMIT_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT, trust_env=False) as c:
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
    url = media.api_url(prov["base_url"], f"v1/videos/{vid}")
    try:
        if client is not None:
            r = await client.get(url, headers=media.auth_headers(prov.get("api_key", "")), timeout=STATUS_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=STATUS_TIMEOUT, trust_env=False) as c:
                r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")))
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


async def download(prov: dict, vid: str, *, max_bytes: int, client: httpx.AsyncClient | None = None) -> bytes:
    url = media.api_url(prov["base_url"], f"v1/videos/{vid}/content")
    try:
        if client is not None:
            r = await client.get(url, headers=media.auth_headers(prov.get("api_key", "")), timeout=DOWNLOAD_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, trust_env=False) as c:
                r = await c.get(url, headers=media.auth_headers(prov.get("api_key", "")))
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
            why = " ".join(str(detail.get("error") or detail.get("message") or "").split())[:300]
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
    data = await download(prov, vid, max_bytes=max_bytes, client=client)
    path = save(data, workspace, str(payload.get("prompt", "")), vid)
    return {
        "id": vid, "path": path, "name": path.name, "bytes": len(data),
        "seconds": round(time.time() - t0, 1), "status": detail,
    }
