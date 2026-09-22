"""Chat channels: one pipeline, several platforms.

`spec` says what can be connected and what each one needs; the adapter modules do the
platform talking; this file is the single surface the API layer sees, so the route that
accepts a message never has to know which platform sent it.

Adding a platform therefore means: a block in `spec.CHANNELS`, a module here if its
protocol is new, and one line in the dispatch tables below. Nothing in `api_channels.py`
changes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from . import base, push, spec, telegram, whatsapp  # noqa: F401  (re-exported for callers)
from .base import Inbound  # noqa: F401

__all__ = [
    "base", "push", "spec", "telegram", "whatsapp",
    "Inbound", "ids", "all_channels", "get", "can_receive", "setting_keys",
    "defaults", "secrets", "ranges", "webhook_path", "public_url", "strip_prefix", "masked",
    "view", "parse", "verify", "handshake", "send", "probe", "format_reply",
    "missing", "allowlist", "poll",
]

_INBOUND = {"whatsapp": whatsapp}
"""Channels that accept a webhook post, mapped to the module that authenticates it."""

_ROBOTS = set(push.ROBOTS)


# ------------------------------------------------------------------ catalogue
def ids() -> tuple[str, ...]:
    return spec.ids()


def all_channels() -> list[dict]:
    return spec.all_channels()


def get(cid: str) -> dict | None:
    return spec.get(cid)


def can_receive(cid: str) -> bool:
    return spec.can_receive(cid)


def setting_keys(cid: str) -> set[str]:
    return spec.setting_keys(cid)


def defaults() -> dict[str, Any]:
    return spec.defaults()


def secrets() -> dict[str, tuple[str, str]]:
    return spec.secrets()


def ranges() -> dict[str, tuple[int, int]]:
    return spec.ranges()


def webhook_path(cid: str) -> str:
    return spec.webhook_path(cid)


def public_url(cid: str, settings: dict) -> str:
    return spec.public_url(cid, settings)


def strip_prefix(cid: str, settings: dict) -> dict:
    return spec.strip_prefix(cid, settings)


def masked(cid: str, settings: dict) -> dict:
    """Configuration with secrets blanked — the only form that may leave over the API."""
    return spec.masked(cid, settings)


def view(cid: str, settings: dict, **kw: Any) -> dict:
    return spec.view(cid, settings, **kw)


# ------------------------------------------------------------------- dispatch
def allows_webhook(cid: str) -> bool:
    """Whether this channel can be reached at `/hooks/{id}` at all."""
    return cid in _INBOUND


def needs_public_url(cid: str) -> bool:
    return bool((spec.get(cid) or {}).get("needs_public_url"))


def public_hosts(settings: dict) -> list[str]:
    """Every public hostname the app has to accept in its Host header.

    Read from the settings rather than hard-coded to WhatsApp: a channel added later
    would otherwise be reachable by the platform and rejected by the app itself, which
    looks exactly like a broken tunnel.
    """
    out: list[str] = []
    for cid in spec.ids():
        url = spec.public_url(cid, settings)
        if not url:
            continue
        host = url.split("://")[-1].split("/")[0].split(":")[0].strip()
        if host and host not in out:
            out.append(host)
    return out


def allowlist(cid: str, raw: Any) -> list[str]:
    if cid == "telegram":
        return telegram.ids(raw)
    return whatsapp.numbers(raw)


def verify(cid: str, cfg: dict, headers: dict, body: bytes) -> str:
    """Authenticate one inbound request.

    Returns an empty string when the caller is who it claims to be, otherwise a sentence
    for the log and the status panel. A channel with no inbound route can never get here,
    and an unconfigured secret always fails: an endpoint that is "not set up yet" but
    still processes input is how a half-finished integration becomes a hole.
    """
    if cid == "whatsapp":
        secret = str(cfg.get("app_secret") or "")
        if not secret:
            return "the app secret is not configured"
        if not whatsapp.verify_signature(secret, body, headers.get(whatsapp.SIGNATURE_HEADER)):
            return "the signature does not match"
        return ""
    return f"{cid} does not accept webhook posts"


def handshake(cid: str, query: dict, cfg: dict) -> str | None:
    """The GET verification some platforms perform when a callback URL is saved."""
    if cid == "whatsapp":
        return whatsapp.challenge(query.get("hub.mode"), query.get("hub.verify_token"),
                                  str(cfg.get("verify_token") or ""), query.get("hub.challenge"))
    return None


def parse(cid: str, payload: Any) -> tuple[list[Inbound], list[str]]:
    if cid == "whatsapp":
        return whatsapp.parse(payload)
    if cid == "telegram":
        item, skipped = telegram.parse_update(payload)
        return ([item] if item else []), ([skipped] if skipped and skipped != "other" else [])
    return [], []


def format_reply(cid: str, cfg: dict, text: str) -> str:
    if cid in _ROBOTS:
        return push.format_message(cfg, text)
    if cid == "telegram":
        return telegram.format_reply(cfg, text)
    return whatsapp.format_reply(cfg, text)


def missing(cid: str, cfg: dict) -> list[str]:
    """What still has to be filled in before this channel works. Empty = ready."""
    if cid in _ROBOTS:
        return push.missing(cid, cfg)
    if cid == "telegram":
        return telegram.missing(cfg)
    if cid == "whatsapp":
        return whatsapp.missing(cfg)
    return []


async def send(cid: str, cfg: dict, to: str, text: str) -> tuple[bool, str]:
    if cid in _ROBOTS:
        return await push.send(cid, cfg, text)
    if cid == "telegram":
        return await telegram.send(cfg, to, text)
    if cid == "whatsapp":
        return await whatsapp.send(cfg, to, text)
    return False, f"unknown channel: {cid}"


async def probe(cid: str, cfg: dict) -> tuple[bool, str]:
    if cid in _ROBOTS:
        return await push.probe(cid, cfg)
    if cid == "telegram":
        return await telegram.probe(cfg)
    if cid == "whatsapp":
        return await whatsapp.probe(cfg)
    return False, f"unknown channel: {cid}"


async def poll(cid: str, cfg: dict, handle: Callable[[Inbound], Awaitable[None]], *,
               stop: asyncio.Event, note: Callable[[str], None] | None = None) -> None:
    """Run a channel that has to fetch its own messages. Only Telegram today."""
    if cid == "telegram":
        await telegram.poll(cfg, handle, stop=stop, note=note)
