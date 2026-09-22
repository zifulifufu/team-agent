"""Image generation through an OpenAI-compatible images endpoint.

This is the one media capability that works against a *gateway* rather than a self-hosted
server: `POST {base}/images/generations` with a model name is what OpenAI serves, what
MetaChat serves on its OpenAI-compatible address (GPT-Image), and what most aggregators
implement — so one implementation reaches all of them. A gateway is also the cheapest way in:
a key and a model name, no GPU.

Three platform facts shape it:

* **The response is either base64 or a URL.** OpenAI's newer image models always inline the
  bytes; older and third-party ones return a temporary URL. Both are accepted, and the URL
  case is downloaded immediately — those links expire within minutes.
* **`response_format` is not portable.** Sending it makes some models reject the request
  outright, so it is never sent; whatever comes back decides which branch runs.
* **Failures arrive as HTTP errors with a JSON body**, and the useful ones (bad key, unknown
  model, unsupported size, rate limit) each need a different fix. `explain` names it.

Platforms whose image models are *not* on an OpenAI-compatible path — MetaChat's Seedream /
FLUX / Z-Image / Midjourney endpoints are asynchronous jobs of their own shape — are not
covered here; they would be another provider kind with its own submit/poll dialect.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import httpx

from . import i18n, media, net
from .media import offline_reason, slug  # noqa: F401  (re-exported for the tool layer)

# The provider kinds this module can drive. A subset of `media.MEDIA_KINDS`: the video tool
# must never pick an image provider, and the other way round.
KINDS: tuple[str, ...] = ("openai_image",)

# What OpenAI's image models accept. A gateway may support more, but offering a size the
# model then refuses produces a paid failure, so the list stays conservative.
SIZES = ("1024x1024", "1536x1024", "1024x1536")
DEFAULT_SIZE = "1024x1024"
DEFAULT_MODEL = "gpt-image-1"

SUBMIT_TIMEOUT = 120.0
DOWNLOAD_TIMEOUT = 120.0

# A refusal is a sentence, not a document. The cap exists so an error body cannot be unbounded
# either — the size of a *reply* is a different question from the size of an *image*.
ERROR_BODY_CAP = 64 * 1024


class ImageError(Exception):
    """A failure worth showing the user: the message is already in the request language."""


# ------------------------------------------------------------------ providers
def media_providers(store) -> list[dict]:
    return [p for p in store.list_providers() if p["kind"] in KINDS]


def pick_provider(store, cfg: dict) -> tuple[dict | None, str]:
    """(the provider to generate with, why there is none).

    A configured id wins; otherwise the first usable one. An id that no longer exists is
    reported rather than silently replaced, because quietly generating on somebody else's
    account is exactly the surprise this file should not produce.
    """
    rows = media_providers(store)
    wanted = str(cfg.get("image_provider_id") or "").strip()
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
            "Image generation is on, but no image provider has been added yet. Add \"Image "
            "generation (OpenAI-compatible)\" under model providers — a MetaChat key or any "
            "endpoint serving /images/generations will do.",
            "绘画已开启,但还没有添加图片服务商。请在「模型服务商」里添加"
            "「绘画(OpenAI 兼容)」——MetaChat 的密钥,或任何提供 /images/generations 的地址都可以。",
        )
    return usable[0], ""


def blocked_by_offline(provider: dict, cfg: dict) -> str:
    return offline_reason(provider, cfg, "image", "图片")


# -------------------------------------------------------------------- request
def _api(base: str, path: str) -> str:
    """Join a base URL and an API path, tolerating a base that already ends in /v1."""
    b = (base or "").strip().rstrip("/")
    p = path.lstrip("/")
    if p.startswith("v1/") and b.endswith("/v1"):
        p = p[3:]
    return f"{b}/{p}"


def _headers(key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def build_payload(prompt: str, *, model: str, size: str) -> dict:
    """The request body. `response_format` is deliberately absent — see the module docstring."""
    return {"model": (model or DEFAULT_MODEL).strip(), "prompt": prompt.strip(),
            "n": 1, "size": size}


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
    code = None
    try:
        obj = json.loads(body or "{}")
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                detail = str(err.get("message") or "")
            else:
                detail = str(obj.get("message") or "")
    except ValueError:
        detail = (body or "").strip()
    detail = re.sub(r"\s+", " ", detail)[:200]
    low = detail.lower()

    if status in (401, 403):
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
                   client: httpx.AsyncClient | None = None) -> dict:
    """One image, end to end: `{data, size, seconds, url}`. Raises ImageError with a reason.

    The payload and its length are separate keys on purpose: a single `bytes` key holding the
    image itself reads as a size at every call site, and `size_label(b"...")` fails with a
    comparison error far from its cause.
    """
    url = _api(provider["base_url"], "/v1/images/generations")
    base = (provider.get("base_url") or "").strip()
    cap = max(ERROR_BODY_CAP, max_bytes * 2)      # base64 inflates by a third; the envelope is small
    own = client is None
    c = client or net.client(url, timeout=SUBMIT_TIMEOUT)
    started = time.time()
    try:
        try:
            async with c.stream("POST", url, json=payload,
                                headers=_headers(provider.get("api_key") or ""),
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
    return {"data": blob, "size": len(blob), "seconds": time.time() - started, "url": url}


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
    """
    base = (provider["base_url"] or "").strip()
    if not base:
        return False, i18n.pick_now("no address is configured for this provider",
                                    "这个服务商没有填地址")
    url = _api(base, "/v1/models")
    own = client is None
    c = client or net.client(url, timeout=15.0)
    try:
        try:
            resp = await c.get(url, headers=_headers(provider.get("api_key") or ""))
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
