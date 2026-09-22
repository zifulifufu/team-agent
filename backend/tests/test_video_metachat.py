"""Video generation through MetaChat's open media API (`api.mmchat.xyz/open/v1`).

The second job shape `app/video.py` speaks, and the one whose properties are easiest to get wrong
because they are *not* H3's: the body is `{prompt, model, params}`, the job is polled on
`video/result/{id}`, the finished file is fetched from a link the service reports, and both models
need a reference image.

The service is faked with an httpx transport, so what is under test is our half of the contract —
the body, the polling, where the file lands, and every refusal that happens before a job is paid
for. Nothing here talks to MetaChat: a real generation costs money and is not something a test
should be able to trigger.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import discovery, media, modelopts, toolhub, video
from app.main import create_app
from app.toolhub import builtin_specs, timeout_budget
from tests.conftest import FakeLLM
from tests.test_collab import setup

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048

GENERATE = "/open/v1/video/generate"
RESULT = "/open/v1/video/result/"
LINK = "https://mc-oss-cn.oss-cn-shanghai.aliyuncs.com/clip.mp4"
MODEL = "grok-imagine-video-1.5-preview"
FRAME = "https://example.com/ref.jpg"


class FakeMeta:
    """Stands in for MetaChat's media API: records what it was asked, replays a script.

    Both endpoints answer 200 with an envelope even when they refuse — `status` is `Success` or
    `Fail`, and the reason is in `message` — which is the property our error handling has to get
    right, so the fake reproduces it rather than using status codes for everything.
    """

    def __init__(self, states=("success",), submit_status=200, submit_body=None,
                 key_expected="", fail_reason="", with_url=True, points=1775,
                 query_status="Success", query_message="", video=MP4):
        self.states = list(states)
        self.submit_status = submit_status
        self.submit_body = submit_body
        self.key_expected = key_expected
        self.fail_reason = fail_reason
        self.with_url = with_url
        self.points = points
        self.query_status = query_status
        self.query_message = query_message
        self.video = video
        self.payloads: list[dict] = []
        self.paths: list[str] = []
        # (path, Authorization header) for every request — the download assertion needs it
        self.auth: list[tuple[str, str]] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.auth.append((request.url.path, request.headers.get("authorization", "")))
        api = request.url.path in (GENERATE,) or request.url.path.startswith(RESULT)
        if api and self.key_expected and request.headers.get("authorization") != f"Bearer {self.key_expected}":
            return httpx.Response(401, json={"status": "authentication_error", "message": "Unauthorized Request"})
        if request.method == "POST" and request.url.path == GENERATE:
            self.payloads.append(json.loads(request.content))
            if self.submit_status >= 400:
                return httpx.Response(self.submit_status,
                                      json={"status": "authentication_error", "message": "Unauthorized Request"})
            if self.submit_body is not None:
                return httpx.Response(200, json=self.submit_body)
            return httpx.Response(200, json={"status": "Success", "message": "视频生成任务创建成功",
                                             "data": {"id": "job-1", "prompt": "x", "model": MODEL}})
        if request.url.path.startswith(RESULT):
            if self.query_status != "Success":
                return httpx.Response(200, json={"status": self.query_status,
                                                 "message": self.query_message, "data": None})
            i = min(self.polls, len(self.states) - 1)
            self.polls += 1
            data: dict = {"id": "job-1", "status": self.states[i], "progress": 50, "prompt": "x"}
            if self.fail_reason:
                data["fail_reason"] = self.fail_reason
            if self.states[i] == "success":
                data["total_points"] = self.points
                if self.with_url:
                    data["video_url"] = LINK
            return httpx.Response(200, json={"status": "Success", "message": "视频任务查询成功", "data": data})
        return httpx.Response(200, content=self.video)      # the object-store link

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Production polls every few seconds; the loop is what is under test, not the waiting."""
    monkeypatch.setattr(video, "POLL_START", 0.01)
    monkeypatch.setattr(video, "POLL_MAX", 0.02)


@pytest.fixture
def meta(monkeypatch):
    """`srv = meta(**kwargs)` builds the service and points the tool at it.

    Only the transport underneath is replaced, exactly as in `test_video.py`: a wrong keyword or a
    missing argument still fails here.
    """
    real = video.generate

    def make(**kw):
        srv = FakeMeta(**kw)

        async def wrapped(prov, payload, **rest):
            rest["client"] = srv.client()
            return await real(prov, payload, **rest)

        monkeypatch.setattr(video, "generate", wrapped)
        return srv

    return make


@pytest.fixture
def meta_env(store, make_router):
    """A group whose video provider is MetaChat's media API, with a model already chosen."""
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 10,
                           "video_max_mb": 8, "video_model": MODEL})
    prov = store.add_provider_from_preset("metachat-video", api_key="sk-mc-123456")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g, prov


async def generate(orch, store, g, args=None, approve=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    return await orch.toolhub.call(ctx, "generate_video",
                                   {"prompt": "a cat asleep on a warm laptop", **(args or {})}, approve)


def video_dir(store, g):
    return Path(store.data_dir) / "workspaces" / g["id"] / "video"


# ------------------------------------------------------------------ what the preset ships
async def test_the_media_api_has_no_list_to_fetch_so_the_ids_ship_with_the_app():
    """`/open/v1/models` and `/open/v1/video/models` both answer 404, so there is nothing to ask for.

    Spelled as an assertion about the *network*: a refresh of this kind must not make a request at
    all, because the honest thing to do here is answer from the table rather than invent an endpoint.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request should have been made, got {request.url}")

    prov = {"kind": "metachat_video", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await discovery.fetch_models(prov, client=c)

    assert [e["id"] for e in got] == sorted(media.BUILTIN_MEDIA_MODELS["metachat_video"])
    assert {e["mode"] for e in got} == {"video"}, "marked for what they are, not left to a name guess"


def test_the_preset_seeds_both_models(store):
    prov = store.add_provider_from_preset("metachat-video")
    assert prov["kind"] == "metachat_video"
    assert sorted(m["model_name"] for m in store.list_provider_models(prov["id"])) == \
        sorted(media.BUILTIN_MEDIA_MODELS["metachat_video"])


def test_a_refresh_makes_them_selectable_in_the_video_settings(store):
    """The end the user actually cares about: press refresh, then the setting can name them."""
    prov = store.add_provider_from_preset("metachat-video")
    asyncio.run(modelopts.refresh_live(store, prov["id"]))

    assert [m["id"] for m in modelopts.models_for_use(store, prov["id"], "video")] == \
        sorted(media.BUILTIN_MEDIA_MODELS["metachat_video"])
    # and the provider is offered for video, without being mistaken for something a member can chat with
    assert prov["id"] in [p["id"] for p in store.providers_for_use("video", video.KINDS)]
    assert [m["id"] for m in store.list_models() if m["provider_id"] == prov["id"]] == []


def test_the_media_options_endpoint_offers_it_after_a_refresh(tmp_path):
    client = TestClient(create_app(tmp_path / "app" / "data", completion_fn=None, background=False),
                        base_url="http://127.0.0.1")
    pid = client.post("/api/providers", json={"preset": "metachat-video", "api_key": "sk-not-real"}).json()["id"]
    assert client.post(f"/api/providers/{pid}/model-options/refresh").status_code == 200

    body = client.get("/api/media/options?use=video").json()
    assert [p["id"] for p in body["providers"]] == [pid]
    assert [m["id"] for m in body["providers"][0]["models"]] == \
        sorted(media.BUILTIN_MEDIA_MODELS["metachat_video"])
    # …and nothing here claims to be able to draw
    assert client.get("/api/media/options?use=image").json()["providers"] == []


# ------------------------------------------------------------------ the request body
def test_the_request_body_matches_the_documented_contract():
    p = video.metachat_payload("给女孩带上墨镜", model=MODEL, ratio="adaptive", duration_seconds=5,
                               short_edge=768, frame=FRAME)
    assert p == {"prompt": "给女孩带上墨镜", "model": MODEL,
                 "params": {"duration": 5, "ratio": "adaptive", "resolution": "720p"},
                 "images": [{"url": FRAME}]}


def test_no_reference_image_means_an_empty_images_array():
    p = video.metachat_payload("x", model=MODEL, ratio="adaptive", duration_seconds=5, short_edge=768)
    assert p["images"] == []


def test_the_pixel_setting_is_mapped_onto_the_two_resolutions_it_takes():
    """A named resolution rather than a pixel count, so the user's "output short edge" has to land
    on one of them — and the default 768 has to mean the better one rather than the cheaper one."""
    assert video.resolution_for(768) == "720p"
    assert video.resolution_for(1280) == "720p"
    assert video.resolution_for(640) == "480p"
    assert video.resolution_for(128) == "480p"
    assert video.resolution_for("nonsense") == "720p"


def test_the_two_kinds_state_their_own_ranges():
    """H3 accepts 4-15 seconds and MetaChat 1-15, so a 3-second request is right on one and
    impossible on the other; the group's own cap applies on top of whichever it is."""
    assert video.clamp_seconds(3, 15, "minimax_video") == (4, True)
    assert video.clamp_seconds(3, 15, "metachat_video") == (3, False)
    assert video.clamp_seconds(60, 10, "metachat_video") == (10, True)
    assert "adaptive" in video.shape_of("metachat_video")["ratios"]
    assert "adaptive" not in video.shape_of("minimax_video")["ratios"]
    assert "21:9" in video.shape_of("minimax_video")["ratios"]
    assert "21:9" not in video.shape_of("metachat_video")["ratios"]


# ------------------------------------------------------------------ the happy path
async def test_a_rendered_clip_lands_in_the_group_workspace(meta_env, meta):
    orch, store, g, prov = meta_env
    srv = meta()
    other = store.create_group("Another project")

    out = await generate(orch, store, g, {"duration_seconds": 5, "first_frame": FRAME})

    assert out.ok, out.text
    path = video_dir(store, g) / out.files[0]["name"]
    assert path.read_bytes() == MP4
    # What was sent is what was asked for, in the shape that API documents
    assert srv.payloads[0]["model"] == MODEL
    assert srv.payloads[0]["params"] == {"duration": 5, "ratio": "adaptive", "resolution": "720p"}
    assert srv.payloads[0]["images"] == [{"url": FRAME}]
    # Polled on the documented path, and the neighbouring group's workspace is untouched
    assert any(p.startswith(RESULT) for p in srv.paths)
    assert not video_dir(store, other).exists()
    # "with sound" is an H3 property and must not be claimed here
    assert "with sound" not in out.text
    assert "cannot watch" in out.text


async def test_it_polls_until_the_job_is_ready_and_downloads_once(meta_env, meta):
    orch, store, g, prov = meta_env
    srv = meta(states=("submitted", "in_progress", "success"))

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert out.ok, out.text
    assert srv.polls == 3
    assert sum(1 for p in srv.paths if p.endswith("/clip.mp4")) == 1


async def test_the_key_goes_to_metachat_and_nowhere_else(meta_env, meta):
    """The video link points at MetaChat's object storage. Sending the key along would hand a
    credential to a third party for a plain file, so the download must not carry it."""
    orch, store, g, prov = meta_env
    srv = meta(key_expected="sk-mc-123456")

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert out.ok, out.text
    api = [h for path, h in srv.auth if path in (GENERATE,) or path.startswith(RESULT)]
    assert api and all(h == "Bearer sk-mc-123456" for h in api)
    download = [h for path, h in srv.auth if path.endswith("/clip.mp4")]
    assert download == [""], "the object-store link must be fetched without the API key"


async def test_what_the_job_cost_is_reported(meta_env, meta):
    """MetaChat bills in its own points and tells us the total; hiding it would be the wrong call."""
    orch, store, g, prov = meta_env
    meta(points=1775)

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert out.ok and "1775" in out.text


# ------------------------------------------------------------------ failures the API reports in the body
async def test_a_failed_job_reports_the_reason_it_gave(meta_env, meta):
    orch, store, g, prov = meta_env
    meta(states=("in_progress", "failure"), fail_reason="参考图无法下载")

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "参考图无法下载" in out.text
    assert not video_dir(store, g).exists()


async def test_a_refusal_carried_in_a_200_is_still_a_refusal(meta_env, meta):
    """MetaChat answers 200 with `status: "Fail"`; reading only the status code would call that a
    success and then fail confusingly on a missing task id."""
    orch, store, g, prov = meta_env
    meta(submit_body={"status": "Fail", "message": "绘图描述中存在错误的参数，请修改后重新提交。", "data": None})

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "错误的参数" in out.text


async def test_a_query_that_comes_back_as_fail_is_reported(meta_env, meta):
    orch, store, g, prov = meta_env
    meta(query_status="Fail", query_message="任务不存在")

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "任务不存在" in out.text


async def test_a_rejected_key_says_which_key(meta_env, meta):
    orch, store, g, prov = meta_env
    meta(submit_status=401)

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "API key" in out.text
    assert "sk-mc-123456" not in out.text, "the refusal must not echo the key into the transcript"


async def test_a_finished_job_with_no_link_is_refused_rather_than_saved(meta_env, meta):
    orch, store, g, prov = meta_env
    meta(with_url=False)

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "no video address" in out.text
    assert not video_dir(store, g).exists()


# ------------------------------------------------------------------ refusals made before paying
async def test_a_local_path_cannot_be_sent_as_a_reference_image(meta_env, meta):
    """MetaChat downloads the picture itself and has no upload endpoint, so a file on this machine
    is not something it can be given. Saying so beats sending a path it cannot open."""
    orch, store, g, prov = meta_env
    srv = meta()
    ws = Path(store.data_dir) / "workspaces" / g["id"]
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    out = await generate(orch, store, g, {"first_frame": "cat.png"})

    assert not out.ok and "http(s)" in out.text
    assert srv.payloads == [], "nothing was sent, so nothing was paid for"


async def test_a_text_only_request_is_refused_with_the_reason(meta_env, meta):
    """Both of its models are image-to-video, so there is no text-only path to offer."""
    orch, store, g, prov = meta_env
    srv = meta()

    out = await generate(orch, store, g)

    assert not out.ok and MODEL in out.text and "reference image" in out.text
    assert srv.payloads == []


async def test_a_last_frame_is_refused_rather_than_dropped(meta_env, meta):
    """The API takes one keyframe. Silently ignoring the second half of the request would leave the
    member believing it was honoured."""
    orch, store, g, prov = meta_env
    srv = meta()

    out = await generate(orch, store, g, {"first_frame": FRAME, "last_frame": "https://example.com/b.jpg"})

    assert not out.ok and "first_frame" in out.text
    assert srv.payloads == []


async def test_an_unchosen_model_is_reported_instead_of_guessed(meta_env, meta):
    orch, store, g, prov = meta_env
    store.update_settings({"video_model": ""})
    srv = meta()

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "Video generation" in out.text
    assert srv.payloads == []


async def test_a_ratio_the_media_api_does_not_take_is_refused_with_its_own_list(meta_env, meta):
    orch, store, g, prov = meta_env
    srv = meta()

    out = await generate(orch, store, g, {"first_frame": FRAME, "aspect_ratio": "21:9"})

    assert not out.ok and "21:9" in out.text and "adaptive" in out.text
    assert srv.payloads == []


async def test_the_h3_shape_refuses_the_media_api_s_own_ratio(store, make_router, monkeypatch):
    """The other direction of the same rule, so the per-kind list is really per kind."""
    store.update_settings({"video_enabled": True})
    store.add_provider_from_preset("minimax-h3")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    real = video.generate
    sent: list[dict] = []

    async def wrapped(prov, payload, **rest):
        sent.append(payload)
        raise AssertionError("nothing should reach the H3 server for a request that is refused")

    monkeypatch.setattr(video, "generate", wrapped)

    out = await generate(orch, store, g, {"aspect_ratio": "adaptive"})

    assert not out.ok and "adaptive" in out.text and "21:9" in out.text
    assert sent == []


async def test_a_clip_over_the_cap_is_refused_instead_of_saved(meta_env, meta):
    orch, store, g, prov = meta_env
    store.update_settings({"video_max_mb": 1})
    meta(video=b"\x00" * (2 * 1024 * 1024))

    out = await generate(orch, store, g, {"first_frame": FRAME})

    assert not out.ok and "MB" in out.text
    assert not video_dir(store, g).exists()


# ------------------------------------------------------------------ the probe
async def test_the_probe_reports_a_rejected_key():
    def handler(request):
        return httpx.Response(401, json={"status": "authentication_error", "message": "Unauthorized Request"})

    prov = {"kind": "metachat_video", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert not ok and "401" in detail


async def test_the_probe_treats_a_missing_task_as_a_live_service():
    def handler(request):
        return httpx.Response(200, json={"status": "Fail", "message": "任务不存在", "data": None})

    prov = {"kind": "metachat_video", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert ok and "200" in detail


async def test_the_probe_reports_a_dead_address():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    prov = {"kind": "metachat_video", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert not ok and "mmchat" in detail


# ------------------------------------------------------------------ the tool surface
def test_the_tool_advertises_both_shapes_without_claiming_one(store):
    spec = builtin_specs()["generate_video"]
    desc = spec["description"]
    for word in ("H3", "MetaChat", "reference image", "http(s)"):
        assert word in desc, word
    # `builtin_specs()` answers in one language and drops the other half, so the Chinese wording is
    # checked where it is kept rather than through the localized view.
    assert "参考图" in toolhub.BUILTIN_SPECS["generate_video"]["description_zh"]
    # The schema is built before a provider is known, so it is the union of both lists; refusing a
    # value here that some provider accepts would be the harder failure to explain.
    assert "adaptive" in spec["parameters"]["properties"]["aspect_ratio"]["enum"]
    assert "21:9" in spec["parameters"]["properties"]["aspect_ratio"]["enum"]
    assert timeout_budget(store.get_settings(), spec) == 900
