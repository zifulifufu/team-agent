"""MetaChat's open media API (`api.mmchat.xyz/open/v1`), as both tools use it.

One key reaches two APIs, and neither is shaped like a chat model's: you submit a job, poll it, and
then download the finished file from a link the service reports. The properties are the ones
easiest to get wrong because they are *not* H3's — the body is `{prompt, model, params}`, the
parameters differ per model, and every video model needs a reference image.

The drawing half has more moving parts than the video half: fifteen models, three different submit
paths (`image/generate`, `midjourney/imagine`), and results that come back as a list or as one
picture depending on the family. So the per-model job table (`media.MEDIA_MODELS`) is asserted
directly here, not just exercised.

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

from app import discovery, imagegen, media, modelopts, net, toolhub, video
from app.main import create_app
from app.store import Store
from app.toolhub import builtin_specs, timeout_budget
from tests.conftest import FakeLLM
from tests.test_collab import setup

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512

GENERATE = "/open/v1/video/generate"
RESULT = "/open/v1/video/result/"
LINK = "https://mc-oss-cn.oss-cn-shanghai.aliyuncs.com/clip.mp4"
MODEL = "grok-imagine-video-1.5-preview"
FRAME = "https://example.com/ref.jpg"

# The drawing half: three submit paths, two result paths, and a picture link.
IMG_GENERATE = "/open/v1/image/generate"
MJ_IMAGINE = "/open/v1/midjourney/imagine"
IMG_RESULT = "/open/v1/image/result/"
MJ_RESULT = "/open/v1/midjourney/result/"
IMG_LINK = "https://mc-oss-cn.oss-cn-shanghai.aliyuncs.com/picture.png"
IMG_MODEL = "z-image-turbo"
MJ_MODEL = "mj-v82"

SUBMITS = (GENERATE, IMG_GENERATE, MJ_IMAGINE)


class FakeMeta:
    """Stands in for MetaChat's media API: records what it was asked, replays a script.

    Both endpoints answer 200 with an envelope even when they refuse — `status` is `Success` or
    `Fail`, and the reason is in `message` — which is the property our error handling has to get
    right, so the fake reproduces it rather than using status codes for everything.
    """

    def __init__(self, states=("success",), submit_status=200, submit_body=None,
                 key_expected="", fail_reason="", with_url=True, points=1775,
                 query_status="Success", query_message="", video=MP4, image=PNG,
                 image_body=None):
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
        self.image = image
        # Overrides the drawing job's `data` outright, for the shapes that are awkward to script
        # through `states` (a list of several pictures, a missing field).
        self.image_body = image_body
        self.payloads: list[dict] = []
        self.paths: list[str] = []
        # (path, Authorization header) for every request — the download assertion needs it
        self.auth: list[tuple[str, str]] = []
        self.polls = 0

    def _is_api(self, path: str) -> bool:
        return path in SUBMITS or path.startswith((RESULT, IMG_RESULT, MJ_RESULT))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        self.auth.append((path, request.headers.get("authorization", "")))
        if self._is_api(path) and self.key_expected and \
                request.headers.get("authorization") != f"Bearer {self.key_expected}":
            return httpx.Response(401, json={"status": "authentication_error", "message": "Unauthorized Request"})
        if request.method == "POST" and path in SUBMITS:
            self.payloads.append(json.loads(request.content))
            if self.submit_status >= 400:
                return httpx.Response(self.submit_status,
                                      json={"status": "authentication_error", "message": "Unauthorized Request"})
            if self.submit_body is not None:
                return httpx.Response(200, json=self.submit_body)
            return httpx.Response(200, json={"status": "Success", "message": "任务创建成功",
                                             "data": {"id": "job-1", "prompt": "x"}})
        if path.startswith(IMG_RESULT) or path.startswith(MJ_RESULT):
            return self._drawing(path)
        if path.startswith(RESULT):
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
        if path.endswith(".png"):
            return httpx.Response(200, content=self.image, headers={"Content-Type": "image/png"})
        return httpx.Response(200, content=self.video)      # the object-store link

    def _drawing(self, path: str) -> httpx.Response:
        if self.query_status != "Success":
            return httpx.Response(200, json={"status": self.query_status,
                                             "message": self.query_message, "data": None})
        i = min(self.polls, len(self.states) - 1)
        self.polls += 1
        data: dict = {"id": "job-1", "status": self.states[i], "progress": 50, "prompt": "x"}
        if self.fail_reason:
            data["fail_reason"] = self.fail_reason
        if self.image_body is not None:
            data.update(self.image_body)
        elif self.states[i] == "success":
            data["total_points"] = self.points
            if self.with_url:
                # Midjourney reports one four-up picture; the others report a list.
                data["image_url"] = IMG_LINK
                data["image_urls"] = [IMG_LINK]
        return httpx.Response(200, json={"status": "Success", "message": "图像任务查询成功", "data": data})

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

    Both tools, from one fake: the drawing and the video halves of that API answer with the same
    envelope, so a second fake would only be a second thing to keep in step. Only the transport
    underneath is replaced, exactly as in `test_video.py`: a wrong keyword or a missing argument
    still fails here.
    """
    real_video = video.generate
    real_image = imagegen.generate

    def make(**kw):
        srv = FakeMeta(**kw)

        async def video_wrapped(prov, payload, **rest):
            rest["client"] = srv.client()
            return await real_video(prov, payload, **rest)

        async def image_wrapped(prov, payload, **rest):
            rest["client"] = srv.client()
            return await real_image(prov, payload, **rest)

        monkeypatch.setattr(video, "generate", video_wrapped)
        monkeypatch.setattr(imagegen, "generate", image_wrapped)
        return srv

    return make


@pytest.fixture
def meta_env(store, make_router):
    """A group whose video provider is MetaChat's media API, with a model already chosen."""
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 10,
                           "video_max_mb": 8, "video_model": MODEL})
    prov = store.add_provider_from_preset("metachat-media", api_key="sk-mc-123456")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g, prov


@pytest.fixture
def draw_env(store, make_router):
    """The same provider, this time with drawing switched on and a model chosen."""
    store.update_settings({"image_enabled": True, "image_timeout": 30, "image_max_mb": 2,
                           "image_model": IMG_MODEL, "image_size": "1024x1024"})
    prov = store.add_provider_from_preset("metachat-media", api_key="sk-mc-123456")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g, prov


async def generate(orch, store, g, args=None, approve=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    return await orch.toolhub.call(ctx, "generate_video",
                                   {"prompt": "a cat asleep on a warm laptop", **(args or {})}, approve)


async def draw(orch, store, g, args=None, approve=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    return await orch.toolhub.call(ctx, "generate_image",
                                   {"prompt": "a cat asleep on a warm laptop", **(args or {})}, approve)


def video_dir(store, g):
    return Path(store.data_dir) / "workspaces" / g["id"] / "video"


def image_dir(store, g):
    return Path(store.data_dir) / "workspaces" / g["id"] / "image"


# ------------------------------------------------------------------ what the preset ships
async def test_the_media_api_has_no_list_to_fetch_so_the_ids_ship_with_the_app():
    """`/open/v1/models`, `/open/v1/video/models` and `/open/v1/image/models` all answer 404, so
    there is nothing to ask for.

    Spelled as an assertion about the *network*: a refresh of this kind must not make a request at
    all, because the honest thing to do here is answer from the table rather than invent an endpoint.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request should have been made, got {request.url}")

    prov = {"kind": "metachat_media", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await discovery.fetch_models(prov, client=c)

    shipped = media.BUILTIN_MEDIA_MODELS["metachat_media"]
    assert [e["id"] for e in got] == sorted([*shipped["image"], *shipped["video"]])
    # Each one carries the mode its own row in the job table declares, rather than being left to a
    # name guess: "mj-v82" and "z-image-turbo" are not words any rule could recognise.
    assert {e["id"]: e["mode"] for e in got}[MJ_MODEL] == "image"
    assert {e["id"]: e["mode"] for e in got}[MODEL] == "video"


def test_a_provider_from_before_the_rename_keeps_its_id_key_and_models(tmp_path):
    """`metachat_video` became `metachat_media` when its one key turned out to reach an image API too.

    The upgrade rewrites only the kind. The id is opaque and settings and model rows point at it, so
    renaming the provider itself would break every reference to it — and the failure would look like
    "the provider I configured is gone" rather than a rename.
    """
    first = Store(tmp_path / "data")
    prov = first.add_provider_from_preset("metachat-media", api_key="sk-mc-123456")
    models = sorted(m["model_name"] for m in first.list_provider_models(prov["id"]))
    first._x("UPDATE providers SET kind='metachat_video' WHERE id=?", (prov["id"],))

    upgraded = Store(tmp_path / "data")            # this is where the rewrite happens

    after = upgraded.get_provider(prov["id"])
    assert after["kind"] == "metachat_media"
    assert after["api_key"], "the key must survive the rewrite"
    assert sorted(m["model_name"] for m in upgraded.list_provider_models(prov["id"])) == models
    # Reachable by both tools again, which is the whole point: before the rewrite the image tool
    # could not see it however the user had configured it.
    assert prov["id"] in [p["id"] for p in upgraded.providers_for_use("image", imagegen.KINDS)]


def test_the_preset_seeds_every_shipped_model(store):
    """Both media, one provider: the same key draws and renders, so a second entry for the same
    credential would be a second thing to keep in step."""
    prov = store.add_provider_from_preset("metachat-media")
    assert prov["kind"] == "metachat_media"
    shipped = media.BUILTIN_MEDIA_MODELS["metachat_media"]
    assert sorted(m["model_name"] for m in store.list_provider_models(prov["id"])) == \
        sorted([*shipped["image"], *shipped["video"]])


def test_a_refresh_makes_them_selectable_in_the_video_settings(store):
    """The end the user actually cares about: press refresh, then the setting can name them."""
    prov = store.add_provider_from_preset("metachat-media")
    asyncio.run(modelopts.refresh_live(store, prov["id"]))
    shipped = media.BUILTIN_MEDIA_MODELS["metachat_media"]

    assert [m["id"] for m in modelopts.models_for_use(store, prov["id"], "video")] == \
        sorted(shipped["video"])
    assert [m["id"] for m in modelopts.models_for_use(store, prov["id"], "image")] == \
        sorted(shipped["image"])
    # and the provider is offered to both tools, without being mistaken for something a member
    # can chat with
    assert prov["id"] in [p["id"] for p in store.providers_for_use("video", video.KINDS)]
    assert prov["id"] in [p["id"] for p in store.providers_for_use("image", imagegen.KINDS)]
    assert [m["id"] for m in store.list_models() if m["provider_id"] == prov["id"]] == []


def test_the_media_options_endpoint_offers_it_after_a_refresh(tmp_path):
    client = TestClient(create_app(tmp_path / "app" / "data", completion_fn=None, background=False),
                        base_url="http://127.0.0.1")
    pid = client.post("/api/providers", json={"preset": "metachat-media", "api_key": "sk-not-real"}).json()["id"]
    assert client.post(f"/api/providers/{pid}/model-options/refresh").status_code == 200
    shipped = media.BUILTIN_MEDIA_MODELS["metachat_media"]

    for use in ("video", "image"):
        body = client.get(f"/api/media/options?use={use}").json()
        assert [p["id"] for p in body["providers"]] == [pid]
        assert [m["id"] for m in body["providers"][0]["models"]] == sorted(shipped[use])
    # The two lists do not bleed into each other: neither tool is offered a model of the other
    # medium, which would be a request that fails after it has been paid for.
    video_ids = {m["id"] for m in client.get("/api/media/options?use=video").json()
                 ["providers"][0]["models"]}
    assert MJ_MODEL not in video_ids and IMG_MODEL not in video_ids
    assert MODEL in video_ids


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
    assert video.clamp_seconds(3, 15, "metachat_media") == (3, False)
    assert video.clamp_seconds(60, 10, "metachat_media") == (10, True)
    assert "adaptive" in video.shape_of("metachat_media")["ratios"]
    assert "adaptive" not in video.shape_of("minimax_video")["ratios"]
    assert "21:9" in video.shape_of("minimax_video")["ratios"]
    assert "21:9" not in video.shape_of("metachat_media")["ratios"]


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


# ------------------------------------------------------------------ drawing through the same key
def test_each_model_is_asked_on_its_own_path_with_its_own_parameters(store):
    """The reason `media.MEDIA_MODELS` is a table rather than one shape for the whole prefix.

    Two of the fifteen take a parameter the others do not, and Midjourney has its own path for both
    verbs. Sending `num` to a model whose documentation does not list it is a paid failure, so the
    assertion is deliberately about which keys are *absent*.
    """
    assert media.job_of(IMG_MODEL, "image")["submit"] == "image/generate"
    assert media.job_of(MJ_MODEL, "image")["submit"] == "midjourney/imagine"
    assert media.job_of(MJ_MODEL, "image")["result"] == "midjourney/result/{vid}"
    assert media.job_of(MJ_MODEL, "image")["file"] == "image_url", "Midjourney returns one four-up picture"
    assert media.job_of(IMG_MODEL, "image")["file"] == "image_urls"

    plain = imagegen.metachat_payload("画一只猫", model=IMG_MODEL, size="1536x1024")
    assert plain == {"prompt": "画一只猫", "model": IMG_MODEL, "params": {"aspect": "3:2"}}, \
        "no `num`: that model's page does not list it"
    # Seedream's page lists the count and not the ratio, so the ratio is left alone — a parameter a
    # model does not know comes back as a failure the user has paid for.
    seedream = imagegen.metachat_payload("画一只猫", model="doubao-seedream-4-5-251128", size="1024x1024")
    assert seedream["params"] == {"num": 1}
    assert (seedream["model"], seedream["prompt"]) == ("doubao-seedream-4-5-251128", "画一只猫")


def test_a_model_name_nobody_has_still_gets_the_generic_job(store):
    """The model name is a free-text setting, so a newer one must not need a release of this app.

    What it gets is the generic path with no parameters at all — the only thing that can be sent
    without knowing what the model accepts.
    """
    job = media.job_of("some-model-from-next-year", "image")
    assert job["submit"] == "image/generate" and job["result"] == "image/result/{vid}"
    assert job["params"] == ()
    assert imagegen.metachat_payload("x", model="some-model-from-next-year", size="1024x1024")["params"] == {}
    # …and pointing the drawing tool at a video model is not silently answered with image paths
    assert media.job_of(MODEL, "image")["submit"] == "image/generate"
    assert media.job_of(IMG_MODEL, "video")["submit"] == "video/generate"


def test_a_clip_link_and_a_picture_link_are_read_from_their_own_fields():
    """`video_url` is a string, `image_urls` a list, `image_url` a string again."""
    assert media.job_links({"video_url": LINK}, "video_url") == [LINK]
    assert media.job_links({"image_urls": [IMG_LINK, "https://x/b.png"]}, "image_urls") == \
        [IMG_LINK, "https://x/b.png"]
    assert media.job_links({"image_url": IMG_LINK}, "image_url") == [IMG_LINK]
    for empty in ({}, {"image_urls": []}, {"image_url": None}, None, "nonsense"):
        assert media.job_links(empty, "image_urls") == []


async def test_a_drawing_lands_in_the_group_workspace(draw_env, meta):
    orch, store, g, prov = draw_env
    srv = meta()
    other = store.create_group("Another project")

    out = await draw(orch, store, g)

    assert out.ok, out.text
    files = list(image_dir(store, g).glob("*.png"))
    assert len(files) == 1 and files[0].read_bytes() == PNG
    # Asked on the drawing path for the chosen model, with that model's parameters only
    assert srv.payloads[0]["model"] == IMG_MODEL
    assert srv.payloads[0]["params"] == {"aspect": "1:1"}
    assert IMG_GENERATE in srv.paths, srv.paths
    assert not image_dir(store, other).exists()
    # The member cannot see the picture, and is told so rather than left to describe it.
    assert "cannot see the result" in out.text


async def test_a_midjourney_drawing_uses_its_own_path_and_its_single_picture(draw_env, meta):
    """The family whose result is one URL rather than a list — a different read of the same
    envelope, which is exactly the thing a single shared "read the URL" would get wrong."""
    orch, store, g, prov = draw_env
    store.update_settings({"image_model": MJ_MODEL})
    srv = meta()

    out = await draw(orch, store, g)

    assert out.ok, out.text
    assert MJ_IMAGINE in srv.paths and not any(p.startswith(IMG_GENERATE) for p in srv.paths)
    assert any(p.startswith(MJ_RESULT) for p in srv.paths)
    assert next(iter(image_dir(store, g).glob("*.png"))).read_bytes() == PNG


async def test_the_key_never_goes_to_the_object_store_for_a_picture_either(draw_env, meta):
    orch, store, g, prov = draw_env
    srv = meta(key_expected="sk-mc-123456")

    out = await draw(orch, store, g)

    assert out.ok, out.text
    api = [h for path, h in srv.auth if path in SUBMITS or path.startswith((IMG_RESULT, MJ_RESULT))]
    assert api and all(h == "Bearer sk-mc-123456" for h in api)
    assert [h for path, h in srv.auth if path.endswith("/picture.png")] == [""]


async def test_the_drawing_cap_is_what_refuses_an_oversized_picture(draw_env, meta):
    orch, store, g, prov = draw_env
    store.update_settings({"image_max_mb": 1})
    meta(image=b"\x00" * (2 * 1024 * 1024), image_body={"image_urls": [IMG_LINK]})

    out = await draw(orch, store, g)

    assert not out.ok and "MB" in out.text
    assert not image_dir(store, g).exists(), "nothing is written for a picture that was refused"


async def test_a_finished_drawing_with_no_picture_is_refused(draw_env, meta):
    orch, store, g, prov = draw_env
    meta(image_body={"total_points": 10})          # success, but no link of any kind

    out = await draw(orch, store, g)

    assert not out.ok and "picture address" in out.text
    assert not image_dir(store, g).exists()


async def test_an_unchosen_drawing_model_is_reported_instead_of_guessed(draw_env, meta):
    """Fifteen models behind one key: picking one at random would spend the user's points on a
    medium or a look they did not ask for."""
    orch, store, g, prov = draw_env
    store.update_settings({"image_model": ""})
    srv = meta()

    out = await draw(orch, store, g)

    assert not out.ok and "MetaChat" in out.text and "Image generation" in out.text
    assert srv.payloads == []


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
def test_the_proxy_policy_follows_the_destination_not_the_generator():
    """An H3 server and MetaChat's media API live in different places, so the same rule — the one
    `net.client` applies app-wide — has to give them different answers.

    Direct for this machine or the LAN, because a system proxy quietly sends that traffic somewhere
    else (that is how a healthy local server reports 502). Proxy allowed for MetaChat, because
    forcing *that* one direct breaks the corporate networks that need it. Getting it wrong shows up
    as two "test the service" buttons on one host disagreeing, and as one medium working while the
    other times out — so the policy is asserted, not just the intent.
    """
    assert net.is_local_url("http://127.0.0.1:30010")
    assert net.is_local_url("http://gpu-box.local:8000")
    assert not net.is_local_url("https://api.mmchat.xyz/open/v1")
    for local in ("http://127.0.0.1:30010", "http://192.168.1.212:30010", "http://gpu-box.local:8000"):
        assert net.client(local)._trust_env is False, local
    assert net.client("https://api.mmchat.xyz/open/v1")._trust_env is True


async def test_the_probe_reports_a_rejected_key():
    def handler(request):
        return httpx.Response(401, json={"status": "authentication_error", "message": "Unauthorized Request"})

    prov = {"kind": "metachat_media", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert not ok and "401" in detail


async def test_the_probe_treats_a_missing_task_as_a_live_service():
    def handler(request):
        return httpx.Response(200, json={"status": "Fail", "message": "任务不存在", "data": None})

    prov = {"kind": "metachat_media", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        ok, detail = await video.probe(prov, client=c)
    assert ok and "200" in detail


async def test_the_probe_reports_a_dead_address():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    prov = {"kind": "metachat_media", "base_url": "https://api.mmchat.xyz/open/v1", "api_key": "sk-x"}
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
