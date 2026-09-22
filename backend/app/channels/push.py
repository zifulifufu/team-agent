"""The one-way channels: group robots.

A group robot (WeCom / Feishu / DingTalk) is an outgoing webhook into somebody's group
chat. None of them can receive messages — the robot posts, people read. So these
channels do exactly one thing: forward what the group already produced into that room.
A wecom robot that could talk back would mean an app registration and a public
callback, which is a different (and much heavier) integration.

They are grouped in one module because the differences are only a payload shape and a
signature scheme, and keeping the four side by side is what stops the fifth one from
being written with its own idea of what a truncated message looks like.

Two details that are easy to get wrong and are handled here anyway:

* **WeCom counts bytes.** Its 2048 limit on a text message is bytes of UTF-8, so Chinese
  text has a ceiling of roughly 680 characters while English has 2048. Checking
  characters would let a message through that the robot then refuses.
* **Signing is not one algorithm.** Feishu signs with the secret as the message and the
  timestamp interleaved, DingTalk signs the other way round and puts the result in the
  query string. Both are implemented, and a mismatch is reported as such rather than as
  a generic failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

from .. import i18n
from .base import clip, clip_bytes, http_client, plain, why

# name, expected host (a mismatch is a warning, not an error), signing scheme, body cap
ROBOTS: dict[str, dict] = {
    "wecom": {"host": "qyapi.weixin.qq.com", "sign": "", "bytes": 2048, "chars": 680},
    "feishu": {"host": "open.feishu.cn", "sign": "feishu", "bytes": 0, "chars": 4096},
    "dingtalk": {"host": "oapi.dingtalk.com", "sign": "dingtalk", "bytes": 0, "chars": 4096},
    "slack": {"host": "hooks.slack.com", "sign": "", "bytes": 0, "chars": 4000},
}


def known(cid: str) -> bool:
    return cid in ROBOTS


def _check_url(cid: str, url: str) -> str:
    """Only http(s) may be sent to.

    Everywhere else in this app a non-http scheme is treated as "a path we will read",
    and a webhook URL that somehow became `file:///…` would have the HTTP client follow
    it. The `http(s)` test is the whole guard, so it lives in one place.
    """
    u = (url or "").strip()
    if not u:
        return i18n.pick_now("the robot webhook address is not set", "没有填机器人 Webhook 地址")
    scheme = urlparse(u).scheme.lower()
    if scheme not in ("http", "https"):
        return i18n.pick_now(f"the webhook address has to start with http:// or https:// (this one starts with {scheme or 'nothing'})",
                             f"Webhook 地址必须以 http:// 或 https:// 开头(当前是 {scheme or '空'})")
    return ""


def missing(cid: str, cfg: dict) -> list[str]:
    out: list[str] = []
    if not str(cfg.get("group_id") or ""):
        out.append(i18n.pick_now("no group chat is bound to this channel",
                                 "这条通道没有绑定群聊"))
    bad = _check_url(cid, str(cfg.get("webhook_url") or ""))
    if bad:
        out.append(bad)
    return out


def format_message(cfg: dict, text: str) -> str:
    body = str(cfg.get("prefix") or "") + plain(text)
    body = clip(body, int(cfg.get("max_chars") or 1500))
    return body


def _signed(cid: str, url: str, secret: str, body: dict) -> tuple[str, dict]:
    """Attach whatever the platform wants for signature verification.

    Returns `(url, body)` — DingTalk carries the signature in the query string, Feishu in
    the body, which is exactly the sort of difference that gets silently fumbled when
    each platform is implemented in isolation.
    """
    scheme = ROBOTS[cid]["sign"]
    if not secret or not scheme:
        return url, body
    if scheme == "feishu":
        ts = str(int(time.time()))
        s = f"{ts}\n{secret}"                        # timestamp, newline, secret — concatenated
        sign = base64.b64encode(hmac.new(s.encode("utf-8"), digestmod=hashlib.sha256).digest()).decode()
        return url, {**body, "timestamp": ts, "sign": sign}
    ts = str(int(time.time() * 1000))                # DingTalk wants milliseconds
    s = f"{ts}\n{secret}"
    sign = base64.b64encode(hmac.new(secret.encode("utf-8"), s.encode("utf-8"),
                                     hashlib.sha256).digest()).decode()
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={ts}&sign={quote(sign)}", body


def _payload(cid: str, text: str) -> dict:
    if cid == "slack":
        return {"text": text}
    if cid == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"msgtype": "text", "text": {"content": text}}


def _explain(cid: str, status: int, body: str) -> str:
    """Turn a robot's refusal into the setting that caused it."""
    code: Any = None
    detail = (body or "").strip()[:200]
    try:
        obj = json.loads(body or "{}")
        if isinstance(obj, dict):
            code = obj.get("errcode", obj.get("code"))
            detail = str(obj.get("errmsg") or obj.get("msg") or detail)[:200]
    except ValueError:
        pass

    if cid == "wecom":
        if code == 93000:
            hint = i18n.pick_now("the key in the webhook address is not valid — copy the address again from the group robot's settings",
                                 "Webhook 地址里的 key 无效——请到群机器人设置里重新复制地址")
        elif code == 45009:
            hint = i18n.pick_now("the robot is rate limited (20 messages a minute) — try again shortly",
                                 "机器人被限流(每分钟 20 条)——请稍后再试")
        else:
            hint = i18n.pick_now("the robot refused the message", "机器人拒绝了这条消息")
    elif cid == "dingtalk":
        if code == 310000 and "sign" in str(detail).lower():
            hint = i18n.pick_now("the signature was refused — the signing secret in the robot's security settings and the one here have to match",
                                 "签名被拒绝——机器人安全设置里的加签密钥要与这里填的一致")
        elif code == 310000:
            hint = i18n.pick_now("the robot refused it: if its security setting is a keyword, every message must contain that word; if it is an IP list, this machine has to be on it",
                                 "机器人拒绝了:如果安全设置选的是自定义关键词,每条消息都必须包含该词;如果是 IP 白名单,本机必须在名单里")
        else:
            hint = i18n.pick_now("the robot refused the message", "机器人拒绝了这条消息")
    elif cid == "feishu":
        if code == 19021:
            hint = i18n.pick_now("the signature was refused — the secret here has to be the one from the bot's signature settings",
                                 "签名被拒绝——这里填的密钥必须是机器人签名设置里的那个")
        elif code in (9499, 19024):
            hint = i18n.pick_now("the bot is rate limited — try again shortly", "机器人被限流——请稍后再试")
        else:
            hint = i18n.pick_now("the bot refused the message (check its keyword or IP security setting)",
                                 "机器人拒绝了这条消息(请检查关键词或 IP 安全设置)")
    else:
        if status == 404 or "no_service" in (body or ""):
            hint = i18n.pick_now("that webhook no longer exists — it was revoked or the app was removed from the channel",
                                 "该 webhook 已不存在——它被撤销了,或应用已被移出该频道")
        else:
            hint = i18n.pick_now("Slack refused the message", "Slack 拒绝了这条消息")
    msg = i18n.pick_now(f"The robot answered HTTP {status}: {hint}", f"机器人返回了 HTTP {status}:{hint}")
    return f"{msg} · {detail}" if detail else msg


async def send(cid: str, cfg: dict, text: str, *, url_override: str = "") -> tuple[bool, str]:
    """Post one message into the room. `(ok, detail)`; `detail` explains failures."""
    url = url_override or str(cfg.get("webhook_url") or "").strip()
    bad = _check_url(cid, url)
    if bad:
        return False, bad
    # The caps are applied here as well as in `format_message`, because this is the last
    # point before the bytes leave: a caller that built its own body (the test button does)
    # would otherwise be able to post something the robot refuses on size alone.
    text = clip(text, int(cfg.get("max_chars") or 1500))
    hard_bytes = ROBOTS[cid]["bytes"]
    if hard_bytes:
        text = clip_bytes(text, hard_bytes)
    if not (text or "").strip():
        return False, i18n.pick_now("nothing to send", "没有可发送的内容")

    body = _payload(cid, text)
    url, body = _signed(cid, url, str(cfg.get("signing_secret") or "").strip(), body)
    try:
        async with http_client("", url) as client:
            resp = await client.post(url, json=body)
    except Exception as e:  # noqa: BLE001
        return False, i18n.pick_now(f"could not reach the robot ({why(e)})", f"连不上机器人({why(e)})")
    if resp.status_code >= 300:
        return False, _explain(cid, resp.status_code, resp.text)
    # These APIs answer 200 with an error in the body, so the status code alone is not the
    # verdict — the body has to be read, and a body that says nothing recognisable is not a
    # confirmation either. Reading it as "fine" is how a message that never arrived gets
    # reported as sent: an intermediary answering 200 with an HTML page used to pass.
    if cid == "slack":
        # Slack's incoming webhook confirms with the literal text `ok`, while a Slack-compatible
        # relay tends to answer Slack's JSON shape (`{"ok": true}`). Either means delivered;
        # anything else does not.
        if (resp.text or "").strip().lower() == "ok":
            return True, i18n.pick_now("sent", "已发送")
        try:
            relay = resp.json()
        except ValueError:
            relay = None
        if not (isinstance(relay, dict) and relay.get("ok") is True):
            return False, _unconfirmed(resp)
        return True, i18n.pick_now("sent", "已发送")
    try:
        obj = resp.json()
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        return False, _unconfirmed(resp)
    if obj.get("errcode", obj.get("code", 0)) not in (0, None):
        return False, _explain(cid, resp.status_code, resp.text)
    return True, i18n.pick_now("sent", "已发送")


def _unconfirmed(resp: Any) -> str:
    """A 2xx whose body carries no acknowledgement this app recognises."""
    body = re.sub(r"\s+", " ", (resp.text or "").strip())[:120]
    return i18n.pick_now(
        f"the robot answered HTTP {resp.status_code}, but with nothing that confirms delivery "
        f"({body or 'an empty body'}), so this is not counted as sent",
        f"机器人返回了 HTTP {resp.status_code},但内容里没有任何表示「已送达」的确认"
        f"({body or '空内容'}),所以不算发送成功")


async def probe(cid: str, cfg: dict) -> tuple[bool, str]:
    """Check what can be checked without posting into somebody's group.

    A robot has no read-only endpoint: any request *is* a message in the room. So this
    validates the address and warns when the host is not the platform's, and the honest
    test is the send button next to it.
    """
    url = str(cfg.get("webhook_url") or "").strip()
    bad = _check_url(cid, url)
    if bad:
        return False, bad
    host = (urlparse(url).hostname or "").lower()
    want = ROBOTS[cid]["host"]
    scheme = ROBOTS[cid]["sign"]
    if scheme and not str(cfg.get("signing_secret") or "").strip():
        return True, i18n.pick_now(
            "the address looks usable. No signing secret is set — correct if the robot's security setting is a keyword or an IP list, wrong if it is 加签/signature",
            "地址看起来可用。没有填签名密钥——如果机器人的安全设置是关键词或 IP 白名单就正确,若是「加签/签名校验」则不对")
    if host and not (host == want or host.endswith("." + want)):
        return True, i18n.pick_now(
            f"the address looks usable, though it does not point at {want} (it is {host}). That is fine behind your own proxy, but a hand-typed address is worth re-checking",
            f"地址看起来可用,但它并不指向 {want}(当前是 {host})。走自己的代理时正常,但手输的地址建议再核对一次")
    return True, i18n.pick_now("the address looks usable — the only real test is to send a message",
                               "地址看起来可用——真正的验证只能靠实际发一条消息")
