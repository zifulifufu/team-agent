"""The chat channels beyond WhatsApp: the catalogue itself, Telegram (polled), and the
one-way group robots.

The catalogue tests are not busywork. Every settings key a channel declares has to reach
three places that fail *silently* when they do not: `DEFAULT_SETTINGS` (an unregistered
key is dropped by `update_settings` without a word), `SECRET_SETTINGS` (a missing entry
writes a credential into the database in the clear) and `RANGES` (a missing entry makes
the settings page return 400). A channel added later inherits those tests for free.

The protocol tests run against real sockets, because the parts worth testing are the
bytes that leave the machine: the payload shape, the signature, and which message a
platform's error body maps to.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI, Request

from app import api_channels, channels
from app.channels import base, push, telegram
from app.main import create_app
from app.store import Store
from tests.conftest import FakeLLM
from tests.fakes import FakeServer
from tests.test_collab import setup

BOT_TOKEN = "123456:AAHtesttoken"
CHAT_ID = "-1001234567890"
ALLOWED = "-1001234567890"


# --------------------------------------------------------------------- stubs
class FakeHub:
    """Stands in for the WebSocket hub: what matters here is that events reach the client
    side and that nothing raises when there are no connections."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, gid: str, event: dict) -> None:
        self.events.append((gid, event))


def telegram_like(pending: list[dict] | None = None, live: list[dict] | None = None,
                  webhook_url: str = ""):
    """A stand-in for api.telegram.org.

    `pending` is what a `timeout=0` call returns (the backlog Telegram hands over on first
    contact); `live` is what a long poll returns, one update per call.
    """
    app = FastAPI()
    app.state.calls = []
    app.state.pending = list(pending or [])
    app.state.live = list(live or [])

    @app.post("/bot{token}/{method}")
    async def call(token: str, method: str, request: Request):
        body = json.loads((await request.body()) or b"{}")
        app.state.calls.append({"method": method, "body": body})
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "username": "team_agent_bot"}}
        if method == "getWebhookInfo":
            return {"ok": True, "result": {"url": webhook_url}}
        if method == "getUpdates":
            if int(body.get("timeout") or 0) == 0:
                return {"ok": True, "result": app.state.pending}
            if app.state.live:
                return {"ok": True, "result": [app.state.live.pop(0)]}
            await asyncio.sleep(0.05)          # behave a little like a long poll
            return {"ok": True, "result": []}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 99}}
        return {"ok": False, "error_code": 400, "description": "unknown method"}

    return app


def robot_like(errcode: int = 0, errmsg: str = "ok", status: int = 200):
    """A stand-in for a wecom / Feishu / DingTalk / Slack robot endpoint."""
    app = FastAPI()
    app.state.calls = []

    @app.post("/hook")
    async def hook(request: Request):
        app.state.calls.append({"query": dict(request.query_params),
                                "body": json.loads((await request.body()) or b"{}")})
        if errcode == 0 and status < 300:
            # Slack answers with plain text; the others answer JSON with a code.
            return {"errcode": 0, "errmsg": "ok", "code": 0, "msg": "success"}
        return {"errcode": errcode, "errmsg": errmsg, "code": errcode, "msg": errmsg}

    return app


def update(text: str = "帮我看看这份方案", chat_id: str = CHAT_ID, uid: int = 1) -> dict:
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat_id},
                                          "from": {"id": 7, "first_name": "Zhang", "last_name": "San"},
                                          "date": 1700000000, "text": text}}


def cfg_for(cid: str, **over) -> dict:
    out = {f["key"]: f["default"] for f in channels.get(cid)["fields"]}
    out.update(over)
    return out


# ============================================================== the catalogue
def test_every_declared_setting_reaches_the_store_and_the_validator():
    """The three registries that drop a key silently when it is missing."""
    from app.main import create_app as _  # noqa: F401 — importing proves the module loads
    from app.presets import DEFAULT_SETTINGS

    for ch in channels.all_channels():
        cid = ch["id"]
        assert ch["fields"], f"{cid} declares no fields"
        for f in ch["fields"]:
            key = f"{cid}_{f['key']}"
            assert key in DEFAULT_SETTINGS, f"{key} is not in DEFAULT_SETTINGS"
            assert f["label"] and f["label_zh"], f"{key} has no bilingual label"
            assert f["desc"] and f["desc_zh"], f"{key} has no bilingual description"
            if f["kind"] == "secret":
                assert key in Store.SECRET_SETTINGS, f"{key} is not registered as a secret"
            if f["kind"] == "number":
                assert f["min"] is not None and f["max"] is not None, f"{key} has no bounds"


def test_the_secret_keychain_idents_are_stable():
    """Renaming a field silently loses a credential the user already pasted in, because the
    keychain entry is filed under the account name. These are the names already in use."""
    s = channels.secrets()
    assert s["whatsapp_token"] == ("whatsapp", "access-token")
    assert s["whatsapp_app_secret"] == ("whatsapp", "app-secret")
    assert s["whatsapp_verify_token"] == ("whatsapp", "verify-token")


def test_the_catalogue_says_which_channels_can_take_input():
    both = {c["id"] for c in channels.all_channels() if channels.can_receive(c["id"])}
    assert both == {"whatsapp", "telegram"}, "only these two can be talked to from outside"
    for cid in ("wecom", "feishu", "dingtalk", "slack"):
        assert channels.get(cid)["direction"] == "out"
        assert not channels.needs_public_url(cid)
        assert not channels.allows_webhook(cid)
    assert channels.needs_public_url("whatsapp") and channels.allows_webhook("whatsapp")
    assert not channels.needs_public_url("telegram") and channels.allows_webhook("telegram") is False
    # every pushing channel has a sender implementation, and a declared body cap
    assert set(push.ROBOTS) == {"wecom", "feishu", "dingtalk", "slack"}
    for cid in push.ROBOTS:
        assert channels.get(cid)["direction"] == "out"


def test_every_channel_needing_a_public_address_registers_its_host():
    settings = {"whatsapp_public_host": "https://a.example.com/", "telegram_public_host": ""}
    assert channels.public_hosts(settings) == ["a.example.com"]
    # ...and a channel that needs none contributes nothing, however it is configured.
    assert channels.public_hosts({"whatsapp_public_host": ""}) == []


# ================================================================== telegram
def test_a_group_id_keeps_its_minus_sign():
    """Regression: sharing the phone-number normalizer would strip the sign, turning group
    chat -1001234567890 into a different id that matches nothing."""
    assert telegram.ids(f"{CHAT_ID}, 42") == [CHAT_ID, "42"]
    assert telegram.ids("@some_channel") == ["@some_channel"]
    assert telegram.ids(" -1001 , -1001 ") == ["-1001"]


def test_only_text_updates_are_usable():
    item, skipped = telegram.parse_update(update("你好"))
    assert item is not None and item.text == "你好" and item.sender == CHAT_ID
    assert item.name == "Zhang San" and skipped == ""
    for kind in ("photo", "voice", "document", "sticker"):
        msg = {"update_id": 2, "message": {"message_id": 2, "chat": {"id": CHAT_ID},
                                           "from": {"id": 7, "first_name": "Zhang"}, kind: True}}
        item, skipped = telegram.parse_update(msg)
        assert item is None and skipped == kind, f"a {kind} update must be named, not silently dropped"


def test_a_photo_with_a_caption_is_refused_and_named():
    """The caption is not used on its own: it reads as a question about a picture nobody in
    the group can see, so the honest outcome is "a photo message was ignored" in the status
    rather than an answer invented around a missing image."""
    msg = {"update_id": 3, "message": {"message_id": 3, "chat": {"id": CHAT_ID, "type": "private"},
                                       "photo": [{"file_id": "x"}], "caption": "看看这个"}}
    item, skipped = telegram.parse_update(msg)
    assert item is None and skipped == "photo"


def test_markdown_becomes_the_html_telegram_renders():
    out = base.to_html("## 结论\n\n**重点** 见 `p<0.05` 与 [指南](https://x.org/g)")
    assert "<b>结论</b>" in out and "<b>重点</b>" in out
    assert "<code>p&lt;0.05</code>" in out, "the code span has to be escaped, not parsed as markup"
    assert '<a href="https://x.org/g">指南</a>' in out
    # and anything the model wrote that looks like a tag is inert
    assert "<script>" not in base.to_html("<script>alert(1)</script>")


def test_a_robot_gets_plain_text_and_a_hard_byte_cap():
    """WeCom counts bytes, not characters, so the cap has to be applied in bytes or the
    message is accepted here and refused there."""
    assert base.plain("## 标题\n\n**粗体** 和 `代码`") == "标题\n\n粗体 和 代码"
    clipped = base.clip_bytes("汉" * 2000, 2048)
    assert len(clipped.encode("utf-8")) <= 2048 and clipped.endswith("…")


async def test_a_polled_message_is_delivered_and_the_backlog_is_not(tmp_path, monkeypatch):
    """The drain exists because Telegram hands over up to 24 hours of undelivered updates on
    first contact: without it, switching the channel on would replay yesterday's messages as
    fresh rounds."""
    fake = telegram_like(pending=[update("昨天的消息", uid=1)], live=[update("今天的消息", uid=2)])
    with FakeServer(fake) as server:
        monkeypatch.setattr(telegram, "API", server.url)
        got: list[str] = []

        async def handle(item) -> None:
            got.append(item.text)
            stop.set()

        stop = asyncio.Event()
        cfg = cfg_for("telegram", bot_token=BOT_TOKEN, allowed=[ALLOWED], group_id="g", drop_pending=True)
        task = asyncio.create_task(telegram.poll(cfg, handle, stop=stop, note=lambda s: None))
        await asyncio.wait_for(task, timeout=20)

    assert got == ["今天的消息"], "the backlog must be discarded and the new message delivered"
    live = [c["body"].get("offset") for c in fake.state.calls
            if c["method"] == "getUpdates" and int(c["body"].get("timeout") or 0) > 0]
    assert live and live[0] == 2, "polling has to resume after the drained update, not replay it"


async def test_the_poller_asks_for_the_acknowledged_message_to_be_dropped(tmp_path, monkeypatch):
    fake = telegram_like(live=[update("hi", uid=5)])
    with FakeServer(fake) as server:
        monkeypatch.setattr(telegram, "API", server.url)
        stop = asyncio.Event()

        async def handle(item) -> None:
            stop.set()

        cfg = cfg_for("telegram", bot_token=BOT_TOKEN, allowed=[ALLOWED], group_id="g")
        await asyncio.wait_for(
            asyncio.create_task(telegram.poll(cfg, handle, stop=stop, note=lambda s: None)), timeout=20)

    live_calls = [c for c in fake.state.calls if c["method"] == "getUpdates" and int(c["body"]["timeout"]) > 0]
    assert live_calls and live_calls[0]["body"]["offset"] == 0, "the first drain did not run (nothing pending)"


async def test_a_telegram_webhook_in_the_way_is_reported_not_guessed(monkeypatch):
    """The one misconfiguration whose only symptom is silence: while a webhook is set,
    getUpdates answers 409 and the channel never receives anything."""
    fake = telegram_like(webhook_url="https://someone-else.example.com/hook")
    with FakeServer(fake) as server:
        monkeypatch.setattr(telegram, "API", server.url)
        ok, detail = await telegram.probe(cfg_for("telegram", bot_token=BOT_TOKEN))
    assert not ok and "webhook" in detail.lower()

    fake2 = telegram_like()
    with FakeServer(fake2) as server:
        monkeypatch.setattr(telegram, "API", server.url)
        ok, detail = await telegram.probe(cfg_for("telegram", bot_token=BOT_TOKEN))
    assert ok and "team_agent_bot" in detail


async def test_the_reply_goes_back_to_the_chat_that_asked(tmp_path, monkeypatch, make_router):
    """The whole path for a polled channel: a message arrives while polling, a round runs,
    and the answer is POSTed to sendMessage with the chat id it came from."""
    fake = telegram_like(live=[update("帮我看看用药方案")])
    with FakeServer(fake) as server:
        monkeypatch.setattr(telegram, "API", server.url)
        st = Store(tmp_path / "data")
        gid = st.list_groups()[0]["id"]
        st.update_settings({"telegram_enabled": True, "telegram_group_id": gid,
                            "telegram_allowed": [ALLOWED], "telegram_bot_token": BOT_TOKEN,
                            "telegram_drop_pending": False, "plan_mode": "off"})
        orch, _group = setup(st, make_router, FakeLLM(default="这个问题建议先核对剂量。"))
        chan = api_channels.Channels(st, orch, FakeHub())
        stop = asyncio.Event()

        async def handle(item) -> None:
            await chan._accept("telegram", item, chan.cfg("telegram"))
            stop.set()

        await asyncio.wait_for(
            asyncio.create_task(telegram.poll(chan.cfg("telegram"), handle, stop=stop,
                                              note=lambda s: None)), timeout=20)
        await asyncio.gather(*list(chan.tasks), return_exceptions=True)

    sends = [c for c in fake.state.calls if c["method"] == "sendMessage"]
    assert sends, "nothing was ever sent back to Telegram"
    assert sends[0]["body"]["chat_id"] == CHAT_ID
    assert "核对剂量" in sends[0]["body"]["text"]
    assert sends[0]["body"]["parse_mode"] == "HTML"


# ================================================================== the robots
@pytest.mark.parametrize("cid,shape", [
    ("wecom", lambda b: b["msgtype"] == "text" and "content" in b["text"]),
    ("feishu", lambda b: b["msg_type"] == "text" and "text" in b["content"]),
    ("dingtalk", lambda b: b["msgtype"] == "text" and "content" in b["text"]),
    ("slack", lambda b: isinstance(b.get("text"), str)),
])
async def test_each_robot_gets_the_payload_its_api_expects(cid, shape):
    fake = robot_like()
    with FakeServer(fake) as server:
        cfg = cfg_for(cid, webhook_url=server.url + "/hook", max_chars=200, prefix="[群里] ")
        ok, detail = await channels.send(cid, cfg, "", channels.format_reply(cid, cfg, "**结论**:先核对剂量"))
    assert ok, detail
    body = fake.state.calls[0]["body"]
    assert shape(body), f"{cid} got the wrong shape: {body}"
    assert "[群里] " in json.dumps(body, ensure_ascii=False)
    assert "**" not in json.dumps(body, ensure_ascii=False), "markdown markers must not survive"


async def test_a_wecom_message_is_cut_to_bytes_not_characters():
    fake = robot_like()
    with FakeServer(fake) as server:
        cfg = cfg_for("wecom", webhook_url=server.url + "/hook", max_chars=680)
        await channels.send("wecom", cfg, "", "汉" * 1500)
    content = fake.state.calls[0]["body"]["text"]["content"]
    assert len(content.encode("utf-8")) <= 2048, "wecom would have refused this outright"


async def test_feishu_signs_with_the_secret_as_the_message():
    """Feishu's scheme: base64(HMAC-SHA256(key="timestamp\\nsecret", msg="")). Getting the two
    swapped produces a failure that looks like "wrong secret"."""
    fake = robot_like()
    secret = "feishu-secret"
    with FakeServer(fake) as server:
        cfg = cfg_for("feishu", webhook_url=server.url + "/hook", signing_secret=secret)
        ok, _ = await channels.send("feishu", cfg, "", "hi")
    assert ok
    sent = fake.state.calls[0]["body"]
    want = base64.b64encode(
        hmac.new(f"{sent['timestamp']}\n{secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
    assert sent["sign"] == want and sent["timestamp"].isdigit()


async def test_dingtalk_signs_with_the_secret_as_the_key_and_puts_it_in_the_query():
    """The other way round from Feishu, and in the URL rather than the body."""
    fake = robot_like()
    secret = "ding-secret"
    with FakeServer(fake) as server:
        cfg = cfg_for("dingtalk", webhook_url=server.url + "/hook", signing_secret=secret)
        ok, _ = await channels.send("dingtalk", cfg, "", "hi")
    assert ok
    q = fake.state.calls[0]["query"]
    assert set(q) == {"timestamp", "sign"}
    want = base64.b64encode(
        hmac.new(secret.encode(), f"{q['timestamp']}\n{secret}".encode(), hashlib.sha256).digest()).decode()
    assert q["sign"] == want and len(q["timestamp"]) == 13       # milliseconds
    assert "sign" not in fake.state.calls[0]["body"]


@pytest.mark.parametrize("cid,errcode,errmsg,want", [
    ("wecom", 93000, "invalid webhook url", "key"),
    ("wecom", 45009, "api freq out of limit", "rate limited"),
    ("dingtalk", 310000, "sign not match", "signature"),
    ("dingtalk", 310000, "keywords not in content", "keyword"),
    ("feishu", 19021, "sign match fail", "signature"),
])
async def test_a_robots_refusal_names_the_setting_to_go_and_change(cid, errcode, errmsg, want):
    fake = robot_like(errcode=errcode, errmsg=errmsg)
    with FakeServer(fake) as server:
        cfg = cfg_for(cid, webhook_url=server.url + "/hook")
        ok, detail = await channels.send(cid, cfg, "", "hi")
    assert not ok, "a 200 with an error in the body is still a failure"
    assert want in detail, f"the reason has to name {want!r}, got: {detail}"


async def test_a_non_http_webhook_address_is_refused_before_any_request():
    for url in ("", "file:///etc/passwd", "ftp://host/hook"):
        ok, detail = await channels.send("slack", cfg_for("slack", webhook_url=url), "", "hi")
        assert not ok and detail


# ================================================================== the pipeline
async def test_a_robot_only_receives_answers_from_its_own_group(tmp_path):
    """The guard that keeps a push from leaking one group's work into another's room."""
    fake = robot_like()
    with FakeServer(fake) as server:
        st = Store(tmp_path / "data")
        mine = st.list_groups()[0]["id"]
        other = st.create_group("另一个群")["id"]
        st.update_settings({"wecom_enabled": True, "wecom_group_id": mine, "wecom_on_answer": True,
                            "wecom_webhook_url": server.url + "/hook", "wecom_max_chars": 200})
        chan = api_channels.Channels(st, None, FakeHub())

        await chan.push_answer(other, "别的群的结论")
        assert fake.state.calls == [], "an answer from another group must not be pushed"

        await chan.push_answer(mine, "**结论**:先核对剂量")
        assert len(fake.state.calls) == 1
        assert chan.status["wecom"].last_reply["ok"] is True

        # an answer produced by a channel whose push is switched off
        st.update_settings({"wecom_on_answer": False})
        await chan.push_answer(mine, "不该推的结论")
        assert len(fake.state.calls) == 1


async def test_a_broken_robot_never_turns_a_good_answer_into_a_failure(tmp_path):
    """`push_answer` is a side effect of a round: it records the error and returns."""
    st = Store(tmp_path / "data")
    gid = st.list_groups()[0]["id"]
    st.update_settings({"wecom_enabled": True, "wecom_group_id": gid, "wecom_on_answer": True,
                        "wecom_webhook_url": "http://127.0.0.1:1/hook", "wecom_max_chars": 200})
    chan = api_channels.Channels(st, None, FakeHub())
    await chan.push_answer(gid, "结论")            # must not raise
    assert chan.status["wecom"].last_reply["ok"] is False
    assert chan.status["wecom"].last_error


# ================================================================== the API surface
def _client(tmp_path):
    """Note the base_url: the app trusts loopback hosts only, so the default "testserver"
    would be rejected by the middleware before any route ran."""
    from fastapi.testclient import TestClient

    st = Store(tmp_path / "data")
    gid = st.list_groups()[0]["id"]
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"))
    return TestClient(app, base_url="http://127.0.0.1"), st, gid


def test_the_catalogue_lists_every_channel_with_its_readiness(tmp_path):
    cl, _st, _gid = _client(tmp_path)
    got = cl.get("/api/channels").json()["channels"]
    assert [c["id"] for c in got] == list(channels.ids())
    wecom = next(c for c in got if c["id"] == "wecom")
    assert wecom["ready"] is False and wecom["missing"], "an unconfigured channel is not ready"
    assert wecom["webhook_path"] == "/hooks/wecom"
    # a channel with no public address has no callback URL to show
    assert wecom["public_url"] == ""
    assert cl.get("/api/channels/whatsapp").json()["public_url"] == ""
    assert cl.get("/api/channels/nope").status_code == 404


def test_saving_a_channel_validates_against_its_own_field_table(tmp_path):
    cl, _st, gid = _client(tmp_path)
    ok = cl.put("/api/channels/telegram", json={"enabled": True, "group_id": gid,
                                                "allowed": "123, -100\n456"})
    assert ok.status_code == 200 and ok.json()["ready"] is False      # token still missing
    saved = cl.get("/api/channels/telegram").json()["settings"]
    assert saved["allowed"] == ["123", "-100", "456"], "ids keep their sign and split on comma/newline"
    assert cl.put("/api/channels/telegram", json={"max_chars": 10}).status_code == 400
    assert cl.put("/api/channels/telegram", json={"max_chars": "abc"}).status_code == 400
    assert cl.put("/api/channels/nope", json={"enabled": True}).status_code == 404
    # a secret submitted once is never read back, only reported as saved
    assert cl.put("/api/channels/telegram", json={"bot_token": BOT_TOKEN}).json()["secrets"]["telegram_bot_token"] is True
    body = cl.get("/api/channels/telegram").json()
    assert BOT_TOKEN not in json.dumps(body)
    token_field = next(f for f in body["fields"] if f["key"] == "bot_token")
    assert token_field["value"] == "" and token_field["set"] is True
    # with the token saved, a bound group and an allowlist all in place, it reports ready
    assert cl.put("/api/channels/telegram", json={"group_id": gid, "allowed": ["42"],
                                                  "enabled": True}).json()["ready"] is True


def test_a_channel_that_cannot_receive_has_no_webhook(tmp_path):
    cl, _st, _gid = _client(tmp_path)
    assert cl.post("/hooks/wecom", json={"text": "hi"}).status_code == 404
    assert cl.post("/hooks/nope", json={}).status_code == 404


def test_the_test_button_says_why_it_cannot_send_yet(tmp_path):
    cl, _st, _gid = _client(tmp_path)
    # nothing configured at all: the address is missing
    got = cl.post("/api/channels/slack/test").json()
    assert got["ok"] is False and got["detail"]
    # a channel with no allowlist has nobody to send a test to, and says so
    got = cl.post("/api/channels/telegram/test").json()
    assert got["ok"] is False and got["detail"]


def test_the_index_route_is_still_loopback_only_with_a_public_channel_configured(tmp_path):
    """Registering a tunnel host must not turn into "accept any Host"."""
    st = Store(tmp_path / "data")
    gid = st.list_groups()[0]["id"]
    st.update_settings({"whatsapp_enabled": True, "whatsapp_group_id": gid,
                        "whatsapp_public_host": "hook.example.com"})
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"))
    from fastapi.testclient import TestClient

    assert TestClient(app, base_url="http://hook.example.com").get("/api/health").status_code == 200
    assert TestClient(app, base_url="http://elsewhere.example.com").get("/api/health").status_code == 400
