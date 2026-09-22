"""The inbound WhatsApp channel: a signature-gated webhook, an allowlist of senders, and a
round that can only reach read-only tools.

Nothing here talks to Meta. `graph_like()` serves the Graph API over a real socket in a
thread, so the whole path is exercised end to end: a signed POST arrives, a round of
collaboration runs, and the reply is POSTed back out — bearer header and body included.

The tests are grouped the way the route checks things, because that order *is* the security
design: size, then configuration, then signature, then allowlist, then dedupe and rate limit.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import api_channels, toolhub
from app.channels import base, whatsapp
from app.main import create_app
from app.store import Store
from tests.conftest import FakeLLM
from tests.fakes import FakeServer
from tests.test_collab import setup

APP_SECRET = "app-secret-xyz"
VERIFY_TOKEN = "verify-me"
TOKEN = "EAAG-test-token"
PID = "1234567890"
NUMBER = "8613800000000"
OTHER = "8613900000000"
HOOK_HOST = "hook.example.com"


# --------------------------------------------------------------------------- stubs
def graph_like(status: int = 200, error: dict | None = None) -> FastAPI:
    """A stand-in for graph.facebook.com. Records every POST so a test can assert on what
    Meta would actually have received."""
    app = FastAPI()
    app.state.sent = []

    @app.get("/{pid}")
    async def number(pid: str, request: Request):  # noqa: ARG001 — only the path has to match
        if not str(request.headers.get("authorization") or "").startswith("Bearer "):
            return JSONResponse({"error": {"message": "no token", "code": 190}}, status_code=401)
        return {"verified_name": "Team Agent", "display_phone_number": "+86 138 0000 0000"}

    @app.post("/{pid}/messages")
    async def send(pid: str, request: Request):
        app.state.sent.append({"pid": pid, "auth": request.headers.get("authorization"),
                               "body": json.loads((await request.body()) or b"{}")})
        if status >= 300:
            return JSONResponse(error or {"error": {"message": "nope"}}, status_code=status)
        return {"messages": [{"id": "wamid.OUT.1"}]}

    return app


def payload(text: str = "帮我看看这份用药方案", frm: str = NUMBER, mid: str = "wamid.IN.1",
            name: str = "Zhang", kind: str = "text") -> dict:
    msg: dict = {"from": frm, "id": mid, "timestamp": "1700000000", "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": NUMBER, "phone_number_id": PID},
            "contacts": [{"profile": {"name": name}, "wa_id": frm}],
            "messages": [msg],
        }}]}]}


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(cl: TestClient, data: dict, secret: str | None = APP_SECRET):
    body = json.dumps(data).encode()
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers[whatsapp.SIGNATURE_HEADER] = sign(body, secret)
    return cl.post("/hooks/whatsapp", content=body, headers=headers)


def wait_for(predicate, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def seed(tmp_path, **over) -> str:
    """Write the settings to disk first: `create_app` reads the public host while it builds the
    middleware, so the channel has to be configured before the app exists."""
    st = Store(tmp_path / "data")
    gid = st.list_groups()[0]["id"]
    st.update_settings({
        "whatsapp_enabled": True, "whatsapp_group_id": gid, "whatsapp_allowed": [NUMBER],
        "whatsapp_app_secret": APP_SECRET, "whatsapp_verify_token": VERIFY_TOKEN,
        "whatsapp_token": TOKEN, "whatsapp_phone_number_id": PID,
        "whatsapp_public_host": HOOK_HOST, "whatsapp_max_chars": 400, "plan_mode": "off",
        **over,
    })
    return gid


@contextlib.contextmanager
def channel(tmp_path, fake_reply: str = "我看过了,建议先核对剂量。", **over):
    """A live app on the public host, with the settings already in place.

    The `with` matters: `TestClient` only keeps one event loop alive for the duration of the
    context manager, and the round of collaboration runs as a background task on it — without
    it the task would be torn down with the request that started it.
    """
    gid = seed(tmp_path, **over)
    fake = FakeLLM(default=fake_reply)
    app = create_app(tmp_path / "data", completion_fn=fake)
    # Talk to the app on whichever host it was configured to accept: with no public host set, the
    # middleware only allows loopback, so a test of the empty case has to use 127.0.0.1.
    host = str(over.get("whatsapp_public_host", HOOK_HOST) or "127.0.0.1").strip()
    host = host.split("://")[-1].split("/")[0].split(":")[0].strip()
    with TestClient(app, base_url=f"http://{host or '127.0.0.1'}") as cl:
        yield cl, app, gid, fake


def user_texts(app, gid) -> list[str]:
    return [m["content"] for m in app.state.store.list_messages(gid) if m["sender_type"] == "user"]


def notices(app, gid) -> list[str]:
    return [m["content"] for m in app.state.store.list_messages(gid) if m["sender_type"] == "system"]


# ================================================================== unit: the gate
def test_a_signature_over_the_raw_body_is_what_authenticates_the_post():
    body = b'{"a":1}'
    assert whatsapp.verify_signature(APP_SECRET, body, sign(body))
    assert not whatsapp.verify_signature(APP_SECRET, body, sign(body, "another-secret"))
    assert not whatsapp.verify_signature(APP_SECRET, body, sign(body)[len("sha256="):])   # prefix missing
    assert not whatsapp.verify_signature(APP_SECRET, body, None)
    assert not whatsapp.verify_signature(APP_SECRET, body, "")
    # No secret configured must refuse rather than accept: otherwise an endpoint that is merely
    # unfinished would process whatever anyone posts at it.
    assert not whatsapp.verify_signature("", body, sign(body))


def test_the_handshake_needs_the_right_verify_token():
    assert whatsapp.challenge("subscribe", VERIFY_TOKEN, VERIFY_TOKEN, "12345") == "12345"
    assert whatsapp.challenge("subscribe", "wrong", VERIFY_TOKEN, "12345") is None
    assert whatsapp.challenge("unsubscribe", VERIFY_TOKEN, VERIFY_TOKEN, "12345") is None
    assert whatsapp.challenge("subscribe", VERIFY_TOKEN, "", "12345") is None


def test_only_text_is_usable_and_the_rest_are_named():
    messages, skipped = whatsapp.parse(payload("你好"))
    assert [m.text for m in messages] == ["你好"] and messages[0].name == "Zhang"
    assert skipped == []
    _, skipped = whatsapp.parse(payload(kind="image"))
    assert skipped == ["image"], "the caller has to be able to say what it could not carry"
    # delivery receipts are not messages and must not start a round
    receipts = {"entry": [{"changes": [{"value": {"statuses": [{"id": "s1", "status": "read"}]}}]}]}
    assert whatsapp.parse(receipts) == ([], [])


def test_numbers_are_normalized_but_not_split_on_spaces():
    """Regression: splitting on whitespace turned "+86 138-0000-0000" into two bogus entries
    ("86" and "13800000000"), so the real number never matched the allowlist."""
    assert whatsapp.numbers("+86 138-0000-0000") == [NUMBER]
    assert whatsapp.numbers(f"{NUMBER}, {OTHER}") == [NUMBER, OTHER]
    assert whatsapp.numbers([NUMBER, NUMBER, " " + OTHER]) == [NUMBER, OTHER]


def test_markdown_is_rewritten_into_whatsapp_markup():
    got = whatsapp.to_plain("## 结论\n\n这是**重点**,见 `p=0.03`。\n\n[指南](https://x.org/g)")
    assert "##" not in got and "*结论*" in got
    assert "*重点*" in got                        # **bold** becomes *bold*
    assert "```p=0.03```" in got                  # inline code becomes monospace
    assert "[指南]" not in got and "指南 (https://x.org/g)" in got


def test_the_same_message_id_is_only_handled_once():
    recent = base.Recent(3)
    assert recent.first_time("a") and not recent.first_time("a")
    for k in ("b", "c", "d"):
        recent.first_time(k)
    assert recent.first_time("a"), "the oldest id should have been evicted, not remembered forever"
    assert recent.first_time(""), "an event with no id is not a duplicate"


def test_a_sender_is_rate_limited():
    limiter = base.RateLimit(2)
    assert limiter.allow(OTHER) and limiter.allow(OTHER) and not limiter.allow(OTHER)
    limiter.forget(OTHER)
    assert limiter.allow(OTHER)


def test_a_failure_is_translated_into_the_thing_to_go_and_change():
    assert "24 hours" in whatsapp.explain_http(400, '{"error":{"code":131047,"message":"x"}}')
    assert "access token" in whatsapp.explain_http(401, '{"error":{"code":190,"message":"x"}}')
    assert "phone number id" in whatsapp.explain_http(404, "not json at all")


def test_only_declared_read_tools_survive_the_restriction():
    """Fail closed. Only an explicit `risk == "read"` is kept, so a risk word this code has
    never seen — and MCP tools, which carry no risk at all — are withheld rather than trusted."""
    tools = {
        "current_time": {"risk": "read"},
        "library_search": {"risk": "read"},
        "run_code": {"risk": "exec"},
        "memory_save": {"risk": "write"},
        "mystery": {"risk": "harmless-looking"},
        "some_mcp_tool": {"read_only": True},          # the MCP branch stores no `risk`
    }
    kept, withheld = toolhub.read_only_tools(tools)
    assert set(kept) == {"current_time", "library_search"}
    assert set(withheld) == {"run_code", "memory_save", "mystery", "some_mcp_tool"}


# ============================================================ the route, over HTTP
def test_the_handshake_echoes_the_challenge_for_the_configured_token(tmp_path):
    with channel(tmp_path) as (cl, _app, _gid, _fake):
        r = cl.get("/hooks/whatsapp", params={"hub.mode": "subscribe",
                                              "hub.verify_token": VERIFY_TOKEN,
                                              "hub.challenge": "12345"})
        assert r.status_code == 200 and r.text == "12345"
        bad = cl.get("/hooks/whatsapp", params={"hub.mode": "subscribe",
                                                "hub.verify_token": "nope",
                                                "hub.challenge": "12345"})
        assert bad.status_code == 403


def test_an_unconfigured_channel_refuses_instead_of_accepting(tmp_path):
    """503 rather than 200, so Meta keeps retrying while the user is still filling things in."""
    for name, over in (("off", {"whatsapp_enabled": False}),
                       ("no-secret", {"whatsapp_app_secret": ""})):
        with channel(tmp_path / name, **over) as (cl, app, gid, _fake):
            assert post(cl, payload()).status_code == 503
            assert user_texts(app, gid) == [], "nothing may run while the channel is unconfigured"


def test_a_post_without_a_valid_signature_is_refused(tmp_path):
    with channel(tmp_path) as (cl, app, gid, _fake):
        for secret in (None, "the-wrong-secret"):
            assert post(cl, payload(), secret=secret).status_code == 403
        assert user_texts(app, gid) == []


def test_a_message_from_a_number_that_is_not_allowlisted_is_dropped(tmp_path):
    with channel(tmp_path) as (cl, app, gid, _fake):
        r = post(cl, payload(frm=OTHER, name="Stranger"))
        assert r.status_code == 200 and r.json()["accepted"] == 0
        assert user_texts(app, gid) == []
        # and the user is told why nothing happened, rather than seeing silence
        assert any(OTHER in n for n in notices(app, gid))


def test_the_same_event_delivered_twice_only_runs_once(tmp_path):
    with channel(tmp_path) as (cl, app, gid, _fake):
        assert post(cl, payload(mid="wamid.SAME")).json()["accepted"] == 1
        again = post(cl, payload(mid="wamid.SAME"))
        assert again.status_code == 200 and again.json()["accepted"] == 0
        assert len(user_texts(app, gid)) == 1


def test_a_flood_from_one_number_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(api_channels, "PER_MINUTE", 2)     # read while the router is built
    with channel(tmp_path) as (cl, app, gid, _fake):
        accepted = [post(cl, payload(mid=f"wamid.F{i}")).json()["accepted"] for i in range(3)]
        assert accepted == [1, 1, 0]
        assert len(user_texts(app, gid)) == 2


def test_an_oversized_body_is_refused_before_it_is_parsed(tmp_path):
    with channel(tmp_path) as (cl, _app, _gid, _fake):
        r = cl.post("/hooks/whatsapp", content=b"x" * (whatsapp.MAX_BODY + 16),
                    headers={whatsapp.SIGNATURE_HEADER: "sha256=00"})
        assert r.status_code == 413


def test_the_answer_is_sent_back_to_whatsapp(tmp_path, monkeypatch):
    """The whole path: signed post in -> round -> POST to the Graph API carrying what that
    number actually needs, in WhatsApp's own markup and clipped to the configured length."""
    fake_graph = graph_like()
    with FakeServer(fake_graph) as server:
        monkeypatch.setattr(whatsapp, "GRAPH", server.url)
        with channel(tmp_path, fake_reply="## 结论\n\n先核对**剂量**。") as (cl, _app, _gid, _fake):
            assert post(cl, payload("帮我看看用药方案")).json()["accepted"] == 1
            assert wait_for(lambda: fake_graph.state.sent), "no reply was ever sent back"

    sent = fake_graph.state.sent[0]
    assert sent["auth"] == f"Bearer {TOKEN}"
    assert sent["pid"] == PID
    body = sent["body"]
    assert body["to"] == NUMBER                          # digits only, no "+"
    assert body["messaging_product"] == "whatsapp" and body["type"] == "text"
    assert "先核对*剂量*。" in body["text"]["body"]        # markdown converted for WhatsApp
    assert len(body["text"]["body"]) <= 400


def test_a_rejected_reply_is_recorded_rather_than_lost(tmp_path, monkeypatch):
    """Silence is the worst failure mode here: when WhatsApp refuses the reply, the reason has
    to reach the status the settings page reads."""
    fake_graph = graph_like(status=400, error={"error": {"code": 131047, "message": "outside window"}})
    with FakeServer(fake_graph) as server:
        monkeypatch.setattr(whatsapp, "GRAPH", server.url)
        with channel(tmp_path) as (cl, _app, _gid, _fake):
            assert post(cl, payload()).json()["accepted"] == 1
            assert wait_for(lambda: fake_graph.state.sent)
            assert wait_for(lambda: (cl.get("/api/channels/whatsapp").json()["counters"]
                                     ["last_reply"] or {}).get("ok") is False)
            last = cl.get("/api/channels/whatsapp").json()["counters"]["last_reply"]
            assert "24 hours" in last["detail"], "the 24-hour window has to be named as the reason"


@pytest.mark.parametrize("typed,want", [
    ("hook.example.com", "https://hook.example.com/hooks/whatsapp"),
    ("hook.example.com/", "https://hook.example.com/hooks/whatsapp"),
    ("  hook.example.com  ", "https://hook.example.com/hooks/whatsapp"),
    ("https://hook.example.com", "https://hook.example.com/hooks/whatsapp"),
    ("https://hook.example.com/", "https://hook.example.com/hooks/whatsapp"),
    ("http://a.example.com/base/", "http://a.example.com/base/hooks/whatsapp"),
    ("https://hook.example.com/hooks/whatsapp", "https://hook.example.com/hooks/whatsapp"),
    ("https://hook.example.com/hooks/whatsapp/", "https://hook.example.com/hooks/whatsapp"),
    ("", ""),
])
def test_the_callback_url_is_built_from_whatever_was_typed(tmp_path, typed, want):
    """Every shape in this list is something a real person types, and a wrong URL here stays
    invisible until Meta's verification request never arrives — the endpoint just looks dead.

    Regression: a scheme-bearing value used to be returned as-is, so "https://host/" produced
    "https://host" with the whole path missing, and the page appended the path a second time.
    """
    with channel(tmp_path, whatsapp_public_host=typed) as (cl, _app, _gid, _fake):
        assert cl.get("/api/channels/whatsapp").json()["public_url"] == want


def test_the_status_reports_what_arrived_and_what_was_ignored(tmp_path):
    with channel(tmp_path) as (cl, _app, _gid, _fake):
        status = cl.get("/api/channels/whatsapp").json()
        assert status["settings"]["enabled"] is True
        assert status["public_url"] == f"https://{HOOK_HOST}/hooks/whatsapp"
        assert status["settings"]["allowed"] == [NUMBER]
        post(cl, payload(frm=OTHER))
        counters = cl.get("/api/channels/whatsapp").json()["counters"]
        assert counters["ignored"] >= 1 and counters["last_inbound"]["sender"] == OTHER


def test_the_secrets_are_stored_but_never_read_back(tmp_path):
    """The API half of the promise: the response says a secret is saved, and returns nothing."""
    with channel(tmp_path) as (cl, _app, _gid, _fake):
        s = cl.get("/api/settings").json()
        assert s["whatsapp_app_secret"] == "" and s["whatsapp_token"] == ""
        assert s["whatsapp_app_secret_set"] is True and s["whatsapp_token_set"] is True
        assert s["whatsapp_verify_token_set"] is True
        dump = json.dumps(s, ensure_ascii=False)
        for secret in (APP_SECRET, TOKEN, VERIFY_TOKEN):
            assert secret not in dump


def test_the_whatsapp_secrets_go_through_the_keychain_like_the_other_keys(tmp_path, monkeypatch):
    """The storage half. Registering them in `SECRET_SETTINGS` is what makes the database hold a
    reference rather than the value; without that they would sit in the clear and travel out in
    every backup. Tests run with TEAM_AGENT_NO_KEYCHAIN, where the documented fallback stores
    them as-is, so the fake keychain is installed to exercise the real path.
    """
    from tests.test_compliance import _FakeKeychain

    kc = _FakeKeychain().install(monkeypatch)
    st = Store(tmp_path / "data")
    st.update_settings({"whatsapp_app_secret": APP_SECRET, "whatsapp_token": TOKEN,
                        "whatsapp_verify_token": VERIFY_TOKEN})
    row = st._one("SELECT value FROM settings WHERE key='whatsapp_app_secret'")
    assert json.loads(row["value"]) == "keychain:whatsapp:app-secret"
    assert kc.items["whatsapp:app-secret"] == APP_SECRET
    assert kc.items["whatsapp:access-token"] == TOKEN
    assert kc.items["whatsapp:verify-token"] == VERIFY_TOKEN
    assert st.get_settings()["whatsapp_app_secret"] == APP_SECRET, "resolved back for the app"
    # clearing it must remove the keychain entry too, not leave a stale secret behind
    st.update_settings({"whatsapp_app_secret": ""})
    assert "whatsapp:app-secret" not in kc.items


def test_the_public_host_is_accepted_but_other_hosts_are_not(tmp_path):
    """Allowing the tunnel's hostname must not open the API to any Host header — everything
    else is still rejected, which is what keeps DNS rebinding away from the local API."""
    seed(tmp_path, whatsapp_public_host=f"https://{HOOK_HOST}/")
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"))
    assert TestClient(app, base_url=f"http://{HOOK_HOST}").get("/api/health").status_code == 200
    assert TestClient(app, base_url="http://elsewhere.example.com").get("/api/health").status_code == 400
    assert TestClient(app, base_url="http://127.0.0.1").get("/api/health").status_code == 200


# ============================== the read-only ceiling, through a real round
async def test_the_round_context_can_be_restricted_to_read_only_tools(store, make_router):
    """`RunState.read_only` is what the settings page promises: input that arrives over the
    network never reaches exec/write tools."""
    store.update_settings({"code_enabled": True})
    orch, group = setup(store, make_router, FakeLLM(default="好"))
    agent = store.list_agents()[0]

    open_ctx = await orch.toolhub.context(group, agent, connect=False)
    assert "run_code" in open_ctx.tools, "the fixture is meant to have an exec tool available"

    locked = await orch.toolhub.context(group, agent, connect=False, read_only=True)
    assert "run_code" not in locked.tools
    assert locked.tools, "reading is still allowed"
    assert all(s.get("risk") == "read" for s in locked.tools.values())
    assert any("read-only" in p for p in locked.problems)


def test_a_round_triggered_over_the_webhook_is_read_only(tmp_path):
    """End to end: with code execution switched on for the group, a WhatsApp round still
    withholds the exec tool, says so, and never puts `run_code` in front of the model."""
    with channel(tmp_path, code_enabled=True, fake_reply="好") as (cl, app, gid, fake):
        assert post(cl, payload("跑个脚本看看")).json()["accepted"] == 1
        assert wait_for(lambda: any(m["sender_type"] == "agent"
                                    for m in app.state.store.list_messages(gid)))
        assert any("read-only" in n for n in notices(app, gid))
        prompts = ["".join(p["content"] for p in msgs) for _model, msgs in fake.calls]
        assert prompts and not any("run_code" in p for p in prompts)
        # and the message is attributed, so later rounds know where it came from
        assert any("WhatsApp · Zhang" in m["sender_name"]
                   for m in app.state.store.list_messages(gid) if m["sender_type"] == "user")
