"""Pieces every chat channel shares.

A channel is a way for a conversation to happen away from this app: messages arrive
from somewhere else, a group chat answers, and the answer goes back out. The shapes
differ (WhatsApp posts a webhook, Telegram is polled, a wecom robot only accepts
pushes) but the parts that must behave identically do not: how an inbound message is
represented, how repeats and floods are absorbed, how an answer is trimmed, and how
the HTTP client is built.

Keeping those here rather than in each adapter is what makes "add another platform" a
data change rather than a rewrite — and, more importantly, what keeps a new adapter
from quietly re-inventing the dedupe or the proxy decision.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx

from .. import net

TIMEOUT = 30.0
# An inbound webhook body is a couple of kilobytes. This only guards against someone
# posting garbage at the endpoint; it is not a real capacity limit.
MAX_BODY = 128 * 1024


@dataclass
class Inbound:
    """One usable incoming text message, in platform-neutral terms.

    `sender` is whatever the platform uses to identify who spoke — a phone number for
    WhatsApp, a chat id for Telegram. It is the allowlist key and the reply address.
    """
    sender: str
    name: str
    text: str
    message_id: str


def clip(text: str, limit: int) -> str:
    """Trim to `limit` characters, marking the cut so nobody thinks the answer ended."""
    body = (text or "").strip()
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:max(0, limit - 1)].rstrip() + "…"


def why(e: Exception) -> str:
    """A connection failure's readable name.

    A timeout stringifies to an empty string, so `f"could not reach X ({e})"` comes out as
    "could not reach X ()" — the one case where the reason matters most is the one that
    would show nothing at all.
    """
    return str(e).strip() or type(e).__name__


def clip_bytes(text: str, limit: int) -> str:
    """Trim to `limit` **bytes** of UTF-8.

    Some platforms count bytes rather than characters (a wecom robot allows 2048), and
    with Chinese text the two differ by a factor of three — checking characters there
    would let a message through that the platform then refuses outright.
    """
    body = (text or "").strip()
    if limit <= 0 or len(body.encode("utf-8")) <= limit:
        return body
    cut = body.encode("utf-8")[:max(0, limit - 3)].decode("utf-8", "ignore").rstrip()
    return cut + "…"


class Recent:
    """Bounded set of recently seen message ids.

    Every platform here retries: Meta redelivers an event when a post is slow, Telegram
    repeats an update until it is acknowledged, and a robot's webhook fire can be
    retried by whatever is in front of it. Insertion order is age order, so the oldest
    entry is evicted first.
    """

    def __init__(self, size: int = 512) -> None:
        self.size = max(1, size)
        self._seen: dict[str, float] = {}

    def first_time(self, key: str) -> bool:
        """True the first time `key` is seen; False for a repeat."""
        if not key:
            return True
        if key in self._seen:
            return False
        self._seen[key] = time.time()
        while len(self._seen) > self.size:
            self._seen.pop(next(iter(self._seen)))
        return True


class RateLimit:
    """Sliding-window limiter, so one noisy sender cannot keep a group busy.

    A round of collaboration can take minutes; without this, a burst of messages from
    one phone number queues up rounds faster than they can finish.
    """

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit, self.window = max(1, limit), window
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def forget(self, key: str) -> None:
        self._hits.pop(key, None)


def http_client(proxy: str, url: str = "") -> httpx.AsyncClient:
    """Build the client used to talk to a platform.

    With an explicit proxy configured, `trust_env=False` keeps the environment's
    HTTP_PROXY from layering a second proxy on top of it. Without one, `net.client`
    decides: loopback/LAN goes direct, external hosts honour the system proxy.
    """
    if proxy:
        return httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=TIMEOUT)
    return net.client(url, timeout=TIMEOUT)


# --------------------------------------------------------------------- markup
_FENCE = re.compile(r"```[\w-]*\n(.*?)```", re.S)
_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.M)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE_SPAN = re.compile(r"```[\w-]*\n(.*?)```|`([^`\n]+)`", re.S)


def plain(text: str) -> str:
    """Markdown reduced to plain text: what the platform will show, minus the syntax.

    Robots that only accept plain text (wecom, Slack, …) otherwise render `##` and
    `**` literally, which looks like a bug rather than an answer.
    """
    s = _FENCE.sub(lambda m: m.group(1).strip(), text or "")
    s = _CODE.sub(lambda m: m.group(1), s)
    s = _HEADING.sub(lambda m: m.group(1).strip(), s)
    s = _BOLD.sub(lambda m: m.group(1).strip(), s)
    s = _STRIKE.sub(lambda m: m.group(1).strip(), s)
    s = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def esc(s: str) -> str:
    """The three characters Telegram's HTML mode requires escaped. Public because the
    Telegram formatter needs it to build a fallback that cannot contain markup."""

    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_html(s: str) -> str:
    """Escape first, then add the few tags, so anything the model wrote is inert."""
    s = esc(s)
    s = _HEADING.sub(lambda m: "<b>" + m.group(1).strip().rstrip("*") + "</b>", s)
    s = _BOLD.sub(lambda m: "<b>" + m.group(1).strip() + "</b>", s)
    s = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    return s


def to_html(text: str) -> str:
    """Markdown to the small HTML subset Telegram renders.

    Code spans are handled first and passed through untouched: escaping them and then
    running the inline rules over their contents would turn a code sample into markup.
    """
    out: list[str] = []
    pos = 0
    for m in _CODE_SPAN.finditer(text or ""):
        out.append(_inline_html((text or "")[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<pre>{esc(m.group(1).strip())}</pre>")
        else:
            out.append(f"<code>{esc(m.group(2))}</code>")
        pos = m.end()
    out.append(_inline_html((text or "")[pos:]))
    return "".join(out).strip()
