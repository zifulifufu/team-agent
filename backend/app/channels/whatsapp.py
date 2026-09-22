"""WhatsApp Cloud API: people message a WhatsApp number, the group here answers,
the reply goes back to WhatsApp.

Three facts about the platform shape the code:

* Meta posts the webhook and expects **HTTP 200 within 5 seconds**; the actual round of
  collaboration takes far longer, so the route answers first and does the work in the
  background. Meta retries on non-200 and disables the endpoint entirely after seven
  days of failures.
* The Cloud API can only reply within the **24-hour service window** opened by the
  user's own message. Outside it, sending requires a pre-approved template. A channel
  that answers immediately always sits inside the window, which is why it never
  initiates a conversation.
* `graph.facebook.com` is **not reachable from mainland China**. Unlike the local
  gateways elsewhere in this project (which force `trust_env=False` so a proxy cannot
  hijack a loopback call), this channel usually *needs* a proxy, so it takes an explicit
  one from its settings and otherwise falls back to the system proxy.

Everything in this module is written around one rule: **fail closed**. Nothing is
processed until an HMAC over the raw body checks out against the app secret; if the
secret is not configured, every request is refused rather than waved through.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from .. import i18n
from .base import MAX_BODY, Inbound, clip, http_client, why  # noqa: F401  (MAX_BODY is re-exported for the tests)

GRAPH = "https://graph.facebook.com/v21.0"
SIGNATURE_HEADER = "x-hub-signature-256"


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """True only when `header` is a correct HMAC-SHA256 of the raw body.

    `compare_digest` rather than `==` so the check cannot be timed byte by byte.
    An unset secret returns False: without it there is nothing to verify against, and
    treating that as success would make the endpoint world-writable.
    """
    if not app_secret or not header:
        return False
    prefix = "sha256="
    if not header.startswith(prefix):
        return False
    want = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[len(prefix):].strip())


def challenge(mode: str | None, token: str | None, expected: str, value: str | None) -> str | None:
    """The GET handshake Meta performs when you save the callback URL: it echoes back
    `hub.challenge` only if `hub.verify_token` matches what you configured. Returns the
    value to reply with, or None to refuse.
    """
    if mode != "subscribe" or not expected or not value:
        return None
    if token is None or not hmac.compare_digest(str(token), expected):
        return None
    return value


def parse(payload: Any, expected_number: str = "") -> tuple[list[Inbound], list[str]]:
    """Pull the usable text messages out of a webhook payload.

    Returns `(messages, skipped)`, where `skipped` names the message types that arrived
    but cannot be used. That list exists so the UI can say "2 images were ignored"
    instead of silently appearing to do nothing — this channel only carries text, and
    unexplained silence is the worst possible failure mode.

    `expected_number` is the `phone_number_id` this channel is set up for. One Meta app can
    serve several numbers, and they all post to the same callback, so without the check a
    message sent to another of the user's numbers would be handled as if it had arrived here —
    the signature proves which app sent the event, not which number it was addressed to. An
    event that does not say which number it belongs to is still read: dropping those would lose
    messages from payloads that leave `metadata` out.
    """
    out: list[Inbound] = []
    skipped: list[str] = []
    if not isinstance(payload, dict):
        return out, skipped
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = (change or {}).get("value") or {}
            here = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if expected_number and here and here != expected_number:
                label = i18n.pick_now("another number", "另一个号码")
                if label not in skipped:
                    skipped.append(label)
                continue
            names: dict[str, str] = {}
            for c in value.get("contacts") or []:
                if isinstance(c, dict) and c.get("wa_id"):
                    names[str(c["wa_id"])] = str((c.get("profile") or {}).get("name") or "")
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                kind = str(msg.get("type") or "")
                frm = str(msg.get("from") or "")
                if kind != "text" or not frm:
                    if kind and kind != "text" and kind not in skipped:
                        skipped.append(kind)
                    continue
                body = str((msg.get("text") or {}).get("body") or "").strip()
                if not body:
                    continue
                out.append(Inbound(sender=frm, name=names.get(frm, ""),
                                   text=body, message_id=str(msg.get("id") or "")))
    return out, skipped


def numbers(raw: Any) -> list[str]:
    """Normalize allowlist entries to bare digits, so "+86 138-0000-0000" and
    "8613800000000" are the same number. Meta always reports `from` as digits only.

    A string is split on commas / semicolons / newlines only — **not** on spaces or
    dashes, because those are how people group the digits of one number: splitting on
    whitespace would turn "+86 138-0000-0000" into two bogus entries, "86" and
    "13800000000", and the real number would then never match the allowlist.
    """
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;，；\n]+", str(raw or ""))
    out: list[str] = []
    for item in items:
        d = re.sub(r"\D", "", str(item))
        if d and d not in out:
            out.append(d)
    return out


# Backwards-compatible alias: the allowlist normalizer was named after what it returns
# before other channels needed their own (Telegram ids keep a leading `-`).
digits = numbers


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.M)
_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_FENCE = re.compile(r"```[\w-]*\n(.*?)```", re.S)


def to_plain(text: str) -> str:
    """Turn the app's Markdown into the markup WhatsApp actually renders.

    WhatsApp understands exactly four things: *bold*, _italic_, ~strike~ and
    ```monospace```. Markdown headings and links are not among them, so a reply pasted
    straight through arrives full of stray `##` and bracket noise.
    """
    s = _FENCE.sub(lambda m: "```\n" + m.group(1).strip() + "\n```", text or "")
    s = _CODE.sub(lambda m: "```" + m.group(1) + "```", s)
    s = _HEADING.sub(lambda m: "*" + m.group(1).strip().rstrip("*") + "*", s)
    s = _BOLD_STAR.sub(lambda m: "*" + m.group(1).strip() + "*", s)
    s = _STRIKE.sub(lambda m: "~" + m.group(1).strip() + "~", s)
    s = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _error_bits(body: str) -> tuple[int | None, str]:
    """Meta answers with {"error": {"message": ..., "code": 131047, ...}}."""
    code: int | None = None
    text = ""
    try:
        obj = json.loads(body or "{}")
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            raw = err.get("code")
            code = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
            text = str(err.get("message") or err.get("error_user_msg") or "")
        elif isinstance(obj, dict):
            text = str(obj.get("message") or "")
    except ValueError:      # a non-JSON body is still worth showing
        text = (body or "").strip()
    return code, re.sub(r"\s+", " ", text)[:200]


def explain_http(status: int, body: str) -> str:
    """Translate a Graph API failure into the thing to go and change."""
    code, detail = _error_bits(body)
    if code == 190 or status in (401, 403):
        hint = i18n.pick_now("the access token was refused — issue a new one in the Meta app and save it again",
                             "访问令牌被拒绝——请到 Meta 应用里重新签发一个,再保存一次")
    elif code == 131047:
        hint = i18n.pick_now("more than 24 hours have passed since the user's last message, so a plain reply is no longer allowed; WhatsApp requires an approved template outside that window",
                             "距用户上一条消息已超过 24 小时,不能再直接回复;WhatsApp 要求这种情况必须用预审通过的模板")
    elif code == 131030:
        hint = i18n.pick_now("the recipient is not on the list of numbers this app may message",
                             "收件人不在这个应用允许发送的号码名单里")
    elif status == 404:
        hint = i18n.pick_now("the phone number id or the request path was not found — check the phone number id",
                             "phone number id 或请求路径不存在——请检查 phone number id")
    elif status == 429 or code in (130429, 131048, 131056):
        hint = i18n.pick_now("rate limited by WhatsApp — slow down and try again shortly",
                             "被 WhatsApp 限流——降低频率,稍后再试")
    elif status >= 500:
        hint = i18n.pick_now("WhatsApp had a server-side problem — this is transient, retry later",
                             "WhatsApp 服务端出错——属于暂时性问题,稍后重试")
    else:
        hint = i18n.pick_now("WhatsApp refused the request", "WhatsApp 拒绝了这次请求")
    msg = i18n.pick_now(f"WhatsApp answered HTTP {status}: {hint}",
                        f"WhatsApp 返回了 HTTP {status}:{hint}")
    return f"{msg} · {detail}" if detail else msg


def missing(cfg: dict) -> list[str]:
    """What still has to be filled in, in the order the setup flow asks for it."""
    out: list[str] = []
    if not str(cfg.get("group_id") or ""):
        out.append(i18n.pick_now("no group chat is bound to this channel",
                                 "这条通道没有绑定群聊"))
    if not str(cfg.get("app_secret") or ""):
        out.append(i18n.pick_now("the app secret is missing, so every webhook post is refused",
                                 "缺少 App Secret,所有回调请求都会被拒绝"))
    if not str(cfg.get("verify_token") or ""):
        out.append(i18n.pick_now("the verify token is missing, so Meta cannot verify the callback URL",
                                 "缺少 verify token,Meta 无法校验回调地址"))
    if not str(cfg.get("token") or "") or not str(cfg.get("phone_number_id") or ""):
        out.append(i18n.pick_now("the access token or the phone number id is missing, so replies cannot be sent",
                                 "缺少访问令牌或 phone number id,无法发送回复"))
    if not numbers(cfg.get("allowed")):
        out.append(i18n.pick_now("nobody is allowlisted, so every message is dropped",
                                 "白名单为空,所有消息都会被丢弃"))
    return out


async def send(cfg: dict, to: str, text: str, *, base: str | None = None) -> tuple[bool, str]:
    """Send one text message. Returns `(ok, detail)`, where `detail` is either the
    WhatsApp message id or a sentence explaining what to fix.
    """
    number = (numbers(to) or [""])[0]
    pid = str(cfg.get("phone_number_id") or "").strip()
    token = str(cfg.get("token") or "").strip()
    proxy = str(cfg.get("proxy") or "").strip()
    if not pid:
        return False, i18n.pick_now("the phone number id is not set", "没有填 phone number id")
    if not token:
        return False, i18n.pick_now("the access token is not set", "没有填访问令牌")
    if not number or not text:
        return False, i18n.pick_now("nothing to send", "没有可发送的内容")

    url = f"{(base or GRAPH).rstrip('/')}/{pid}/messages"
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
               "to": number, "type": "text", "text": {"preview_url": False, "body": text}}
    try:
        async with http_client(proxy, url) as client:
            resp = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001 — a connection failure is the common case here
        return False, i18n.pick_now(
            f"could not reach WhatsApp ({why(e)}). From mainland China graph.facebook.com needs a proxy — set one "
            f"for this channel (Clash usually listens on http://127.0.0.1:7890)",
            f"连不上 WhatsApp({why(e)})。在中国大陆访问 graph.facebook.com 需要代理——请给这条通道设置代理"
            f"(Clash 通常监听 http://127.0.0.1:7890)")
    if resp.status_code >= 300:
        return False, explain_http(resp.status_code, resp.text)
    try:
        mid = str(((resp.json() or {}).get("messages") or [{}])[0].get("id") or "")
    except ValueError:
        mid = ""
    return True, mid or i18n.pick_now("sent", "已发送")


def format_reply(cfg: dict, text: str) -> str:
    return clip(to_plain(text), int(cfg.get("max_chars") or 1500))


async def probe(cfg: dict, *, base: str | None = None) -> tuple[bool, str]:
    """Read the number's own metadata.

    The cheapest way to prove that the token, the proxy and the phone number id all work
    together — one GET, and no message is sent to anybody. Worth having separately from
    `send`, because the first thing that goes wrong on this channel is a token that was
    copied with a trailing space.
    """
    pid = str(cfg.get("phone_number_id") or "").strip()
    token = str(cfg.get("token") or "").strip()
    proxy = str(cfg.get("proxy") or "").strip()
    if not pid:
        return False, i18n.pick_now("the phone number id is not set", "没有填 phone number id")
    if not token:
        return False, i18n.pick_now("the access token is not set", "没有填访问令牌")
    url = f"{(base or GRAPH).rstrip('/')}/{pid}?fields=display_phone_number,verified_name"
    try:
        async with http_client(proxy, url) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001
        return False, i18n.pick_now(
            f"could not reach WhatsApp ({why(e)}) — from mainland China this needs a proxy",
            f"连不上 WhatsApp({why(e)})——在中国大陆需要代理")
    if resp.status_code >= 300:
        return False, explain_http(resp.status_code, resp.text)
    try:
        obj = resp.json() or {}
    except ValueError:
        obj = {}
    label = " · ".join(x for x in (str(obj.get("verified_name") or ""),
                                   str(obj.get("display_phone_number") or "")) if x)
    return True, label or i18n.pick_now("the token and the phone number id are both valid",
                                        "令牌与 phone number id 都有效")
