"""Video generation through a self-hosted MiniMax H3 server.

The server is faked with an httpx transport, so what is under test is our half of the contract:
the request body, the polling, where the file lands, and every path that refuses a call before it
costs GPU time. H3 itself is a diffusion transformer — an endpoint nobody runs on a laptop — so
nothing here pretends to check picture quality.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import video
from app.main import create_app
from app.toolhub import builtin_specs, timeout_budget
from tests.conftest import FakeLLM
from tests.test_collab import setup

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


class FakeServer:
    """Stands in for the SGLang video server: records what it was asked, replays a script."""

    def __init__(self, statuses=("completed",), body=MP4, submit_status=200,
                 content_length=None, key_expected="", detail=None):
        self.statuses = list(statuses)
        self.body = body
        self.submit_status = submit_status
        self.content_length = content_length
        self.key_expected = key_expected
        self.detail = detail or {}
        self.payloads: list[dict] = []
        self.paths: list[str] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if self.key_expected and request.headers.get("authorization") != f"Bearer {self.key_expected}":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        if request.method == "POST" and request.url.path.endswith("/v1/videos"):
            self.payloads.append(json.loads(request.content))
            if self.submit_status >= 400:
                return httpx.Response(self.submit_status, json={"detail": "the server said no"})
            return httpx.Response(200, json={"id": "vid-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=self.body,
                                  headers={} if self.content_length is None else {"Content-Length": str(self.content_length)})
        i = min(self.polls, len(self.statuses) - 1)
        self.polls += 1
        return httpx.Response(200, json={"status": self.statuses[i], **self.detail})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Production polls every couple of seconds; the loop is what is under test, not the waiting."""
    monkeypatch.setattr(video, "POLL_START", 0.01)
    monkeypatch.setattr(video, "POLL_MAX", 0.02)


@pytest.fixture
def fake(monkeypatch):
    """`srv = fake(**kwargs)` builds a server and points the tool at it.

    The tool's own call is left untouched — only the transport underneath it is replaced — so a
    wrong keyword or a missing argument still fails here.
    """
    real = video.generate

    def make(**kw):
        srv = FakeServer(**kw)

        async def wrapped(prov, payload, **rest):
            rest["client"] = srv.client()
            return await real(prov, payload, **rest)

        monkeypatch.setattr(video, "generate", wrapped)
        return srv

    return make


@pytest.fixture
def video_env(store, make_router):
    """A group whose host may generate video, with the H3 preset added."""
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 10, "video_max_mb": 8})
    prov = store.add_provider_from_preset("minimax-h3")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g, prov


async def generate(orch, store, g, args=None, approve=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    return await orch.toolhub.call(ctx, "generate_video", {"prompt": "a cat asleep on a warm laptop", **(args or {})}, approve)


def video_dir(store, g):
    return Path(store.data_dir) / "workspaces" / g["id"] / "video"


# ------------------------------------------------------------------ the request body
def test_the_request_body_matches_the_released_contract():
    """The shape is taken from the repository's own reproducible-768p scripts; a server built
    against those scripts must not have to guess what we meant."""
    p = video.build_payload("a cat asleep on a sofa", short_edge=768, aspect_ratio="16:9",
                            duration_seconds=10, seed=0)
    assert p == {"task": "t2va", "prompt": "a cat asleep on a sofa", "conditions": [],
                 "target": {"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 10},
                 "seed": 0}


def test_frames_turn_it_into_the_first_last_frame_mode():
    p = video.build_payload("x", short_edge=768, aspect_ratio="9:16", duration_seconds=4, seed=7,
                            first_frame="file:///w/a.png", last_frame="file:///w/b.png")
    assert p["task"] == "fl2va"
    assert p["conditions"] == [
        {"type": "image", "uri": "file:///w/a.png", "role": "keyframe", "frame_index": 0},
        {"type": "image", "uri": "file:///w/b.png", "role": "keyframe", "frame_index": -1},
    ]


def test_the_whole_9_to_16_single_frame_case_is_one_condition():
    p = video.build_payload("x", short_edge=768, aspect_ratio="9:16", duration_seconds=4, seed=0,
                            first_frame="https://example.com/a.png")
    assert p["task"] == "fl2va" and len(p["conditions"]) == 1


# ------------------------------------------------------------------ the happy path
async def test_a_rendered_clip_lands_in_the_group_workspace(video_env, fake):
    orch, store, g, prov = video_env
    srv = fake()
    other = store.create_group("Another project")

    out = await generate(orch, store, g, {"duration_seconds": 8, "aspect_ratio": "16:9"})

    assert out.ok, out.text
    path = video_dir(store, g) / out.files[0]["name"]
    assert path.read_bytes() == MP4
    assert out.files == [{"kind": "video", "name": path.name, "bytes": len(MP4), "seconds": 8}]
    # What was sent is what was asked for
    assert srv.payloads[0]["prompt"].startswith("a cat asleep")
    assert srv.payloads[0]["target"] == {"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 8}
    # The path is in the text as well, so the transcript is readable on its own
    assert str(path) in out.text
    # …and the model is told not to make up what the clip shows
    assert "cannot watch" in out.text
    # The neighbouring group's workspace is untouched
    assert not video_dir(store, other).exists()


async def test_it_polls_until_the_clip_is_ready_and_downloads_once(video_env, fake):
    orch, store, g, prov = video_env
    srv = fake(statuses=("queued", "running", "completed"))

    out = await generate(orch, store, g)

    assert out.ok
    assert srv.polls == 3
    assert sum(1 for p in srv.paths if p.endswith("/content")) == 1


async def test_the_key_is_sent_as_a_bearer_token(store, make_router, fake):
    store.update_settings({"video_enabled": True})
    prov = store.add_provider_from_preset("minimax-h3", api_key="sk-video-123456")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    srv = fake(key_expected="sk-video-123456")

    out = await generate(orch, store, g)

    assert out.ok and srv.payloads


async def test_a_frame_inside_the_workspace_is_handed_over_as_a_file_url(video_env, fake):
    orch, store, g, prov = video_env
    ws = Path(store.data_dir) / "workspaces" / g["id"]
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    srv = fake()

    out = await generate(orch, store, g, {"first_frame": "cat.png"})

    assert out.ok
    assert srv.payloads[0]["task"] == "fl2va"
    assert srv.payloads[0]["conditions"][0]["uri"] == f"file://{(ws / 'cat.png').resolve()}"


# ------------------------------------------------------------------ refusals
async def test_it_asks_before_rendering(store, make_router, fake):
    """An exec-risk tool under the default permission mode asks every single time."""
    store.update_settings({"video_enabled": True, "perm_mode": "ask_risky"})
    store.add_provider_from_preset("minimax-h3")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    srv = fake()
    asked = []

    async def approve(spec, args):
        asked.append(spec["name"])
        return False

    out = await generate(orch, store, g, approve=approve)

    assert asked == ["generate_video"] and out.denied and not out.ok
    assert srv.payloads == []                      # nothing reached the server
    assert not video_dir(store, g).exists()        # and nothing was written


async def test_a_failed_render_is_reported_and_leaves_no_file(video_env, fake):
    orch, store, g, prov = video_env
    srv = fake(statuses=("failed",), detail={"error": "CUDA out of memory"})

    out = await generate(orch, store, g)

    assert not out.ok and "CUDA out of memory" in out.text and out.files == []
    d = video_dir(store, g)
    assert not d.exists() or not list(d.glob("*"))


async def test_a_render_that_never_finishes_gives_up_and_says_so(video_env, fake, monkeypatch):
    orch, store, g, prov = video_env
    monkeypatch.setattr(video, "MIN_DEADLINE", 0.05)     # instead of waiting out the real floor
    srv = fake(statuses=("queued",))

    out = await generate(orch, store, g)

    assert not out.ok and "still not ready" in out.text and "queued" in out.text
    assert srv.polls >= 1
    # Nothing on disk at all: no clip, and no half-written `.part` either
    d = video_dir(store, g)
    assert not d.exists() or not list(d.glob("*"))


async def test_an_oversized_clip_is_refused_before_it_is_saved(video_env, fake):
    orch, store, g, prov = video_env
    store.update_settings({"video_max_mb": 1})
    srv = fake(body=b"x" * (1024 * 1024 + 100))

    out = await generate(orch, store, g)

    assert not out.ok and "MB" in out.text
    d = video_dir(store, g)
    assert not d.exists() or not list(d.glob("*"))
    assert srv.paths[-1].endswith("/content")            # it was downloaded, then rejected


async def test_a_declared_size_over_the_cap_is_refused_without_downloading_it(video_env, fake):
    orch, store, g, prov = video_env
    store.update_settings({"video_max_mb": 1})
    srv = fake(content_length=99 * 1024 * 1024, body=b"x" * 10)

    out = await generate(orch, store, g)

    assert not out.ok


async def test_a_frame_outside_the_workspace_is_refused(video_env, fake):
    """The workspace is a boundary for the images too — otherwise the server would be handed a
    path to any file on this machine."""
    orch, store, g, prov = video_env
    srv = fake()

    out = await generate(orch, store, g, {"first_frame": "../../../etc/passwd"})

    assert not out.ok and "outside this group's workspace" in out.text
    assert srv.payloads == []


async def test_a_frame_that_is_not_there_is_refused(video_env, fake):
    orch, store, g, prov = video_env
    srv = fake()

    out = await generate(orch, store, g, {"first_frame": "not-here.png"})

    assert not out.ok and "no file at" in out.text and srv.payloads == []


async def test_a_bad_aspect_ratio_is_refused_with_the_list(video_env, fake):
    orch, store, g, prov = video_env
    srv = fake()

    out = await generate(orch, store, g, {"aspect_ratio": "16:10"})

    assert not out.ok and "16:10" in out.text and "21:9" in out.text and srv.payloads == []


async def test_a_server_that_refuses_the_request_is_quoted_back(video_env, fake):
    orch, store, g, prov = video_env
    srv = fake(submit_status=503)

    out = await generate(orch, store, g)

    assert not out.ok and "the server said no" in out.text and srv.payloads


async def test_a_too_long_clip_is_clamped_and_the_clamp_is_declared(video_env, fake):
    """H3 tops out at 15s and the group may allow less; silently shortening a request would make
    the model report a duration that never happened."""
    orch, store, g, prov = video_env
    srv = fake()

    out = await generate(orch, store, g, {"duration_seconds": 60})

    assert out.ok
    assert srv.payloads[0]["target"]["duration_seconds"] == 10      # the group's own cap
    assert "10" in out.text and "adjusted" in out.text


# ------------------------------------------------------------------ availability
async def test_the_tool_is_absent_until_video_generation_is_on(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    assert "generate_video" not in ctx.tools


async def test_video_on_without_a_provider_says_so_instead_of_hiding(store, make_router):
    store.update_settings({"video_enabled": True})
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    assert "generate_video" not in ctx.tools
    assert any("no video provider" in p for p in ctx.problems)


async def test_a_non_local_video_provider_is_blocked_while_outbound_calls_are_off(store, make_router):
    """Same rule as the chat router: `is_local` is what lets something run offline, so a rented
    GPU box must be marked non-local or the offline switch would not govern it."""
    store.update_settings({"video_enabled": True, "external_calls_enabled": False})
    p = store.add_provider_from_preset("minimax-h3")
    store.update_provider(p["id"], {"is_local": False, "base_url": "https://gpu.example.com"})
    orch, g = setup(store, make_router, FakeLLM(default="好"))

    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)

    assert "generate_video" not in ctx.tools
    assert any("outbound calls are switched off" in x for x in ctx.problems)


def test_the_video_tool_gets_its_own_time_budget():
    """Rendering is minutes; `tool_timeout` is sized for a call that answers quickly. Without the
    override the render would be killed after the work had already been done."""
    cfg = {"tool_timeout": 60, "video_timeout": 900}
    assert timeout_budget(cfg, builtin_specs()["generate_video"]) == 900
    assert timeout_budget(cfg, builtin_specs()["run_code"]) == 60
    assert timeout_budget(cfg, {"name": "current_time"}) == 60      # no key -> the shared limit


# ------------------------------------------------------------------ the probe
async def test_the_probe_treats_404_as_a_live_server():
    """A real server answers 404 for a task id that does not exist. That still proves it is up,
    and it costs no GPU time — which is the whole point of probing before generating."""
    def handler(request):
        return httpx.Response(404)

    prov = {"base_url": "http://127.0.0.1:30010", "api_key": ""}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert ok and "404" in detail


async def test_the_probe_reports_a_dead_address():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    prov = {"base_url": "http://127.0.0.1:30010", "api_key": ""}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert not ok and "30010" in detail


async def test_the_probe_reports_a_rejected_key():
    def handler(request):
        return httpx.Response(403, json={"message": "nope"})

    prov = {"base_url": "http://127.0.0.1:30010", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert not ok and "403" in detail


# ------------------------------------------------------------------ the API surface
@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = tmp_path / "data"
        yield c


def gid(c):
    return c.get("/api/groups").json()[0]["id"]


def test_a_media_provider_never_becomes_a_chat_model(client):
    before = client.get("/api/route/preview").json()["chain"]
    client.post("/api/providers", json={"preset": "minimax-h3"})

    prov = next(p for p in client.get("/api/providers").json() if p["id"] == "minimax-h3")
    assert prov["kind"] == "minimax_video" and prov["models"] == []
    assert [m for m in client.get("/api/models").json() if m["provider_id"] == "minimax-h3"] == []
    # …and it does not slip into the routing chain either
    assert client.get("/api/route/preview").json()["chain"] == before


def test_even_a_hand_added_model_stays_out_of_the_chat_list(client):
    """A media provider has no model string at all; one added by mistake must not become
    selectable for a member."""
    client.post("/api/providers", json={"preset": "minimax-h3"})
    client.post("/api/providers/minimax-h3/models", json={"model_name": "minimax-h3"})
    assert [m for m in client.get("/api/models").json() if m["provider_id"] == "minimax-h3"] == []


def test_capabilities_say_whether_video_can_run(client):
    g = gid(client)
    assert client.get(f"/api/groups/{g}/capabilities").json()["video"]["enabled"] is False

    client.put("/api/settings", json={"video_enabled": True})
    v = client.get(f"/api/groups/{g}/capabilities").json()["video"]
    assert v["enabled"] and v["provider"] is None and "no video provider" in v["problem"]

    client.post("/api/providers", json={"preset": "minimax-h3"})
    v = client.get(f"/api/groups/{g}/capabilities").json()["video"]
    assert v["provider"]["id"] == "minimax-h3" and v["problem"] == ""


def test_the_video_settings_are_all_writable(client):
    """Anything numeric that the UI can change has to be accepted here too."""
    for k, val in (("video_short_edge", 512), ("video_max_seconds", 6), ("video_timeout", 1800), ("video_max_mb", 128)):
        assert client.put("/api/settings", json={k: val}).status_code == 200, k
    assert client.put("/api/settings", json={"video_max_seconds": 99}).status_code == 400
    assert client.put("/api/settings", json={"video_provider_id": "minimax-h3"}).status_code == 200


def test_the_probe_endpoint_explains_each_state(client, monkeypatch):
    assert client.post("/api/video/test", json={}).json()["provider"] is None

    client.post("/api/providers", json={"preset": "minimax-h3"})

    async def up(prov, **kw):
        return True, "answered"

    monkeypatch.setattr(video, "probe", up)
    r = client.post("/api/video/test", json={}).json()
    assert r["ok"] and r["provider"]["id"] == "minimax-h3"

    client.put("/api/settings", json={"external_calls_enabled": False})
    client.patch("/api/providers/minimax-h3", json={"is_local": False})
    r = client.post("/api/video/test", json={}).json()
    assert not r["ok"] and "outbound calls are switched off" in r["detail"]


def test_the_rendered_clip_is_served_from_its_own_group_only(client):
    g = gid(client)
    other = client.post("/api/groups", json={"name": "Another project"}).json()["id"]
    d = Path(client.data) / "workspaces" / g / "video"
    d.mkdir(parents=True)
    (d / "clip.mp4").write_bytes(MP4)

    r = client.get(f"/api/groups/{g}/video/clip.mp4")
    assert r.status_code == 200 and r.content == MP4 and r.headers["content-type"] == "video/mp4"
    assert client.get(f"/api/groups/{g}/video/missing.mp4").status_code == 404
    assert client.get(f"/api/groups/{other}/video/clip.mp4").status_code == 404
    # A name that is not a plain filename never reaches the filesystem
    assert client.get(f"/api/groups/{g}/video/x.txt").status_code == 400
    assert client.get(f"/api/groups/{g}/video/.hidden.mp4").status_code == 400
    for bad in ("..%2f..%2fclip.mp4", "%2e%2e%2f%2e%2e%2fpasswd.mp4"):
        assert client.get(f"/api/groups/{g}/video/{bad}").status_code in (400, 404)
