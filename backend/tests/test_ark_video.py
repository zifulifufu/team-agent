"""Video generation through Volcengine's Ark (Doubao Seedance).

The third job shape this app speaks, and the one whose request is structurally different from the
other two: the prompt is one item in a `content` array, every reference is another item carrying a
`role`, and which role you use decides the *task type* the model runs — which in turn constrains
the parameters. That is the part easiest to get wrong, so the body is asserted field by field
rather than through the tool's prose.

The service is faked with an httpx transport, so what is under test is our half of the contract —
the body, the ratios, the polling, where the file lands, and every refusal that happens before a
job is paid for. Nothing here talks to Ark: a real generation costs money.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import discovery, media, video
from app.main import create_app
from app import toolhub
from app.toolhub import builtin_specs, timeout_budget
from tests.conftest import FakeLLM
from tests.test_collab import setup

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048

SUBMIT = "/api/v3/contents/generations/tasks"
TASK = "/api/v3/contents/generations/tasks/"
LINK = "https://ark-content-generation-cn-beijing.tos-cn-beijing.volces.com/out.mp4"
MODEL = "doubao-seedance-2-5-260628"


class FakeArk:
    """Stands in for Ark: records what it was asked, replays a script.

    Two of its habits are reproduced rather than simplified, because our error handling exists
    because of them: the task object nests the finished clip under `content`, and a refusal arrives
    as HTTP 401 with `{"error": {"code": "AuthenticationError", ...}}` — measured against the live
    service, which is also how the probe's rule was arrived at.
    """

    def __init__(self, statuses=("succeeded",), submit_status=200, submit_body=None,
                 with_url=True, fail_message="", usage=None, video=MP4, key_expected=""):
        self.statuses = list(statuses)
        self.submit_status = submit_status
        self.submit_body = submit_body
        self.with_url = with_url
        self.fail_message = fail_message
        self.usage = usage
        self.video = video
        self.key_expected = key_expected
        self.payloads: list[dict] = []
        self.paths: list[str] = []
        self.auth: list[tuple[str, str]] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.auth.append((request.url.path, request.headers.get("authorization", "")))
        if (self.key_expected and request.url.path.startswith("/api/v3")
                and request.headers.get("authorization") != f"Bearer {self.key_expected}"):
            return httpx.Response(401, json={"error": {
                "code": "AuthenticationError", "message": "The API key format is incorrect.",
                "param": "", "type": "Unauthorized"}})
        if request.method == "POST" and request.url.path == SUBMIT:
            self.payloads.append(json.loads(request.content))
            if self.submit_status >= 400:
                return httpx.Response(self.submit_status, json={"error": {
                    "code": "InvalidParameter", "message": "the model does not exist"}})
            if self.submit_body is not None:
                return httpx.Response(200, json=self.submit_body)
            return httpx.Response(200, json={"id": "cgt-1"})
        if request.url.path.startswith(TASK):
            i = min(self.polls, len(self.statuses) - 1)
            self.polls += 1
            body: dict = {"id": "cgt-1", "model": MODEL, "status": self.statuses[i]}
            if self.statuses[i] == "succeeded":
                body["content"] = {"video_url": LINK} if self.with_url else {}
                body["usage"] = self.usage or {"completion_tokens": 108900, "total_tokens": 108900}
            if self.statuses[i] == "failed" and self.fail_message:
                body["error"] = {"code": "InternalError", "message": self.fail_message}
            return httpx.Response(200, json=body)
        return httpx.Response(200, content=self.video)      # the object-storage link

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Production polls every few seconds; the loop is what is under test, not the waiting."""
    monkeypatch.setattr(video, "POLL_START", 0.01)
    monkeypatch.setattr(video, "POLL_MAX", 0.02)


@pytest.fixture
def ark(monkeypatch):
    """`srv = ark(**kwargs)` builds the service and points the tool at it."""
    real = video.generate

    def make(**kw):
        srv = FakeArk(**kw)

        async def wrapped(prov, payload, **rest):
            rest["client"] = srv.client()
            return await real(prov, payload, **rest)

        monkeypatch.setattr(video, "generate", wrapped)
        return srv

    return make


@pytest.fixture
def ark_env(store, make_router):
    """A group whose video provider is Ark, with the shipped model already chosen."""
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 30,
                           "video_max_mb": 8, "video_model": MODEL})
    prov = store.add_provider_from_preset("doubao-seedance", api_key="sk-ark-123456")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g, prov


async def generate(orch, store, g, args=None, approve=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    return await orch.toolhub.call(ctx, "generate_video",
                                   {"prompt": "一杯苹果果茶广告", **(args or {})}, approve)


def video_dir(store, g):
    return Path(store.data_dir) / "workspaces" / g["id"] / "video"


def body_of(payload: dict, index: int) -> dict:
    return payload["content"][index]


# ------------------------------------------------------------------ the request body
def test_the_body_is_the_content_array_ark_documents():
    """Text first, then references in the order the prompt's `@图片1` counting expects."""
    p = video.ark_payload(
        "首帧为图片参考", model=MODEL, ratio="16:9", duration_seconds=11, short_edge=768,
        reference_images=("https://a/1.jpg", "https://a/2.jpg"),
        reference_videos=("https://a/v.mp4",), reference_audios=("https://a/a.mp3",),
    )
    assert p["model"] == MODEL
    assert p["ratio"] == "16:9" and p["duration"] == 11 and p["resolution"] == "720p"
    assert p["watermark"] is False, "the user's own example asks for no watermark"
    assert body_of(p, 0) == {"type": "text", "text": "首帧为图片参考"}
    assert body_of(p, 1) == {"type": "image_url", "image_url": {"url": "https://a/1.jpg"},
                             "role": "reference_image"}
    assert body_of(p, 2)["role"] == "reference_image"
    assert body_of(p, 3) == {"type": "video_url", "video_url": {"url": "https://a/v.mp4"},
                             "role": "reference_video"}
    assert body_of(p, 4) == {"type": "audio_url", "audio_url": {"url": "https://a/a.mp3"},
                             "role": "reference_audio"}
    assert len(p["content"]) == 5 and "generate_audio" not in p, \
        "an omitted generate_audio leaves Ark's own default (on) in place, which is also what a " \
        "model name this app ships no description for will accept"


def test_a_keyframe_is_its_own_role_and_only_it_pins_the_ratio():
    p = video.ark_payload("x", model=MODEL, ratio="16:9", duration_seconds=5, short_edge=512,
                          first_frame="data:image/png;base64,AAAA", last_frame="https://a/b.png")
    assert body_of(p, 1)["role"] == "first_frame" and body_of(p, 2)["role"] == "last_frame"
    assert p["resolution"] == "480p", "the short-edge setting maps onto Ark's named resolutions"
    assert video.ark_ratio("16:9", keyframe=True) == ("adaptive", True)
    assert video.ark_ratio("adaptive", keyframe=True) == ("adaptive", False)
    assert video.ark_ratio("16:9", keyframe=False) == ("16:9", False)
    assert video.ark_ratio("9:16", keyframe=False) == ("9:16", False)


def test_the_two_names_that_are_not_urls_are_passed_through_untouched():
    """Ark also takes its own asset ids and raw data URLs; mangling either would break a caller
    that knows more about this API than we do."""
    ws = Path("/tmp")
    assert video.ark_asset("asset://abc-123", ws, what="x", what_zh="x") == "asset://abc-123"
    assert video.ark_asset("data:image/png;base64,AA", ws, what="x", what_zh="x") == "data:image/png;base64,AA"


def test_a_workspace_file_is_inlined_because_a_group_has_nothing_public(tmp_path):
    """The picture a group has is a file in its own folder; base64 is the only way to give it to a
    service that fetches URLs, and Ark documents that form."""
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    got = video.ark_asset("shot.png", tmp_path, what="reference picture", what_zh="参考图")
    assert got.startswith("data:image/png;base64,")
    assert base64.b64decode(got.split(",", 1)[1]).startswith(b"\x89PNG")


def test_a_file_too_big_to_inline_is_refused_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ARK_INLINE_MB", 0)
    (tmp_path / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(video.VideoError) as e:
        video.ark_asset("big.png", tmp_path, what="reference clip", what_zh="参考视频")
    assert "64 MB" in str(e.value), "the reason has to name the limit that matters"


def test_a_file_outside_the_workspace_is_refused(tmp_path):
    """These bytes leave this machine, so a member must not be able to post arbitrary local files.

    Tested through the `file://` form, which is the one that resolves to a real path on this
    machine: a bare `/x.png` is read from the workspace root instead (the rule `frame_uri` also
    uses), so it can never reach outside — it just fails to find anything.
    """
    root = tmp_path / "group"
    root.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(video.VideoError) as e:
        video.ark_asset(outside.as_uri(), root, what="reference picture", what_zh="参考图")
    assert "outside this group's workspace" in str(e.value)
    # And the by-the-letter form lands on the workspace-root rule rather than on somebody's file.
    with pytest.raises(video.VideoError) as e2:
        video.ark_asset(str(outside), root, what="reference picture", what_zh="参考图")
    assert "workspace root" in str(e2.value)


# ------------------------------------------------------------------ the happy path
async def test_a_rendered_clip_lands_in_the_group_workspace(ark_env, ark):
    orch, store, g, prov = ark_env
    srv = ark()

    out = await generate(orch, store, g, {"duration_seconds": 11})

    assert out.ok and "Seedance" in out.text, out.text
    files = list(video_dir(store, g).glob("*.mp4"))
    assert len(files) == 1 and files[0].stat().st_size == len(MP4)
    assert TASK + "cgt-1" in srv.paths, "the poll is on the task object, not a separate status path"
    assert SUBMIT in srv.paths and srv.paths[-1] == "/out.mp4", srv.paths


async def test_the_job_is_polled_through_every_state_until_it_is_done(ark_env, ark):
    orch, store, g, prov = ark_env
    srv = ark(statuses=("queued", "running", "succeeded"))

    out = await generate(orch, store, g)

    assert out.ok and srv.polls >= 3


async def test_the_clip_is_fetched_without_the_key(ark_env, ark):
    """The link points at Ark's own object storage. Sending the key there would hand the
    credential to a third party for a file that does not need it."""
    orch, store, g, prov = ark_env
    srv = ark()

    await generate(orch, store, g)

    download = [h for path, h in srv.auth if path == "/out.mp4"]
    assert download == [""], srv.auth
    assert srv.payloads[0]["model"] == MODEL, "and the key was sent to Ark itself"
    assert all(h.startswith("Bearer ") for path, h in srv.auth if path.startswith("/api/v3"))


async def test_what_the_job_cost_is_reported(ark_env, ark):
    """Ark bills in tokens and says how many; hiding it would be the wrong call."""
    orch, store, g, prov = ark_env
    ark(usage={"completion_tokens": 4242, "total_tokens": 4242})

    out = await generate(orch, store, g)

    assert out.ok and "4242" in out.text


async def test_the_provider_that_takes_references_is_the_only_one_asked_for_them(store, make_router):
    """Sending `reference_videos` to H3 would have it silently ignored, and the clip that came back
    would look like the model's fault."""
    store.update_settings({"video_enabled": True, "video_model": ""})
    store.add_provider_from_preset("minimax-h3")
    orch, g = setup(store, make_router, FakeLLM(default="x"))

    out = await generate(orch, store, g, {"reference_videos": ["https://a/v.mp4"]})

    assert not out.ok and "does not take reference pictures" in out.text


async def test_a_prompt_that_names_a_file_that_is_not_there_says_so(ark_env, ark):
    orch, store, g, prov = ark_env
    srv = ark()

    out = await generate(orch, store, g, {"first_frame": "image/missing.png"})

    assert not out.ok and "image/missing.png" in out.text
    assert "workspace root" in out.text, "the path rule has to be stated, not implied"
    assert srv.payloads == []


async def test_a_message_that_says_the_ratio_had_to_change(ark_env, ark):
    """A keyframe pins the output shape, so the request is adjusted rather than refused — and the
    caller is told, exactly as it is for a clamped duration."""
    orch, store, g, prov = ark_env
    srv = ark()
    (Path(store.workspace_dir(g["id"])) / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    out = await generate(orch, store, g, {"first_frame": "shot.png", "aspect_ratio": "16:9"})

    assert out.ok and "adaptive" in out.text
    assert srv.payloads[0]["ratio"] == "adaptive"
    assert srv.payloads[0]["content"][1]["role"] == "first_frame"


# ------------------------------------------------------------------ failures
async def test_a_job_that_fails_reports_the_reason_ark_gave(ark_env, ark):
    orch, store, g, prov = ark_env
    ark(statuses=("running", "failed"), fail_message="the reference image could not be fetched")

    out = await generate(orch, store, g)

    assert not out.ok and "could not be fetched" in out.text
    assert not video_dir(store, g).exists()


async def test_a_finished_job_with_no_link_is_refused_rather_than_saved(ark_env, ark):
    orch, store, g, prov = ark_env
    ark(with_url=False)

    out = await generate(orch, store, g)

    assert not out.ok and "no video address" in out.text
    assert not video_dir(store, g).exists()


async def test_a_refused_submit_says_what_ark_said(ark_env, ark):
    orch, store, g, prov = ark_env
    ark(submit_status=400)

    out = await generate(orch, store, g)

    assert not out.ok and "the model does not exist" in out.text


async def test_a_rejected_key_is_reported_without_echoing_it(ark_env, ark):
    """Measured against the live service: an Ark key it will not accept answers 401 with
    AuthenticationError, and the useful part of that is the name of the service it came from."""
    orch, store, g, prov = ark_env
    ark(submit_status=401)

    out = await generate(orch, store, g)

    assert not out.ok and "Ark" in out.text and "does not exist" in out.text
    assert "sk-ark-123456" not in out.text, "the refusal must not echo the key into the transcript"


# ------------------------------------------------------------------ what the preset ships
async def test_the_model_ships_with_the_app_because_ark_publishes_no_listing():
    """A refresh must not make a request at all: the honest answer is the shipped table, not an
    endpoint that does not exist."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request should have been made, got {request.url}")

    prov = {"kind": "ark_video", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await discovery.fetch_models(prov, client=c)

    assert [e["id"] for e in got] == sorted(media.BUILTIN_MEDIA_MODELS["ark_video"]["video"])
    assert {e["mode"] for e in got} == {"video"}


def test_the_preset_seeds_the_model(store):
    prov = store.add_provider_from_preset("doubao-seedance")
    assert prov["kind"] == "ark_video" and prov["base_url"].endswith("/api/v3")
    assert [m["model_name"] for m in store.list_provider_models(prov["id"])] == [MODEL]
    assert [m["use"] for m in store.list_provider_models(prov["id"])] == ["video"]


def test_a_refresh_makes_it_selectable_in_the_video_settings(store):
    prov = store.add_provider_from_preset("doubao-seedance")
    asyncio.run(__import__("app.modelopts", fromlist=["x"]).refresh_live(store, prov["id"]))

    assert prov["id"] in [p["id"] for p in store.providers_for_use("video", video.KINDS)]
    assert [m["id"] for m in store.list_models() if m["provider_id"] == prov["id"]] == [], \
        "and it is still not something a member can be pointed at as a chat model"


def test_the_media_options_endpoint_offers_it_after_a_refresh(tmp_path):
    client = TestClient(create_app(tmp_path / "app" / "data", completion_fn=None, background=False),
                        base_url="http://127.0.0.1")
    pid = client.post("/api/providers", json={"preset": "doubao-seedance", "api_key": "sk-not-real"}).json()["id"]
    assert client.post(f"/api/providers/{pid}/model-options/refresh").status_code == 200

    body = client.get("/api/media/options?use=video").json()
    assert [p["id"] for p in body["providers"]] == [pid]
    assert [m["id"] for m in body["providers"][0]["models"]] == [MODEL]
    assert client.get("/api/media/options?use=image").json()["providers"] == []


def test_the_tool_advertises_the_three_shapes(store):
    spec = builtin_specs()["generate_video"]
    desc = spec["description"]
    for word in ("H3", "MetaChat", "Ark", "Seedance", "reference_videos", "reference_audios"):
        assert word in desc, word
    # `builtin_specs()` answers in one language and drops the other half, so the Chinese wording is
    # checked where it is kept rather than through the localized view.
    assert "参考素材" in toolhub.BUILTIN_SPECS["generate_video"]["description_zh"]
    props = spec["parameters"]["properties"]
    for name in ("reference_images", "reference_videos", "reference_audios", "generate_audio"):
        assert props[name]["type"] in ("array", "boolean"), name
    # The schema is built before a provider is known, so it is the union of all three lists.
    assert "adaptive" in props["aspect_ratio"]["enum"] and "21:9" in props["aspect_ratio"]["enum"]
    assert timeout_budget(store.get_settings(), spec) == 900


# ------------------------------------------------------------------ the probe
async def test_the_probe_reports_a_rejected_key():
    """Measured against the live service: an Ark key it will not accept comes back 401 with
    AuthenticationError — so 401/403 is the only answer that means something is wrong."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "AuthenticationError",
                                                   "message": "The API key format is incorrect."}})

    prov = {"kind": "ark_video", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "sk-nope"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)

    assert not ok and "rejected the key" in detail


async def test_a_task_that_does_not_exist_is_not_a_broken_service():
    """A 404 for a made-up id proves the request was authenticated and processed, which is all this
    probe claims — reading `4xx` as "broken" is the mistake that made a working key look like a
    dead service on the other provider."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "ResourceNotFound",
                                                   "message": "The task does not exist."}})

    prov = {"kind": "ark_video", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "sk-ok"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)

    assert ok and "accepted the key" in detail and "404" in detail
