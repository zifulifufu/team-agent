"""Telegram: a bot answers, and this app **polls** Telegram instead of being called.

The interesting part is what this channel does *not* need. Telegram can push updates to
a webhook, but a webhook requires a public HTTPS address and a certificate. Polling
(`getUpdates`) reverses the direction: this app opens the connection outward, so a
machine behind NAT with no tunnel and no public name can still receive messages. That is
why this is the one channel that works out of the box on a laptop.

Three platform behaviours the code has to respect:

* **Updates are acknowledged by offset.** Sending `offset=<last id + 1>` marks everything
  below it as handled. Miss that and the same message drives the same round forever.
* **`getUpdates` and a webhook are mutually exclusive** — while one is set, polling
  answers `409 Conflict`. Since the symptom is "the bot is silent", `probe` asks
  `getWebhookInfo` and says so plainly rather than leaving it to be guessed.
* **A backlog appears on first contact.** With no offset, Telegram hands over up to 24
  hours of undelivered updates, so a channel enabled today would replay yesterday's
  messages as fresh rounds. The first call therefore drains and discards.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from .. import i18n
from .base import Inbound, clip, http_client, to_html, why

API = "https://api.telegram.org"
HARD_CHARS = 4096

# Long-poll wait. Telegram holds the request open for up to `timeout` seconds, so the
# HTTP timeout has to be comfortably longer or a quiet channel looks like a failure.
POLL_SECONDS = 25
POLL_HTTP_TIMEOUT = 40.0
# After an error the loop waits this long, doubling up to the ceiling. A wrong token or a
# webhook conflict will not fix itself, so hammering the API is pointless.
BACKOFF_FIRST = 2.0
BACKOFF_MAX = 120.0


def ids(raw: Any) -> list[str]:
    """Normalize the allowlist.

    Telegram identifies a chat by a number, and **group ids are negative** — stripping
    the sign the way a phone-number allowlist does would turn -1001234567890 into
    1001234567890, which is a different chat. Channel handles (@name) are accepted too.
    """
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;，；\n]+", str(raw or ""))
    out: list[str] = []
    for item in items:
        s = str(item).strip()
        got = s if s.startswith("@") else re.sub(r"[^\d-]", "", s)
        if got and got != "-" and got not in out:
            out.append(got)
    return out


def parse_update(update: Any) -> tuple[Inbound | None, str]:
    """One update to one message, or a note about why it cannot be used.

    Only text is carried. A photo is refused **even when it has a caption**: the caption
    would read as a question about a picture nobody in the group can see, and an answer
    invented around a missing image is worse than being told it was skipped. Naming the
    kind is what lets the status panel say so instead of the channel looking dead.
    """
    if not isinstance(update, dict):
        return None, ""
    msg = update.get("message") or update.get("edited_message") or {}
    if not isinstance(msg, dict):
        return None, ""
    chat = msg.get("chat") or {}
    sender = str(chat.get("id") or "")
    if not sender:
        return None, ""
    kind = "text"
    if not msg.get("text"):
        for marker in ("photo", "voice", "audio", "document", "video", "video_note", "sticker",
                       "location", "contact", "poll"):
            if msg.get(marker):
                kind = marker
                break
        else:
            kind = "other"
    body = str(msg.get("text") or "").strip()
    if kind != "text" or not body:
        return None, kind
    # The person, not the room: in a group `chat` is the group (its name is `title`) and the
    # speaker is in `from`, so reading `chat` first would label every message with the group.
    who = msg.get("from") or {}
    name = " ".join(str(x) for x in (who.get("first_name") or "", who.get("last_name") or "") if x)
    name = name or str(chat.get("title") or "") or str(who.get("username") or "")
    mid = f"{sender}:{msg.get('message_id')}"
    return Inbound(sender=sender, name=name, text=body, message_id=mid), ""


def _url(cfg: dict, method: str) -> str:
    return f"{API}/bot{str(cfg.get('bot_token') or '').strip()}/{method}"


def missing(cfg: dict) -> list[str]:
    out: list[str] = []
    if not str(cfg.get("group_id") or ""):
        out.append(i18n.pick_now("no group chat is bound to this channel",
                                 "这条通道没有绑定群聊"))
    if not str(cfg.get("bot_token") or ""):
        out.append(i18n.pick_now("the bot token is missing, so nothing can be received or sent",
                                 "缺少机器人 token,无法接收或发送"))
    if not ids(cfg.get("allowed")):
        out.append(i18n.pick_now("nobody is allowlisted, so every message is dropped",
                                 "白名单为空,所有消息都会被丢弃"))
    return out


def _explain(data: dict, status: int) -> str:
    code = int(data.get("error_code") or status or 0)
    desc = str(data.get("description") or "").strip()
    low = desc.lower()
    if code == 401:
        hint = i18n.pick_now("the bot token was refused — copy it again from @BotFather",
                             "机器人 token 被拒绝——请重新从 @BotFather 复制")
    elif code == 409:
        hint = i18n.pick_now("another process is already polling this bot (or a webhook is set). Only one consumer may call getUpdates at a time",
                             "已有另一个进程在拉取这个机器人(或设置了 webhook)。getUpdates 同时只能有一个消费者")
    elif "chat not found" in low:
        hint = i18n.pick_now("Telegram does not know that chat id — open the bot in Telegram and send it a message first",
                             "Telegram 不认识这个 chat id——请先在 Telegram 里给机器人发一条消息")
    elif "blocked" in low or "can't initiate" in low or "chat not" in low:
        hint = i18n.pick_now("the bot may not start this conversation: that person has to send the bot a message first",
                             "机器人不能主动开启对话:需要对方先给机器人发一条消息")
    elif code == 429:
        hint = i18n.pick_now(f"rate limited by Telegram — retry in {data.get('parameters', {}).get('retry_after', '?')} seconds",
                             f"被 Telegram 限流——请在 {data.get('parameters', {}).get('retry_after', '?')} 秒后重试")
    elif code >= 500:
        hint = i18n.pick_now("Telegram had a server-side problem — this is transient",
                             "Telegram 服务端出错——属于暂时性问题")
    else:
        hint = i18n.pick_now("Telegram refused the request", "Telegram 拒绝了这次请求")
    msg = i18n.pick_now(f"Telegram answered HTTP {status}: {hint}", f"Telegram 返回了 HTTP {status}:{hint}")
    return f"{msg} · {desc}" if desc else msg


async def _call(cfg: dict, method: str, payload: dict | None = None, *,
                timeout: float = 15.0) -> tuple[bool, Any, str]:
    token = str(cfg.get("bot_token") or "").strip()
    if not token:
        return False, None, i18n.pick_now("the bot token is not set", "没有填机器人 token")
    url = _url(cfg, method)
    try:
        async with http_client(str(cfg.get("proxy") or "").strip(), url) as client:
            resp = await client.post(url, json=payload or {}, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — a connection failure is the common case
        return False, None, i18n.pick_now(
            f"could not reach Telegram ({why(e)}). Telegram is blocked in mainland China, so this channel needs a proxy",
            f"连不上 Telegram({why(e)})。Telegram 在中国大陆无法直连,这条通道需要代理")
    try:
        data = resp.json() or {}
    except ValueError:
        data = {}
    if resp.status_code >= 300 or not data.get("ok", False):
        return False, data, _explain(data, resp.status_code)
    return True, data.get("result"), ""


def format_reply(cfg: dict, text: str) -> str:
    return clip(to_html(text), int(cfg.get("max_chars") or 3500))


async def send(cfg: dict, to: str, text: str) -> tuple[bool, str]:
    chat = (ids(to) or [""])[0]
    if not chat or not text:
        return False, i18n.pick_now("nothing to send", "没有可发送的内容")
    ok, result, detail = await _call(cfg, "sendMessage", {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })
    if not ok:
        return False, detail
    mid = str((result or {}).get("message_id") or "")
    return True, mid or i18n.pick_now("sent", "已发送")


async def probe(cfg: dict) -> tuple[bool, str]:
    """Prove the token works, and check for the one setting that silently defeats polling.

    `getMe` alone would say "all good" on a bot whose updates are being delivered to a
    webhook somewhere, and the only symptom of that is a channel that never reacts.
    """
    ok, result, detail = await _call(cfg, "getMe")
    if not ok:
        return False, detail
    who = "@" + str((result or {}).get("username") or "") or "the bot"
    ok, hook, detail = await _call(cfg, "getWebhookInfo")
    if ok and isinstance(hook, dict) and hook.get("url"):
        return False, i18n.pick_now(
            f"{who} is reachable, but a webhook is set to {hook['url']}: while one is set Telegram refuses getUpdates, "
            f"so messages go elsewhere. Delete the webhook (or use it instead of polling)",
            f"{who} 可以访问,但它设置了 webhook({hook['url']}):只要设置了 webhook,Telegram 就会拒绝 getUpdates,"
            f"消息会发到别处。请删除该 webhook(或改用 webhook 方式)")
    return True, i18n.pick_now(f"{who} is reachable and no webhook is in the way",
                               f"{who} 可以访问,且没有 webhook 干扰")


async def poll(cfg: dict, handle: Callable[[Inbound], Awaitable[None]], *,
               stop: asyncio.Event, note: Callable[[str], None] | None = None) -> None:
    """Long-poll `getUpdates` until `stop` is set.

    `handle` is called once per usable message, in order. Nothing here knows about
    allowlists or rounds — that is the caller's business, so the same pipeline serves
    both a polled channel and a posted one.
    """
    def say(text: str) -> None:
        if note:
            note(text)

    offset = 0
    if bool(cfg.get("drop_pending", True)):
        # One non-blocking call to find where the backlog ends, then start after it.
        ok, updates, _ = await _call(cfg, "getUpdates", {"timeout": 0, "limit": 1,
                                                         "allowed_updates": ["message"]}, timeout=15.0)
        if ok and isinstance(updates, list) and updates:
            offset = int(updates[-1].get("update_id") or 0) + 1
            say(i18n.pick_now(f"skipped {len(updates)} message(s) that arrived while this was off",
                              f"已跳过 {len(updates)} 条应用关闭期间到达的消息"))

    backoff = BACKOFF_FIRST
    while not stop.is_set():
        ok, updates, detail = await _call(cfg, "getUpdates", {
            "timeout": POLL_SECONDS, "offset": offset, "allowed_updates": ["message"],
        }, timeout=POLL_HTTP_TIMEOUT)
        if stop.is_set():
            return
        if not ok:
            say(detail)
            # `wait` rather than `sleep` so a stopped channel reacts immediately instead
            # of sitting out a two-minute backoff.
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue
        backoff = BACKOFF_FIRST
        for update in updates or []:
            uid = int((update or {}).get("update_id") or 0)
            if uid:
                offset = max(offset, uid + 1)
            item, skipped = parse_update(update)
            if item is None:
                if skipped and skipped != "other":
                    say(i18n.pick_now(f"a {skipped} message was ignored: this channel carries text only",
                                      f"忽略了一条 {skipped} 消息:这条通道只承载文本"))
                continue
            try:
                await handle(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad message must not stop the loop
                say(i18n.pick_now(f"handling a Telegram message failed: {e}",
                                  f"处理 Telegram 消息时出错:{e}"))
