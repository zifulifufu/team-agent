"""The channel pipeline: the one route that accepts messages from outside, plus the
endpoints the settings page uses to configure, probe and test each channel.

This module is the app's externally reachable surface, so the order of checks in the POST
handler is the security design, not an accident:

1. the body is size-capped before anything parses it;
2. the channel must be enabled **and** fully configured — an unconfigured channel refuses
   rather than accepts, because an endpoint that is "not set up yet" but still processes
   input is exactly how a half-finished integration becomes a hole;
3. the request must authenticate (an HMAC over the raw body, a secret header — whatever
   the platform offers; see `channels.verify`);
4. only allowlisted senders may speak — an empty allowlist means nobody;
5. repeated message ids are dropped and per-sender rate limits apply;
6. only then is a round scheduled, **in the background**, because webhook platforms
   require an answer within seconds while a round of collaboration takes far longer.

A round started from outside is always `read_only`: nobody is sitting at this machine to
approve a tool call, so the tool list is filtered rather than merely discouraged.

Two directions live here. Channels that receive answer the sender; channels that only
push (`direction="out"`) are handled by `push_answer`, which the orchestrator calls once
per finished round.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import channels, i18n
from .channels.base import MAX_BODY, RateLimit, Recent

# One ignored-message notice per sender per this many seconds: a stranger poking the
# endpoint must not be able to fill the chat transcript.
NOTICE_EVERY = 600.0
# Per-sender ceiling on rounds started per minute.
PER_MINUTE = 6
MAX_ALLOWED = 50
# How often the channel configuration is re-read to start or stop a polling channel.
SUPERVISE_EVERY = 5.0
# What the test button sends. Kept short and unmistakable.
TEST_TEXT = ("Test message from Team Agent: the {name} channel is connected.",
             "来自 Team Agent 的测试消息:{name} 通道已连通。")


@dataclass
class Status:
    """What the settings page shows for one channel.

    A webhook is invisible by nature, so without this the only observable symptom of a
    misconfiguration is silence — and silence is the one thing nobody can debug.
    """
    accepted: int = 0
    rejected: int = 0
    ignored: int = 0
    last_inbound: dict = field(default_factory=dict)
    last_reply: dict = field(default_factory=dict)
    last_error: str = ""
    poller: str = ""            # "", "running", "stopped" — only for fetched channels

    def snapshot(self) -> dict:
        return {"accepted": self.accepted, "rejected": self.rejected, "ignored": self.ignored,
                "last_inbound": self.last_inbound, "last_reply": self.last_reply,
                "last_error": self.last_error, "poller": self.poller}


class Channels:
    """Holds the per-channel runtime state and builds the router for it."""

    def __init__(self, store: Any, orch: Any, hub: Any) -> None:
        self.store, self.orch, self.hub = store, orch, hub
        # The hooks live on the orchestrator (it owns the pipeline); the channel layer only
        # needs the outgoing gate, and reading it from there keeps one owner instead of two.
        self.hooks = getattr(orch, "hooks", None)
        self.status: dict[str, Status] = {cid: Status() for cid in channels.ids()}
        self.seen = {cid: Recent(512) for cid in channels.ids()}
        self.limiter = RateLimit(PER_MINUTE, 60.0)
        self.notice = RateLimit(1, NOTICE_EVERY)
        self.tasks: set[asyncio.Task] = set()
        self.pollers: dict[str, tuple[asyncio.Task, asyncio.Event, dict]] = {}
        self.supervisor: asyncio.Task | None = None
        # Polling only runs in the real app (see `startup`). Saving a channel from the
        # settings page must not secretly start talking to the platform in a test process
        # or in a dev instance that was built without the background tasks.
        self.polling_running = False
        self.router = APIRouter()
        self._routes()

    # ------------------------------------------------------------- helpers
    def cfg(self, cid: str, settings: dict | None = None) -> dict:
        return channels.strip_prefix(cid, settings if settings is not None else self.store.get_settings())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _system(self, gid: str, text: str) -> None:
        try:
            msg = self.store.add_message(gid, "system", None, i18n.pick_now("System", "系统"), text)
            await self.hub.broadcast(gid, {"type": "message", "message": msg})
        except Exception:  # noqa: BLE001 — a notice that fails must not break the route
            pass

    def _label(self, cid: str, item: channels.Inbound) -> str:
        name = (channels.get(cid) or {}).get("name") or cid
        return f"{name} · {item.name or item.sender}"

    @staticmethod
    def _bound_group(store: Any, cfg: dict) -> dict | None:
        gid = str(cfg.get("group_id") or "")
        return store.get_group(gid) if gid else None

    # --------------------------------------------------------------- rounds
    async def _round(self, cid: str, item: channels.Inbound, cfg: dict) -> None:
        """Answer one inbound message and send the group's reply back."""
        st = self.status[cid]
        group = self._bound_group(self.store, cfg)
        if group is None:
            return
        gid = group["id"]
        replies: list[str] = []

        async def emit(ev: dict) -> None:
            await self.hub.broadcast(gid, ev)
            # A member's finished reply arrives as `message_end`, not `message`: `message` is
            # for messages that are already whole (yours, and the task board), while an agent's
            # reply is streamed as deltas and only complete at the end. Collecting `message`
            # here would leave the reply list permanently empty — the round would run, the
            # desktop would show the answer, and nothing would ever reach the platform.
            if ev.get("type") == "message_end":
                m = ev.get("message") or {}
                if m.get("sender_type") == "agent" and (m.get("content") or "").strip():
                    replies.append(m["content"])

        try:
            # `read_only=True` is the promise made on the settings page: input that arrives
            # over the network never reaches exec/write tools. See `RunState.read_only` —
            # the tool list is filtered, not merely discouraged.
            await self.orch.handle_user_message(gid, item.text, emit,
                                                 sender_name=self._label(cid, item), read_only=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — surfaced in the status, not swallowed
            st.last_error = repr(e)
            await self._system(gid, i18n.pick_now(f"This {cid} round failed: {e}",
                                                  f"这条来自 {cid} 的协作出错了:{e}"))
        finally:
            await self.hub.broadcast(gid, {"type": "idle"})
            if replies:
                allowed, text = await self._before_send(gid, replies[-1])
                if allowed:
                    body = channels.format_reply(cid, cfg, text)
                    ok, detail = await channels.send(cid, cfg, item.sender, body)
                    st.last_reply = {"at": time.time(), "ok": bool(ok), "detail": detail, "chars": len(body)}
                    if not ok:
                        st.last_error = detail
                else:
                    # A hook held it back. Recorded as a reply that did not happen (with the reason
                    # the settings page already shows) rather than counted as inbound noise.
                    st.last_reply = {"at": time.time(), "ok": False, "detail": text, "chars": 0}

    async def _accept(self, cid: str, item: channels.Inbound, cfg: dict) -> bool:
        """Shared gate for a single message, whether it was posted or polled."""
        st = self.status[cid]
        st.last_inbound = {"at": time.time(), "sender": item.sender, "name": item.name,
                           "text": item.text[:200]}
        if not self.seen[cid].first_time(item.message_id):
            st.ignored += 1
            return False                              # the platform redelivered an event we handled
        allowed = channels.allowlist(cid, cfg.get("allowed"))[:MAX_ALLOWED]
        group = self._bound_group(self.store, cfg)
        if item.sender not in allowed:
            st.ignored += 1
            st.last_error = i18n.pick_now(f"{item.sender} is not on the allowlist, so the message was dropped",
                                          f"{item.sender} 不在白名单里,这条消息已丢弃")
            if group and self.notice.allow(cid + item.sender):
                await self._system(group["id"], i18n.pick_now(
                    f"A {cid} message from {item.sender} was dropped: it is not on the allowlist.",
                    f"来自 {item.sender} 的 {cid} 消息被丢弃:该来源不在白名单里。"))
            return False
        if group is None:
            st.ignored += 1
            st.last_error = i18n.pick_now("no group chat is bound to this channel, or the group no longer exists",
                                          "这条通道没有绑定群聊,或绑定的群已不存在")
            return False
        if not self.limiter.allow(cid + item.sender):
            st.ignored += 1
            st.last_error = i18n.pick_now(f"{item.sender} is sending too fast — try again in a minute",
                                          f"{item.sender} 发送过于频繁——请等一分钟再试")
            return False
        st.accepted += 1
        self._spawn(self._round(cid, item, cfg))
        return True

    async def _before_send(self, gid: str, text: str) -> tuple[bool, str]:
        """Ask the `before_send` hooks about text that is about to leave this machine.

        One choke point for both directions (a reply to an inbound message, and the push into a
        group robot), because "the message already went out" is the one thing no hook can undo.
        Returns `(allowed, text_to_send_or_reason)`.
        """
        if self.hooks is None:
            return True, text
        reason, maybe = await self.hooks.gate_outgoing(gid, text)
        if reason:
            return False, reason
        return True, maybe

    async def push_answer(self, gid: str, text: str) -> None:
        """Forward a finished answer into every one-way channel bound to this group.

        Called by the orchestrator once per round. Failures are recorded rather than
        raised: a broken robot must never turn a good answer into a failed round.
        """
        if not (text or "").strip():
            return
        allowed, text = await self._before_send(gid, text)
        if not allowed:
            return
        settings = self.store.get_settings()
        for cid in channels.ids():
            ch = channels.get(cid) or {}
            if ch.get("direction") != "out":
                continue
            cfg = self.cfg(cid, settings)
            if not cfg.get("enabled") or not cfg.get("on_answer"):
                continue
            if str(cfg.get("group_id") or "") != gid:
                continue
            st = self.status[cid]
            body = channels.format_reply(cid, cfg, text)
            ok, detail = await channels.send(cid, cfg, "", body)
            st.last_reply = {"at": time.time(), "ok": bool(ok), "detail": detail, "chars": len(body)}
            if not ok:
                st.last_error = detail

    # -------------------------------------------------------------- pollers
    def _sync_pollers(self) -> None:
        settings = self.store.get_settings()
        if not self.polling_running:
            return
        for cid in channels.ids():
            ch = channels.get(cid) or {}
            if ch.get("transport") != "poll":
                continue
            cfg = self.cfg(cid, settings)
            want = bool(cfg.get("enabled")) and not channels.missing(cid, cfg)
            have = cid in self.pollers
            # A changed token or allowlist has to take effect without a restart, so the loop
            # is restarted when its configuration differs from the one it started with.
            if want and have and self.pollers[cid][2] != cfg:
                self._stop_poller(cid)
                have = False
            if want and not have:
                self._start_poller(cid, cfg)
            elif not want and have:
                self._stop_poller(cid)
                self.status[cid].poller = "stopped"

    def _start_poller(self, cid: str, cfg: dict) -> None:
        stop = asyncio.Event()
        st = self.status[cid]

        async def handle(item: channels.Inbound) -> None:
            await self._accept(cid, item, cfg)

        def note(text: str) -> None:
            if text:
                st.last_error = text

        async def loop() -> None:
            try:
                await channels.poll(cid, cfg, handle, stop=stop, note=note)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the supervisor will restart it
                st.last_error = repr(e)
                st.poller = "stopped"

        task = asyncio.create_task(loop())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.pollers[cid] = (task, stop, cfg)
        st.poller = "running"

    def _stop_poller(self, cid: str) -> None:
        got = self.pollers.pop(cid, None)
        if got:
            task, stop, _ = got
            stop.set()
            task.cancel()

    async def startup(self) -> None:
        self.polling_running = True
        self._sync_pollers()
        self.supervisor = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        while True:
            await asyncio.sleep(SUPERVISE_EVERY)
            try:
                self._sync_pollers()
            except Exception as e:  # noqa: BLE001 — supervision must never die
                print("channel supervisor error:", e)

    async def shutdown(self) -> None:
        self.polling_running = False
        if self.supervisor:
            self.supervisor.cancel()
        for cid in list(self.pollers):
            self._stop_poller(cid)
        for t in list(self.tasks):
            t.cancel()

    # --------------------------------------------------------------- routes
    def _routes(self) -> None:
        r = self.router

        @r.get("/api/channels")
        async def catalogue() -> dict:
            settings = self.store.get_settings()
            out = []
            for cid in channels.ids():
                cfg = channels.strip_prefix(cid, settings)
                miss = channels.missing(cid, cfg)
                out.append({
                    **channels.view(cid, settings),
                    "settings": channels.masked(cid, settings),
                    "ready": not miss,
                    "missing": miss,
                    "webhook_path": channels.webhook_path(cid),
                    "public_url": channels.public_url(cid, settings),
                    "counters": self.status[cid].snapshot(),
                })
            return {"channels": i18n.localize(out)}

        @r.get("/api/channels/{cid}")
        async def one(cid: str) -> dict:
            """One channel's status, for the refresh button and the counters."""
            if not channels.get(cid):
                return JSONResponse({"detail": f"unknown channel: {cid}"}, status_code=404)
            settings = self.store.get_settings()
            cfg = channels.strip_prefix(cid, settings)
            miss = channels.missing(cid, cfg)
            return i18n.localize({
                **channels.view(cid, settings),
                "settings": channels.masked(cid, settings),
                "ready": not miss,
                "missing": miss,
                "webhook_path": channels.webhook_path(cid),
                "public_url": channels.public_url(cid, settings),
                "counters": self.status[cid].snapshot(),
            })

        @r.put("/api/channels/{cid}")
        async def save(cid: str, body: dict) -> dict:
            ch = channels.get(cid)
            if not ch:
                return JSONResponse({"detail": f"unknown channel: {cid}"}, status_code=404)
            patch: dict[str, Any] = {}
            for f in ch["fields"]:
                key = f["key"]
                if key not in body and f"{cid}_{key}" not in body:
                    continue
                raw = body.get(key, body.get(f"{cid}_{key}"))
                name = f"{cid}_{key}"
                kind = f["kind"]
                if kind == "switch":
                    patch[name] = bool(raw)
                elif kind == "number":
                    try:
                        n = int(raw)
                    except (TypeError, ValueError):
                        return JSONResponse({"detail": f"{key} must be a number"}, status_code=400)
                    if f["min"] is not None and not (f["min"] <= n <= (f["max"] or n)):
                        return JSONResponse({"detail": f"{key} must be between {f['min']} and {f['max']}"},
                                            status_code=400)
                    patch[name] = n
                elif kind in ("ids", "numbers"):
                    patch[name] = channels.allowlist(cid, raw)
                else:
                    patch[name] = str(raw or "").strip()
            before = self.store.get_settings()
            if patch:
                self.store.update_settings(patch)
                if any(before.get(k) != v for k, v in patch.items()):
                    # Message ids belong to the account that answered, and a save can switch
                    # which bot or number that is: Telegram ids are per chat, so the new bot's
                    # "chat:7" is a different message from the old bot's. Keeping the set across
                    # that switch drops the new account's messages as duplicates; clearing it can
                    # at most re-handle a redelivery, which the platforms send again anyway.
                    self.seen[cid] = Recent(512)
            # Secrets are never read back, so readiness is judged from the store rather than
            # from what was just submitted — which also means an empty secret field (the way
            # the UI says "keep the saved one") still counts as configured.
            settings = self.store.get_settings()
            saved_secrets = {f"{cid}_{f['key']}": bool(settings.get(f"{cid}_{f['key']}"))
                             for f in ch["fields"] if f["kind"] == "secret"}
            cfg = channels.strip_prefix(cid, settings)
            miss = channels.missing(cid, cfg)
            self._sync_pollers()
            return {"ok": True, "ready": not miss, "missing": i18n.localize(miss),
                    "secrets": saved_secrets}

        @r.post("/api/channels/{cid}/probe")
        async def probe(cid: str) -> dict:
            if not channels.get(cid):
                return JSONResponse({"detail": f"unknown channel: {cid}"}, status_code=404)
            ok, detail = await channels.probe(cid, self.cfg(cid))
            if not ok:
                self.status[cid].last_error = detail
            return {"ok": bool(ok), "detail": detail}

        @r.post("/api/channels/{cid}/test")
        async def test(cid: str) -> dict:
            """Send one real message, to prove the whole path.

            Nothing here is a read-only check: on a robot the only honest test is a message
            in the room, and on WhatsApp or Telegram a message to the first allowlisted
            sender. It is a separate button from `probe` for exactly that reason.
            """
            ch = channels.get(cid)
            if not ch:
                return JSONResponse({"detail": f"unknown channel: {cid}"}, status_code=404)
            cfg = self.cfg(cid)
            body = i18n.pick_now(*TEST_TEXT).format(name=ch["name"])
            body = str(cfg.get("prefix") or "") + body
            target = ""
            if ch["direction"] == "both":
                allowed = channels.allowlist(cid, cfg.get("allowed"))
                if not allowed:
                    return {"ok": False, "detail": i18n.pick_now(
                        "nobody is allowlisted yet, so there is no one to send a test to",
                        "白名单还是空的,不知道该给谁发测试消息")}
                target = allowed[0]
            ok, detail = await channels.send(cid, cfg, target, body)
            st = self.status[cid]
            st.last_reply = {"at": time.time(), "ok": bool(ok), "detail": detail, "chars": len(body)}
            if not ok:
                st.last_error = detail
            return {"ok": bool(ok), "detail": detail}

        @r.post("/api/channels/{cid}/reconnect")
        async def reconnect(cid: str) -> dict:
            """Restart a polling channel on demand — the supervisor would do it within
            seconds anyway, but "check now" should not make the user wait for a timer."""
            if not channels.get(cid):
                return JSONResponse({"detail": f"unknown channel: {cid}"}, status_code=404)
            self._stop_poller(cid)
            self._sync_pollers()
            return {"ok": True, "poller": self.status[cid].poller}

        # ======================================================= inbound webhooks
        @r.get("/hooks/{cid}")
        async def handshake(cid: str, request: Request) -> Response:
            cfg = self.cfg(cid)
            st = self.status.get(cid)
            if st is None or not channels.allows_webhook(cid) or not cfg.get("enabled"):
                return JSONResponse({"detail": "this channel does not accept webhooks"},
                                    status_code=404)
            got = channels.handshake(cid, dict(request.query_params), cfg)
            if got is None:
                st.rejected += 1
                return JSONResponse({"detail": "the verification does not match"}, status_code=403)
            return PlainTextResponse(got)

        @r.post("/hooks/{cid}")
        async def inbound(cid: str, request: Request) -> Response:
            st = self.status.get(cid)
            if st is None or not channels.allows_webhook(cid):
                return JSONResponse({"detail": "this channel does not accept webhooks"}, status_code=404)
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY:
                st.rejected += 1
                return JSONResponse({"detail": "the body is too large"}, status_code=413)
            # Read it in pieces and stop at the cap. `await request.body()` would buffer
            # whatever arrives and only then allow the check, so a caller sending a chunked
            # body without a Content-Length could make this process allocate without limit —
            # on the one route the internet is meant to reach.
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    st.rejected += 1
                    return JSONResponse({"detail": "the body is too large"}, status_code=413)
            raw = bytes(raw)

            cfg = self.cfg(cid)
            if not cfg.get("enabled") or channels.missing(cid, cfg):
                # 503 rather than 200 so the platform keeps retrying: the usual reason to be
                # here is that the channel has not been filled in yet.
                st.rejected += 1
                return JSONResponse({"detail": f"the {cid} channel is not configured"}, status_code=503)
            reason = channels.verify(cid, cfg, dict(request.headers), raw)
            if reason:
                st.rejected += 1
                st.last_error = reason
                return JSONResponse({"detail": reason}, status_code=403)

            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                st.rejected += 1
                return JSONResponse({"detail": "the body is not JSON"}, status_code=400)

            messages, skipped = channels.parse(cid, payload, cfg)
            started = 0
            for item in messages:
                if await self._accept(cid, item, cfg):
                    started += 1
            if skipped:
                st.last_error = i18n.pick_now(
                    f"ignored message types this channel cannot carry: {', '.join(skipped)}",
                    f"忽略了这条通道承载不了的消息类型:{', '.join(skipped)}")
            # The platform only needs to know the post arrived; the work continues in the
            # background, because a round takes far longer than the timeout it allows.
            return JSONResponse({"ok": True, "accepted": started})


def build_channels(store: Any, orch: Any, hub: Any) -> Channels:
    return Channels(store, orch, hub)
