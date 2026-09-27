"""Image generation through an OpenAI-compatible images endpoint, **or** MetaChat's media API.

The first is the one media capability that works against a *gateway* rather than a self-hosted
server: `POST {base}/images/generations` with a model name is what OpenAI serves, what MetaChat
serves on its OpenAI-compatible address (GPT-Image), and what most aggregators implement — so one
implementation reaches all of them. A gateway is also the cheapest way in: a key and a model name,
no GPU.

The second is `metachat_media`: MetaChat's own image API, which is a *job* rather than a reply —
submit, poll, then download the picture from a link. It is the only way to reach the drawing models
that are not on its OpenAI-compatible address (Midjourney, FLUX, Seedream, Z-Image, Grok Image), and
its paths and parameters differ per model, so they are described in `media.MEDIA_MODELS` rather than
assumed here.

Three platform facts shape the first path:

* **The response is either base64 or a URL.** OpenAI's newer image models always inline the
  bytes; older and third-party ones return a temporary URL. Both are accepted, and the URL
  case is downloaded immediately — those links expire within minutes.
* **`response_format` is not portable.** Sending it makes some models reject the request
  outright, so it is never sent; whatever comes back decides which branch runs.
* **Failures arrive as HTTP errors with a JSON body**, and the useful ones (bad key, unknown
  model, unsupported size, rate limit) each need a different fix. `explain` names it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import time
import urllib.parse
from pathlib import Path

import httpx
from PIL import Image

from . import i18n, media, net
from .media import offline_reason
from .provider_errors import quota_exhausted

# The provider kinds this module can drive. A subset of `media.MEDIA_KINDS`: the video tool
# must never pick an image provider, and the other way round. `metachat_media` is in both
# subsets because its one key reaches an image API *and* a video API.
KINDS: tuple[str, ...] = ("openai_image", "metachat_media")

# What OpenAI's image models accept. A gateway may support more, but offering a size the
# model then refuses produces a paid failure, so the list stays conservative.
SIZES = ("1024x1024", "1536x1024", "1024x1536")
DEFAULT_SIZE = "1024x1024"
DEFAULT_MODEL = "gpt-image-1"

# What a picture prompt has to contain, for the step that writes one out of a group's conversation
# (`orchestrator._media_prompt`). The opposite of a chat answer: no explanation, no request.
PROMPT_NOTE = ("Describe the picture itself — subject, composition, style, lighting, and any text "
               "that must appear in it.")
PROMPT_NOTE_ZH = "描述画面本身 —— 主体、构图、风格、光线,以及必须出现在画面里的文字。"

SUBMIT_TIMEOUT = 120.0
DOWNLOAD_TIMEOUT = 120.0

# MetaChat's API takes a named aspect ratio rather than a pixel size, so our three sizes are mapped
# onto the ratios every one of its families documents. A second setting saying almost the same
# thing would be a setting to keep in sync, and it would still have to decide which one applies.
META_ASPECTS = {"1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3"}

# Status words of a media job. Anything else means "still working" and the deadline decides.
_DONE = {"completed", "complete", "succeeded", "success", "done", "finished"}
_FAILED = {"failed", "failure", "error", "cancelled", "canceled", "expired", "rejected"}

POLL_START, POLL_MAX = 2.0, 10.0
MIN_DEADLINE = 5.0

# A refusal is a sentence, not a document. The cap exists so an error body cannot be unbounded
# either — the size of a *reply* is a different question from the size of an *image*.
ERROR_BODY_CAP = 64 * 1024
REFERENCE_COUNT = 4
REFERENCE_BYTES = 20 * 1024 * 1024


class ImageError(Exception):
    """A failure worth showing the user: the message is already in the request language."""


# ------------------------------------------------------------------ providers
def pick_provider(store, cfg: dict, prefer: str = "") -> tuple[dict | None, str]:
    """(the provider to generate with, why there is none).

    `prefer` is the provider a *media member* was created from, and it outranks the setting: a
    member called "Seedream" must draw with Seedream whatever the group's last global choice was.
    An id that no longer exists is reported rather than silently replaced, since quietly generating
    on somebody else's account is exactly the surprise this file should not produce.
    """
    # Not `media.providers_of_kind`: a gateway whose kind is "chat" still belongs here when its
    # own model list says it serves image models (see Store.providers_for_use).
    rows = store.providers_for_use("image", KINDS)
    wanted = str(prefer or cfg.get("image_provider_id") or "").strip()
    if wanted:
        p = next((x for x in rows if x["id"] == wanted), None)
        if p is None:
            return None, i18n.pick_now(
                f"Image generation is set to use \"{wanted}\", but no image provider with that id exists any more.",
                f"绘画指定使用「{wanted}」,但已经找不到这个 id 的图片服务商了。",
            )
        if not p["enabled"]:
            return None, i18n.pick_now(f"Image provider \"{p['name']}\" is switched off.",
                                       f"图片服务商「{p['name']}」已停用。")
        if not (p["base_url"] or "").strip():
            return None, i18n.pick_now(f"Image provider \"{p['name']}\" has no address configured.",
                                       f"图片服务商「{p['name']}」没有填地址。")
        return p, ""
    usable = [p for p in rows if p["enabled"] and (p["base_url"] or "").strip()]
    if not usable:
        return None, i18n.pick_now(
            "Image generation is on, but nothing here can draw yet. Add an "
            "\"Image generation (OpenAI-compatible)\" provider — any endpoint serving "
            "/images/generations will do — or refresh the model list of a gateway that serves image "
            "models; one is offered for drawing as soon as its list is known.",
            "绘画已开启,但还没有能画的服务商。可以添加一个「绘画(OpenAI 兼容)」服务商"
            "(任何提供 /images/generations 的地址都可以),或者刷新某个网关的模型列表——"
            "只要它提供绘画模型,刷新后就会自动出现在绘画服务里。",
        )
    return usable[0], ""


def blocked_by_offline(provider: dict, cfg: dict) -> str:
    return offline_reason(provider, cfg, "image", "图片")


# -------------------------------------------------------------------- request
def build_payload(prompt: str, *, model: str, size: str) -> dict:
    """The request body for an OpenAI-compatible images endpoint.
    `response_format` is deliberately absent — see the module docstring."""
    return {"model": (model or DEFAULT_MODEL).strip(), "prompt": prompt.strip(),
            "n": 1, "size": size}


def read_references(workspace: Path, paths: object) -> list[dict]:
    """Read only explicitly selected workspace images; never publish files to a hosting service."""
    if not isinstance(paths, list) or not 1 <= len(paths) <= REFERENCE_COUNT:
        raise ImageError(i18n.pick_now("reference_images must contain 1–4 workspace image paths.",
                                       "reference_images 必须包含 1–4 个本群图片路径。"))
    root = workspace.resolve()
    result, total = [], 0
    formats = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise ImageError("reference_images: invalid image path")
        path = (root / value).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ImageError(i18n.pick_now(f"Reference is not a file in this workspace: {value}",
                                           f"参考图不是本群工作目录内的文件：{value}"))
        try:
            # Bound the read itself, not just stat(), which can race a writer.
            with path.open("rb") as stream:
                data = stream.read(REFERENCE_BYTES - total + 1)
            total += len(data)
            if total > REFERENCE_BYTES:
                raise ImageError(i18n.pick_now("Reference images exceed the 20 MB total limit.",
                                               "参考图合计超过 20 MB 上限。"))
            with Image.open(io.BytesIO(data)) as picture:
                mime = formats.get(picture.format)
                if not mime or picture.width * picture.height > 40_000_000:
                    raise ImageError(i18n.pick_now("Use PNG, JPEG or WebP references up to 40 megapixels each.",
                                                   "参考图须为 PNG、JPEG 或 WebP，单张不超过 4000 万像素。"))
                picture.verify()
        except ImageError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ImageError(i18n.pick_now(f"Cannot read reference image {value}: {exc}",
                                           f"无法读取参考图 {value}：{exc}")) from None
        result.append({"path": str(path.relative_to(root)), "name": path.name, "mime": mime,
                       "data": data, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return result


def metachat_payload(prompt: str, *, model: str, size: str) -> dict:
    """The body MetaChat's image job documents: `{prompt, model, params}`.

    Only the parameters *this* model's own page lists, and of those only the aspect ratio: the
    pixel sizes (`1MP`, `1K`…) are left at each family's documented default, because sending a
    value the model does not know is a paid failure rather than a helpful error. `num` is 1
    because one image is what gets saved.
    """
    job = media.job_of(model, "image")
    values = {"aspect": META_ASPECTS.get(size, "1:1"), "num": 1}
    return {"prompt": prompt.strip(), "model": (model or "").strip(),
            "params": {k: values[k] for k in job["params"] if k in values}}


def decode_b64(value: str) -> bytes:
    s = (value or "").strip()
    if s.startswith("data:"):                     # some gateways inline a data: URL
        s = s.split(",", 1)[-1]
    try:
        blob = base64.b64decode(s, validate=False)
    except Exception as e:  # noqa: BLE001
        raise ImageError(i18n.pick_now(f"the image data could not be decoded ({e})",
                                       f"图片数据无法解码({e})")) from None
    if not blob:
        # `validate=False` drops characters outside the alphabet, so a gateway that answers with
        # an error string — or with "!!!!" — decodes to nothing. Returning those bytes would save
        # a 0-byte .png and report a successful generation.
        raise ImageError(i18n.pick_now(
            "the image data was empty once decoded, so no image came back",
            "图片数据解码后是空的,也就是没有拿到图片"))
    return blob


def first_image(data: object) -> tuple[bytes | None, str]:
    """Pull `(bytes, url)` out of an images response; exactly one of the two is set."""
    if not isinstance(data, dict):
        raise ImageError(i18n.pick_now("the reply was not an image object",
                                       "返回的不是一个图片对象"))
    items = data.get("data")
    if not isinstance(items, list) or not items:
        # Some gateways report a failure with HTTP 200 and an error object.
        err = data.get("error") or data.get("message")
        if err:
            raise ImageError(str(err)[:300])
        raise ImageError(i18n.pick_now("the reply contained no image", "返回里没有图片"))
    first = items[0] if isinstance(items[0], dict) else {}
    if first.get("b64_json"):
        return decode_b64(str(first["b64_json"])), ""
    if first.get("url"):
        return None, str(first["url"])
    if first.get("image_url"):
        return None, str(first["image_url"])
    raise ImageError(i18n.pick_now("the reply contained no image data",
                                   "返回里没有图片数据"))


def explain(status: int, body: str) -> str:
    """Turn a refusal into the setting that caused it."""
    detail = ""
    try:
        obj = json.loads(body or "{}")
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or "")
            elif isinstance(err, str) and err:
                detail = err
            else:
                detail = str(obj.get("message") or "")
    except ValueError:
        detail = (body or "").strip()
    detail = re.sub(r"\s+", " ", detail)[:200]
    low = detail.lower()

    if quota_exhausted(body):
        hint = i18n.pick_now(
            "the provider reports exhausted billing quota — waiting will not refill it; top up or select another configured provider",
            "服务商账户额度不足——等待不会恢复；请补充额度或选择其他已配置服务商")
    elif status in (401, 403):
        hint = i18n.pick_now("the key was refused — check the API key saved for this provider",
                             "密钥被拒绝——请检查这个服务商保存的 API key")
    elif status == 404 or "model" in low and ("not" in low or "unknown" in low):
        hint = i18n.pick_now("the model or the address was not found — check the model name and that the address ends in /v1",
                             "模型或地址不存在——请检查模型名,以及地址是否以 /v1 结尾")
    elif status == 429:
        hint = i18n.pick_now("rate limited by the service — try again shortly",
                             "被服务方限流——稍后再试")
    elif status == 400 and ("size" in low or "resolution" in low):
        hint = i18n.pick_now(f"the service does not accept this size — it accepts {', '.join(SIZES)}",
                             f"服务方不接受这个尺寸——它接受 {', '.join(SIZES)}")
    elif status == 400 and ("content" in low or "safety" in low or "policy" in low):
        hint = i18n.pick_now("the prompt was refused by the service's own content policy",
                             "提示词被服务方的内容策略拒绝了")
    elif status >= 500:
        hint = i18n.pick_now("the service had a server-side problem — this is transient",
                             "服务方出错——属于暂时性问题")
    else:
        hint = i18n.pick_now("the service refused the request", "服务方拒绝了这次请求")
    msg = i18n.pick_now(f"The image service answered HTTP {status}: {hint}",
                        f"图片服务返回了 HTTP {status}:{hint}")
    return f"{msg} · {detail}" if detail else msg


def _why(status: int, body: str) -> str:
    return explain(status, body)


def _too_big(cap: int, *, image_limit: bool) -> ImageError:
    """Two different limits, two different sentences: one is the group's allowance for an image,
    the other is a reply that is not an image at all but something oversized."""
    if image_limit:
        return ImageError(i18n.pick_now(
            f"the image is larger than this group allows ({media.size_label(cap)})",
            f"图片超过了本群允许的大小({media.size_label(cap)})"))
    return ImageError(i18n.pick_now(
        f"the image service sent more than {media.size_label(cap)} in one reply, so it was not read "
        "(an image that size could not have been used anyway)",
        f"图片服务单次返回了超过 {media.size_label(cap)} 的内容,已停止读取(这么大的图片本来也用不上)"))


async def _read_capped(resp: httpx.Response, cap: int, *, image_limit: bool) -> bytes:
    """Read a reply, refusing to hold more than `cap` bytes.

    Reading the body and checking its length afterwards is the wrong order: by then a service
    that answers with a gigabyte has already been buffered, and the check on the *image* size
    does not bound the memory the *reply* takes. Every read in this module goes through here,
    which is also what makes the error path in `fetch` possible — `resp.text` on a streamed
    response raises before it has been read, and that used to hide the service's own message.
    """
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body.extend(chunk)
        if len(body) > cap:
            raise _too_big(cap, image_limit=image_limit)
    return bytes(body)


def _local_host(host: str) -> bool:
    """True for an address that only means something on this machine or on this network."""
    import ipaddress

    h = (host or "").strip().strip("[]").lower()
    if not h:
        return True                       # no host at all: not somewhere this could be fetched from
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False                      # a name on the internet; resolving it is DNS's business
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
                or ip.is_unspecified or ip.is_multicast)


def may_fetch(url: str, base: str) -> bool:
    """May this download go ahead? An address on the internet always may; one on this machine
    or the local network only when it is the provider's own address.

    Everywhere else in this app `http(s)` means "does not touch this machine", which is true of
    a host on the internet and false of `127.0.0.1`, `10.x` or `192.168.x`. A gateway chooses
    the URL it answers with, so without this it could read a service on this machine — or a
    neighbour on the LAN — and hand the response back as an "image". A provider that returns its
    own address (a local ComfyUI or a self-hosted server) keeps working, and so does a gateway
    that returns a CDN link, because that is neither.
    """
    try:
        host = (httpx.URL(url).host or "").lower()
        own = (httpx.URL(base).host or "").lower()
    except Exception:  # noqa: BLE001 — an unparsable address is not one to fetch from
        return False
    return (not _local_host(host)) or host == own


async def fetch(url: str, *, max_bytes: int, client: httpx.AsyncClient | None = None,
                base: str = "") -> bytes:
    """Download an image a gateway returned as a URL. Those links expire quickly, so this
    happens straight away rather than when the UI asks for it.

    `base` is the provider's own address; it is what lets a local provider's link through while
    refusing a link that points at something local from a provider that is not (see `may_fetch`).
    """
    if not may_fetch(url, base):
        try:
            shown = httpx.URL(url).host or url
        except Exception:  # noqa: BLE001
            shown = url
        raise ImageError(i18n.pick_now(
            f"the image service asked for a download from \"{shown}\", which is this machine or the "
            "local network, while the provider itself is not local. That was refused: a service "
            "should not be able to reach into your network through this app. If the provider really "
            "does run on this machine, mark it as local.",
            f"图片服务要求从「{shown}」下载图片,那是本机或局域网地址,而这个服务商并不是本地服务——已拒绝:"
            "服务方不应该通过本应用访问你的内网。如果它确实跑在本机,请把它标为本地。"))
    own = client is None
    c = client or net.client(url, timeout=DOWNLOAD_TIMEOUT)
    try:
        async with c.stream("GET", url) as resp:
            if resp.status_code >= 300:
                # The body is read here instead of through `resp.text`: on a streamed response
                # the text is not available until something has read it, and asking for it threw
                # away the service's own message in favour of an httpx error about reading.
                detail = await _read_capped(resp, ERROR_BODY_CAP, image_limit=False)
                raise ImageError(_why(resp.status_code, detail.decode("utf-8", "replace")))
            return await _read_capped(resp, max_bytes, image_limit=True)
    except ImageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ImageError(i18n.pick_now(f"the image could not be downloaded ({e})",
                                       f"图片下载失败({e})")) from None
    finally:
        if own:
            await c.aclose()


async def generate(provider: dict, payload: dict, *, max_bytes: int, deadline_s: float,
                   client: httpx.AsyncClient | None = None, reference_images: list[dict] | None = None) -> dict:
    """One image, end to end: `{data, size, seconds, url}`. Raises ImageError with a reason.

    The payload and its length are separate keys on purpose: a single `bytes` key holding the
    image itself reads as a size at every call site, and `size_label(b"...")` fails with a
    comparison error far from its cause.
    """
    if provider.get("kind") == "metachat_media":
        if reference_images:
            raise ImageError(i18n.pick_now(
                "This MetaChat media adapter has no local-image upload. Use an enabled OpenAI-compatible image member with /images/edits for workspace references. References were not discarded; no generation was submitted.",
                "当前 MetaChat 媒体适配器尚未接入本地参考图上传。请由主持选择支持 /images/edits 的已启用 OpenAI 兼容绘图成员。参考图没有被丢弃，本次未提交生成。"))
        return await _metachat_generate(provider, payload, max_bytes=max_bytes,
                                        deadline_s=deadline_s, client=client)
    url = media.api_url(provider["base_url"], "/v1/images/edits" if reference_images else "/v1/images/generations")
    base = (provider.get("base_url") or "").strip()
    cap = max(ERROR_BODY_CAP, max_bytes * 2)      # base64 inflates by a third; the envelope is small
    own = client is None
    c = client or net.client(url, timeout=SUBMIT_TIMEOUT)
    started = time.time()
    try:
        try:
            # The documented edits endpoint takes multipart image bytes. No public upload or
            # fallback to text-only generation: a rejected edit must stay a failed edit.
            body = ({"data": {k: str(v) for k, v in payload.items()},
                     "files": [("image" if len(reference_images) == 1 else "image[]",
                                (ref["name"], ref["data"], ref["mime"])) for ref in reference_images]}
                    if reference_images else {"json": payload})
            headers = media.auth_headers(provider.get("api_key") or "")
            if reference_images:
                headers.pop("Content-Type", None)  # httpx supplies the multipart boundary
            async with c.stream("POST", url, **body,
                                headers=headers,
                                timeout=max(5.0, deadline_s)) as resp:
                status = resp.status_code
                raw = await _read_capped(resp, cap if status < 300 else ERROR_BODY_CAP,
                                        image_limit=False)
        except ImageError:
            raise
        except Exception as e:  # noqa: BLE001 — the common case is an unreachable address
            raise ImageError(i18n.pick_now(
                f"could not reach the image service at {url} ({e})",
                f"连不上图片服务 {url}({e})")) from None
        if status >= 300:
            raise ImageError(_why(status, raw.decode("utf-8", "replace")))
        try:
            data = json.loads(raw)
        except ValueError:
            raise ImageError(i18n.pick_now("the image service returned something that is not JSON",
                                           "图片服务返回的不是 JSON")) from None
        blob, link = first_image(data)
        if blob is None:
            blob = await fetch(link, max_bytes=max_bytes, client=c, base=base)
    finally:
        if own:
            await c.aclose()
    if len(blob) > max_bytes:
        raise ImageError(i18n.pick_now(
            f"the image is larger than this group allows ({media.size_label(max_bytes)})",
            f"图片超过了本群允许的大小({media.size_label(max_bytes)})"))
    return {"data": blob, "size": len(blob), "seconds": time.time() - started, "url": link}


# -------------------------------------------------------------------- MetaChat's image job
async def _metachat_generate(provider: dict, payload: dict, *, max_bytes: int, deadline_s: float,
                             client: httpx.AsyncClient | None = None) -> dict:
    """One image through MetaChat's own image API: submit, wait, download the link it reports.

    A separate function rather than a branch inside `generate`: a job is a different shape from a
    reply, and the only thing the two share is what they return.
    """
    model = str(payload.get("model") or "")
    job = media.job_of(model, "image")
    base = (provider.get("base_url") or "").strip()
    own = client is None
    c = client or net.client(base, timeout=SUBMIT_TIMEOUT)
    started = time.time()
    try:
        vid = await _meta_submit(c, provider, job["submit"], payload)
        data = await _meta_wait(c, provider, job, vid, deadline_s)
        links = media.job_links(data, job["file"])
        if not links:
            raise ImageError(i18n.pick_now(
                f"MetaChat reported the job as finished but returned no picture address ({model}). "
                "This is worth reporting to them rather than retrying.",
                f"MetaChat 报告任务已完成,但没有返回图片地址({model})。这种情况建议反馈给 MetaChat,重试没有意义。"))
        # No Authorization header on this one: the link points into MetaChat's object storage, and
        # the key is for MetaChat alone. `fetch` also refuses a link that points back at this
        # machine or the local network.
        blob = await fetch(links[0], max_bytes=max_bytes, client=c, base=base)
    finally:
        if own:
            await c.aclose()
    if len(blob) > max_bytes:
        raise ImageError(i18n.pick_now(
            f"the image is larger than this group allows ({media.size_label(max_bytes)})",
            f"图片超过了本群允许的大小({media.size_label(max_bytes)})"))
    return {"data": blob, "size": len(blob), "seconds": time.time() - started, "url": links[0]}


async def _meta_submit(c: httpx.AsyncClient, provider: dict, path: str, payload: dict) -> str:
    """Hand the drawing over; returns the task id. Raises ImageError with a sentence."""
    url = media.api_url(provider["base_url"], path)
    try:
        r = await c.post(url, json=payload, headers=media.auth_headers(provider.get("api_key") or ""),
                         timeout=SUBMIT_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — the common case is an unreachable address
        raise ImageError(i18n.pick_now(
            f"could not reach MetaChat's image service at {url} ({e})",
            f"连不上 MetaChat 的图片服务 {url}({e})")) from None
    if r.status_code in (401, 403):
        raise ImageError(i18n.pick_now(
            "MetaChat rejected the key, so nothing was drawn. Check the API key on that provider.",
            "MetaChat 认为密钥无效,没有画。请检查那个服务商里的 API Key。"))
    if r.status_code >= 300:
        raise ImageError(_why(r.status_code, r.text))
    try:
        body = r.json()
    except ValueError:
        raise ImageError(i18n.pick_now(
            "MetaChat returned something that is not JSON. Check that the address is "
            "https://api.mmchat.xyz/open/v1.",
            "MetaChat 返回的不是 JSON。请检查地址是不是 https://api.mmchat.xyz/open/v1。")) from None
    if not media.meta_ok(body):
        # A refusal arrives with HTTP 200 and the verdict in the body, so the status code said
        # nothing and this is the only place the actual reason exists.
        raise ImageError(i18n.pick_now(
            f"MetaChat did not accept the drawing ({media.meta_said(body)})",
            f"MetaChat 没有接受这次绘图({media.meta_said(body)})"))
    vid = str(((body or {}).get("data") or {}).get("id") or "").strip()
    if not vid:
        raise ImageError(i18n.pick_now(
            f"MetaChat accepted the drawing but returned no task id ({str(body)[:200]})",
            f"MetaChat 收下了绘图请求但没有返回任务 id({str(body)[:200]})"))
    return vid


async def _meta_wait(c: httpx.AsyncClient, provider: dict, job: dict, vid: str,
                     deadline_s: float) -> dict:
    """Poll until the job is done; returns its `data` object. Raises ImageError on failure or
    when the deadline passes."""
    path = job["result"].format(vid=urllib.parse.quote(vid, safe=""))
    url = media.api_url(provider["base_url"], path)
    deadline = time.monotonic() + max(MIN_DEADLINE, deadline_s)
    interval = POLL_START
    while True:
        try:
            r = await c.get(url, headers=media.auth_headers(provider.get("api_key") or ""),
                            timeout=SUBMIT_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise ImageError(i18n.pick_now(
                f"lost contact with MetaChat while waiting ({e})",
                f"等待期间与 MetaChat 失去联系({e})")) from None
        if r.status_code >= 300:
            raise ImageError(_why(r.status_code, r.text))
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not media.meta_ok(body):
            raise ImageError(i18n.pick_now(
                f"MetaChat could not report on the drawing ({media.meta_said(body)})",
                f"MetaChat 无法汇报绘图状态({media.meta_said(body)})"))
        data = (body or {}).get("data") or {}
        state = str(data.get("status") or "").strip().lower()
        if state in _DONE:
            return data if isinstance(data, dict) else {}
        if state in _FAILED:
            why = " ".join(str(data.get("fail_reason") or data.get("error") or "").split())[:300]
            raise ImageError(i18n.pick_now(
                f"MetaChat reported the drawing failed{': ' + why if why else ''}",
                f"MetaChat 报告绘图失败{':' + why if why else ''}"))
        if time.monotonic() >= deadline:
            raise ImageError(i18n.pick_now(
                f"gave up after {int(deadline_s)}s: the picture was still not ready (last status "
                f"\"{state or 'none'}\"). Raise the image timeout under Permissions & control if the "
                "service is just slow.",
                f"等了 {int(deadline_s)} 秒仍未画好(最后状态「{state or '无'}」),已放弃。"
                "如果只是服务慢,可以在「权限与操控」里把绘画时限调大。"))
        await asyncio.sleep(interval)
        interval = min(POLL_MAX, interval * 1.5)


def save(data: bytes, workspace: Path, prompt: str) -> Path:
    """Write the image into the group's workspace, and nowhere else."""
    try:
        return media.save_bytes(data, workspace, subdir="image", ext=".png",
                                stem_source=prompt, fallback="image",
                                what="image", what_zh="图片")
    except ValueError as e:
        raise ImageError(str(e)) from None


async def probe(provider: dict, model: str, *, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Check what can be checked without spending a generation.

    There is no read-only image endpoint, so this asks `/models` when the service has one:
    reaching it proves the address and the key, and finding the model id in the list proves
    the name. The honest test of the rest is to generate one.

    MetaChat's media API has no list to read at all (`/open/v1/models`: 404), so for that one the
    question becomes the one `media.probe_meta` answers — is it there and does it take this key —
    which is also what the video tool asks it. Asking `/models` here would report a working
    provider as broken.
    """
    base = (provider["base_url"] or "").strip()
    if not base:
        return False, i18n.pick_now("no address is configured for this provider",
                                    "这个服务商没有填地址")
    if provider.get("kind") == "metachat_media":
        own = client is None
        c = client or net.client(base, timeout=15.0)
        try:
            ok, detail = await media.probe_meta(base, provider.get("api_key") or "", client=c)
        finally:
            if own:
                await c.aclose()
        if not ok:
            return False, detail
        # This API publishes no list to read, but the ids it accepts are the ones this build ships
        # (`media.BUILTIN_MEDIA_MODELS`) — that table is what gives every one of them its submit
        # path. So a name outside it is *known* not to work, and saying so here is the difference
        # between a settings page that warns and a round that spends three calls discovering it.
        # `reconcile_model` is the same judgement the tool makes, so the two cannot disagree.
        name = (model or "").strip()
        picked, note = media.reconcile_model(provider, "image", name)
        if note:
            return True, f"{detail} {note}"
        if not name:
            return True, i18n.pick_now(
                f"{detail} No model is chosen, so this build's first shipped one "
                f"(\"{picked}\") will be used. Pick one under Permissions & control → Image generation.",
                f"{detail} 没有选定模型,将使用本版本自带清单里的第一个「{picked}」。"
                "可以在「权限与操控 → 绘画」里选一个。")
        return True, i18n.pick_now(
            f"{detail} \"{name}\" is one of the ids this build ships for that API, so it can be "
            "sent as written.",
            f"{detail}「{name}」是本版本为那个接口自带的 id 之一,可以照原样发出。")
    url = media.api_url(base, "/v1/models")
    own = client is None
    c = client or net.client(url, timeout=15.0)
    try:
        try:
            resp = await c.get(url, headers=media.auth_headers(provider.get("api_key") or ""))
        except Exception as e:  # noqa: BLE001
            return False, i18n.pick_now(f"could not reach {url} ({e})", f"连不上 {url}({e})")
    finally:
        if own:
            await c.aclose()
    if resp.status_code >= 300:
        return False, _why(resp.status_code, resp.text)
    try:
        ids = [str(m.get("id")) for m in (resp.json() or {}).get("data") or [] if isinstance(m, dict)]
    except ValueError:
        ids = []
    want = (model or DEFAULT_MODEL).strip()
    if not ids:
        return True, i18n.pick_now(
            "the address and the key work. This service does not list its models, so the only "
            f"remaining check is to generate one with \"{want}\".",
            f"地址与密钥可用。这个服务方不列出模型清单,所以最后一步只能实际用「{want}」生成一张。")
    if want not in ids:
        sample = ", ".join(ids[:6])
        return True, i18n.pick_now(
            f"the address and the key work, but \"{want}\" is not in the {len(ids)} models this "
            f"service lists ({sample}…)",
            f"地址与密钥可用,但「{want}」不在这个服务方列出的 {len(ids)} 个模型里({sample}…)")
    return True, i18n.pick_now(f"\"{want}\" is available and the key works",
                               f"「{want}」可用,密钥也有效")
