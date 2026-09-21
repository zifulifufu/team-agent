"""The `/hooks/whatsapp` endpoint: messages from WhatsApp drive a round of collaboration.

This module is the app's only externally reachable surface, so the order of checks in
the POST handler is the security design, not an accident:

1. the body is size-capped before anything parses it;
2. the channel must be enabled **and** have an app secret — an unconfigured channel
   refuses rather than accepts, because an endpoint that is "not set up yet" but still
   processes input is exactly how a half-finished integration becomes a hole;
3. the HMAC over the raw body must match (see `whatsapp.verify_signature`);
4. only allowlisted numbers may speak — an empty allowlist means nobody;
5. repeated message ids are dropped and per-sender rate limits apply;
6. only then is a round scheduled, **in the background**, because Meta requires a 200
   within five seconds while a round of collaboration takes far longer.

The reply is whatever the group produced: the last message an agent posted, turned into
WhatsApp's markup and clipped. Nothing is sent back when the round produced no answer,
so a failure shows up as a status entry instead of a stray empty message.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import i18n, whatsapp

WEBHOOK_PATH = "/hooks/whatsapp"
# One ignored-message notice per sender per this many seconds: a stranger poking the
# endpoint must not be able to fill the chat transcript.
NOTICE_EVERY = 600.0
# Per-sender ceiling on rounds started per minute.
PER_MINUTE = 6
MAX_ALLOWED = 50


@dataclass
class Status:
    """What the settings page shows. A webhook is invisible by nature, so without this
    the only observable symptom of a misconfiguration is silence."""
    accepted: int = 0
    rejected: int = 0
    ignored: int = 0
    last_inbound: dict = field(default_factory=dict)
    last_reply: dict = field(default_factory=dict)
    last_error: str = ""

    def snapshot(self) -> dict:
        return {"accepted": self.accepted, "rejected": self.rejected, "ignored": self.ignored,
                "last_inbound": self.last_inbound, "last_reply": self.last_reply,
                "last_error": self.last_error}


def build_whatsapp_router(store: Any, orch: Any, hub: Any) -> APIRouter:
    r = APIRouter()
    status = Status()
    seen = whatsapp.Recent(512)
    limiter = whatsapp.RateLimit(PER_MINUTE, 60.0)
    notice = whatsapp.RateLimit(1, NOTICE_EVERY)
    running: set[asyncio.Task] = set()

    def cfg() -> dict:
        return store.get_settings()

    def public_url(settings: dict) -> str:
        host = str(settings.get("whatsapp_public_host") or "").strip().rstrip("/")
        if not host:
            return ""
        return host if "://" in host else f"https://{host}{WEBHOOK_PATH}"

    async def _system(gid: str, text: str) -> None:
        try:
            msg = store.add_message(gid, "system", None, i18n.pick_now("System", "系统"), text)
            await hub.broadcast(gid, {"type": "message", "message": msg})
        except Exception:  # noqa: BLE001 — a notice that fails must not break the route
            pass

    async def run_round(gid: str, item: whatsapp.Inbound, settings: dict) -> None:
        """Answer one WhatsApp message and send the group's reply back."""
        replies: list[str] = []

        async def emit(ev: dict) -> None:
            await hub.broadcast(gid, ev)
            # A member's finished reply arrives as `message_end`, not `message`: `message` is
            # for messages that are already whole (yours, and the task board), while an agent's
            # reply is streamed as deltas and only complete at the end. Collecting `message`
            # here would leave the reply list permanently empty — the round would run, the
            # desktop would show the answer, and nothing would ever reach WhatsApp.
            if ev.get("type") == "message_end":
                m = ev.get("message") or {}
                if m.get("sender_type") == "agent" and (m.get("content") or "").strip():
                    replies.append(m["content"])

        label = f"WhatsApp · {item.name}" if item.name else f"WhatsApp · {item.wa_id}"
        try:
            # `read_only=True` is the promise made on the settings page: input that
            # arrives over the network never reaches exec/write tools. See
            # `RunState.read_only` — the tool list is filtered, not merely discouraged.
            await orch.handle_user_message(gid, item.text, emit, sender_name=label, read_only=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — surfaced in the status, not swallowed
            status.last_error = repr(e)
            await _system(gid, i18n.pick_now(f"This WhatsApp round failed: {e}",
                                             f"这条来自 WhatsApp 的协作出错了:{e}"))
        finally:
            await hub.broadcast(gid, {"type": "idle"})
            if replies:
                body = str(settings.get("whatsapp_prefix") or "") + whatsapp.to_plain(replies[-1])
                body = whatsapp.clip(body, int(settings.get("whatsapp_max_chars") or 1500))
                ok, detail = await whatsapp.send_text(
                    {"phone_number_id": settings.get("whatsapp_phone_number_id"),
                     "access_token": settings.get("whatsapp_token"),
                     "proxy": settings.get("whatsapp_proxy")},
                    item.wa_id, body,
                )
                status.last_reply = {"at": time.time(), "ok": bool(ok), "detail": detail,
                                     "chars": len(body)}
                if not ok:
                    status.last_error = detail

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        running.add(task)
        task.add_done_callback(running.discard)

    # ============================================================ Meta's handshake
    @r.get(WEBHOOK_PATH)
    async def handshake(request: Request) -> Response:
        """Meta calls this once, when the callback URL is saved. It echoes back
        `hub.challenge` only if the verify token matches."""
        settings = cfg()
        verify_token = str(settings.get("whatsapp_verify_token") or "")
        if not settings.get("whatsapp_enabled") or not verify_token:
            status.rejected += 1
            return JSONResponse({"detail": "the WhatsApp channel is not configured"}, status_code=403)
        q = request.query_params
        got = whatsapp.challenge(q.get("hub.mode"), q.get("hub.verify_token"),
                                 verify_token, q.get("hub.challenge"))
        if got is None:
            status.rejected += 1
            return JSONResponse({"detail": "the verify token does not match"}, status_code=403)
        return PlainTextResponse(got)

    # =================================================================== messages
    @r.post(WEBHOOK_PATH)
    async def inbound(request: Request) -> Response:
        settings = cfg()
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > whatsapp.MAX_BODY:
            status.rejected += 1
            return JSONResponse({"detail": "the body is too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > whatsapp.MAX_BODY:
            status.rejected += 1
            return JSONResponse({"detail": "the body is too large"}, status_code=413)

        app_secret = str(settings.get("whatsapp_app_secret") or "")
        if not settings.get("whatsapp_enabled") or not app_secret:
            # 503 rather than 200 so Meta keeps retrying: the usual reason to be here is
            # that the channel has not been filled in yet.
            status.rejected += 1
            return JSONResponse({"detail": "the WhatsApp channel is not configured"}, status_code=503)
        if not whatsapp.verify_signature(app_secret, raw, request.headers.get(whatsapp.SIGNATURE_HEADER)):
            status.rejected += 1
            return JSONResponse({"detail": "the signature does not match"}, status_code=403)

        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            status.rejected += 1
            return JSONResponse({"detail": "the body is not JSON"}, status_code=400)

        messages, skipped = whatsapp.parse(payload)
        allowed = whatsapp.digits(settings.get("whatsapp_allowed"))[:MAX_ALLOWED]
        gid = str(settings.get("whatsapp_group_id") or "")
        group = store.get_group(gid) if gid else None

        started = 0
        for item in messages:
            status.last_inbound = {"at": time.time(), "wa_id": item.wa_id, "name": item.name,
                                   "text": item.text[:200]}
            if not seen.first_time(item.message_id):
                status.ignored += 1
                continue                                  # Meta redelivered an event we handled
            if item.wa_id not in allowed:
                status.ignored += 1
                status.last_error = i18n.pick_now(
                    f"{item.wa_id} is not on the allowlist, so the message was dropped",
                    f"{item.wa_id} 不在白名单里,这条消息已丢弃")
                if group and notice.allow(item.wa_id):
                    await _system(group["id"], i18n.pick_now(
                        f"A WhatsApp message from {item.wa_id} was dropped: that number is not on the allowlist.",
                        f"来自 {item.wa_id} 的 WhatsApp 消息被丢弃:该号码不在白名单里。"))
                continue
            if group is None:
                status.ignored += 1
                status.last_error = i18n.pick_now(
                    "no group chat is bound to this channel, or the group no longer exists",
                    "这条通道没有绑定群聊,或绑定的群已不存在")
                continue
            if not limiter.allow(item.wa_id):
                status.ignored += 1
                status.last_error = i18n.pick_now(
                    f"{item.wa_id} is sending too fast — try again in a minute",
                    f"{item.wa_id} 发送过于频繁——请等一分钟再试")
                continue
            status.accepted += 1
            started += 1
            spawn(run_round(group["id"], item, settings))

        if skipped:
            status.last_error = i18n.pick_now(
                f"ignored message types this channel cannot carry: {', '.join(skipped)}",
                f"忽略了这条通道承载不了的消息类型:{', '.join(skipped)}")
        # Meta only needs to know the post arrived; the work continues in the background.
        return JSONResponse({"ok": True, "accepted": started})

    # ================================================================== status
    @r.get("/api/whatsapp/status")
    async def whatsapp_status() -> dict:
        settings = cfg()
        return {
            "enabled": bool(settings.get("whatsapp_enabled")),
            "webhook_path": WEBHOOK_PATH,
            "public_url": public_url(settings),
            "group_id": settings.get("whatsapp_group_id") or "",
            "allowed": whatsapp.digits(settings.get("whatsapp_allowed")),
            "counters": status.snapshot(),
        }

    @r.post("/api/whatsapp/probe")
    async def whatsapp_probe() -> dict:
        """Ask Meta about the number's own metadata.

        The first thing that goes wrong on this channel is a token that will not work, and the
        symptom is an empty chat rather than an error — so this needs a button that fails
        loudly, on demand, without sending a message to anybody.
        """
        settings = cfg()
        ok, detail = await whatsapp.probe({
            "phone_number_id": settings.get("whatsapp_phone_number_id"),
            "access_token": settings.get("whatsapp_token"),
            "proxy": settings.get("whatsapp_proxy"),
        })
        if not ok:
            status.last_error = detail
        return {"ok": bool(ok), "detail": detail}

    return r
