"""Image generation through an OpenAI-compatible `/images/generations` endpoint.

The service is faked with an httpx transport, so what is under test is our half of the
contract: the body we send, both response shapes (inline base64 and a temporary URL), where
the file lands, and every refusal that should happen *before* somebody is billed for a
generation.

The kind isolation at the end deserves its own note: video and image providers are both
"media", and picking one by the union of kinds would let the video tool run against an image
endpoint. That failure looks like a broken server, so it is asserted directly.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from app import imagegen, media, video
from app.toolhub import builtin_specs, timeout_budget
from tests.conftest import FakeLLM
from tests.test_collab import setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
B64 = base64.b64encode(PNG).decode()


class FakeServer:
    """Stands in for an images API: records what it was asked, replays a script."""

    def __init__(self, *, body: dict | None = None, status: int = 200, image: bytes = PNG,
                 link_broken: bool = False, key_expected: str = ""):
        self.body = body
        self.status = status
        self.image = image
        self.link_broken = link_broken
        self.key_expected = key_expected
        self.payloads: list[dict] = []
        self.paths: list[str] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.headers.append(dict(request.headers))
        if self.key_expected and request.headers.get("authorization") != f"Bearer {self.key_expected}":
            return httpx.Response(401, json={"error": {"message": "bad key", "code": "invalid_api_key"}})
        if request.url.path.endswith("/images/generations"):
            self.payloads.append(json.loads(request.content))
            if self.status >= 300:
                return httpx.Response(self.status, json=self.body or {"error": {"message": "no"}})
            return httpx.Response(200, json=self.body if self.body is not None else {"data": [{"b64_json": B64}]})
        if self.status >= 300 and not request.url.path.endswith("/image-out.png"):
            return httpx.Response(self.status, json=self.body or {"error": {"message": "no"}})
        if request.url.path.endswith("/image-out.png"):        # the URL branch downloads this
            if self.link_broken:
                return httpx.Response(403, text="link expired")
            return httpx.Response(200, content=self.image, headers={"Content-Type": "image/png"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-image-1"}, {"id": "gpt-image-1.5"}]})
        return httpx.Response(404, json={"error": {"message": "no such path"}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake(monkeypatch):
    """`srv = fake(**kwargs)` builds a server and points the module at it."""
    real = imagegen.generate

    def make(**kw):
        srv = FakeServer(**kw)

        async def wrapped(prov, payload, **rest):
            # Only when the caller did not bring one: a test that passes its own transport is
            # testing that transport, and silently swapping it would hide what it asserts on.
            rest.setdefault("client", srv.client())
            return await real(prov, payload, **rest)

        monkeypatch.setattr(imagegen, "generate", wrapped)
        return srv

    return make


@pytest.fixture
def env(store, make_router, fake):
    """A group whose host may draw, with an OpenAI-compatible image provider added.

    The transport is replaced here rather than per test: a test that forgot would reach the
    real network through whatever proxy the machine has, and report a 502 in place of its
    actual subject.
    """
    srv = fake()
    store.update_settings({"image_enabled": True, "image_timeout": 30, "image_max_mb": 1,
                           "image_model": "gpt-image-1", "image_size": "1024x1024"})
    prov = store.add_provider_from_preset("openai-image")
    store.update_provider(prov["id"], {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test-1234567890"})
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g, store.get_provider(prov["id"]), srv


async def draw(orch, store, g, args=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    return await orch.toolhub.call(ctx, "generate_image", {"prompt": "a cat asleep on a laptop", **(args or {})})


def image_dir(store, g):
    return Path(store.data_dir) / "workspaces" / g["id"] / "image"


# ------------------------------------------------------------- what we send
def test_the_request_body_omits_response_format_and_sends_one_image():
    """`response_format` is what OpenAI's newer image models reject outright; a service that
    wants it says so by returning a URL, and both shapes are accepted on the way back."""
    p = imagegen.build_payload("a cat", model="gpt-image-1.5", size="1536x1024")
    assert p == {"model": "gpt-image-1.5", "prompt": "a cat", "n": 1, "size": "1536x1024"}
    assert "response_format" not in p


def test_the_base_url_tolerates_a_trailing_slash_or_a_trailing_v1():
    assert imagegen._api("https://x/v1", "/v1/images/generations") == "https://x/v1/images/generations"
    assert imagegen._api("https://x/v1/", "v1/images/generations") == "https://x/v1/images/generations"
    assert imagegen._api("https://x", "/v1/images/generations") == "https://x/v1/images/generations"


# ------------------------------------------------------------ response shapes
def test_an_inline_base64_image_is_decoded():
    blob, url = imagegen.first_image({"data": [{"b64_json": B64}]})
    assert blob == PNG and url == ""


def test_a_data_url_is_accepted_as_well():
    blob, _ = imagegen.first_image({"data": [{"b64_json": "data:image/png;base64," + B64}]})
    assert blob == PNG


def test_a_temporary_url_is_handed_back_for_downloading():
    blob, url = imagegen.first_image({"data": [{"url": "https://x/image-out.png"}]})
    assert blob is None and url == "https://x/image-out.png"


def test_an_error_object_under_http_200_still_fails():
    """Some gateways answer 200 with an error body; treating that as success would save an
    empty file and report a picture."""
    with pytest.raises(imagegen.ImageError) as e:
        imagegen.first_image({"error": {"message": "content policy"}})
    assert "content policy" in str(e.value)
    with pytest.raises(imagegen.ImageError):
        imagegen.first_image({"data": []})
    with pytest.raises(imagegen.ImageError):
        imagegen.first_image({"data": [{}]})


# --------------------------------------------------------------- the request
async def test_a_base64_reply_becomes_bytes(env):
    orch, store, g, prov, srv = env
    fake = FakeServer()
    got = await imagegen.generate(prov, imagegen.build_payload("a cat", model="gpt-image-1", size="1024x1024"),
                                 max_bytes=1024 * 1024, deadline_s=10, client=fake.client())
    assert got["data"] == PNG
    assert fake.payloads[0]["prompt"] == "a cat"


async def test_a_url_reply_is_downloaded_immediately(env):
    """Those links expire within minutes, so they are fetched during the call rather than when
    the UI asks for the file."""
    orch, store, g, prov, srv = env
    fake = FakeServer(body={"data": [{"url": "http://127.0.0.1:9/image-out.png"}]})
    got = await imagegen.generate(prov, imagegen.build_payload("a cat", model="m", size="1024x1024"),
                                 max_bytes=1024 * 1024, deadline_s=10, client=fake.client())
    assert got["data"] == PNG
    assert any(p.endswith("/image-out.png") for p in fake.paths)


async def test_an_expired_link_is_reported(env):
    orch, store, g, prov, srv = env
    fake = FakeServer(body={"data": [{"url": "http://127.0.0.1:9/image-out.png"}]}, link_broken=True)
    with pytest.raises(imagegen.ImageError) as e:
        await imagegen.generate(prov, imagegen.build_payload("a cat", model="m", size="1024x1024"),
                                max_bytes=1024 * 1024, deadline_s=10, client=fake.client())
    assert "403" in str(e.value)


async def test_a_download_bigger_than_the_cap_is_refused_while_streaming(env):
    orch, store, g, prov, srv = env
    fake = FakeServer(body={"data": [{"url": "http://127.0.0.1:9/image-out.png"}]},
                      image=b"x" * 5000)
    with pytest.raises(imagegen.ImageError) as e:
        await imagegen.generate(prov, imagegen.build_payload("a cat", model="m", size="1024x1024"),
                                max_bytes=1000, deadline_s=10, client=fake.client())
    assert "larger" in str(e.value)


async def test_the_key_is_sent_as_a_bearer_token(env):
    orch, store, g, prov, srv = env
    fake = FakeServer(key_expected="sk-test-1234567890")
    got = await imagegen.generate(prov, imagegen.build_payload("a cat", model="m", size="1024x1024"),
                                 max_bytes=1024 * 1024, deadline_s=10, client=fake.client())
    assert got["data"] == PNG


async def test_failures_name_the_setting_to_go_and_change(env):
    orch, store, g, prov, srv = env
    for status, body, want in (
        (401, {"error": {"message": "bad key"}}, "key"),
        (404, {"error": {"message": "model not found"}}, "model"),
        (429, {"error": {"message": "too many requests"}}, "rate limited"),
        (400, {"error": {"message": "unsupported size"}}, "size"),
        (400, {"error": {"message": "content policy violation"}}, "content policy"),
        (503, {"error": {"message": "upstream down"}}, "transient"),
    ):
        fake = FakeServer(status=status, body=body)
        with pytest.raises(imagegen.ImageError) as e:
            await imagegen.generate(prov, imagegen.build_payload("x", model="m", size="1024x1024"),
                                    max_bytes=1024 * 1024, deadline_s=10, client=fake.client())
        assert want in str(e.value), f"{status} should mention {want}: {e.value}"


# ------------------------------------------------------------------ where it lands
async def test_the_image_lands_in_the_groups_own_image_folder(env):
    orch, store, g, prov, srv = env
    out = await draw(orch, store, g)
    assert out.ok, out.text
    files = list(image_dir(store, g).glob("*.png"))
    assert len(files) == 1 and files[0].read_bytes() == PNG
    assert out.files == [{"kind": "image", "name": files[0].name, "bytes": len(PNG)}]
    assert "cannot see the result" in out.text, "the model must be told not to describe it"


async def test_a_symlinked_image_folder_is_refused(env):
    """A member can create `image` as a symlink with run_code; the save must not follow it."""
    orch, store, g, prov, srv = env
    ws = Path(store.data_dir) / "workspaces" / g["id"]
    ws.mkdir(parents=True, exist_ok=True)
    outside = ws.parent / "outside"
    outside.mkdir(exist_ok=True)
    (ws / "image").symlink_to(outside, target_is_directory=True)
    out = await draw(orch, store, g)
    assert not out.ok and "symlink" in out.text
    assert list(outside.iterdir()) == []


def test_a_planted_link_at_the_staging_name_is_not_followed(tmp_path, monkeypatch):
    """Same property as the video writer, which the two now share."""
    ws = tmp_path / "ws"
    (ws / "image").mkdir(parents=True)
    monkeypatch.setattr(media.time, "strftime", lambda *_: "20260101-000000")
    monkeypatch.setattr(media.os, "getpid", lambda: 4242)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"precious")
    (ws / "image" / ".20260101-000000-a-cat.4242.part").symlink_to(victim)

    with pytest.raises(imagegen.ImageError) as e:
        imagegen.save(b"NEW", ws, "a cat")

    assert victim.read_bytes() == b"precious" and "staging" in str(e.value)


def test_two_images_with_the_same_prompt_do_not_overwrite_each_other(tmp_path):
    ws = tmp_path / "ws"
    (ws / "image").mkdir(parents=True)
    a = imagegen.save(b"one", ws, "a cat")
    b = imagegen.save(b"two", ws, "a cat")
    assert a != b and a.read_bytes() == b"one" and b.read_bytes() == b"two"


# ------------------------------------------------------------- picking a provider
def test_an_image_provider_is_not_usable_for_video_and_the_other_way_round(store):
    """Both are "media"; picking by the union of kinds would hand the video tool an image
    endpoint, which surfaces as a broken server rather than a wrong lookup."""
    image_prov = store.add_provider_from_preset("openai-image")
    video_prov = store.add_provider_from_preset("minimax-h3")
    assert [p["id"] for p in imagegen.media_providers(store)] == [image_prov["id"]]
    assert [p["id"] for p in video.media_providers(store)] == [video_prov["id"]]
    assert set(media.MEDIA_KINDS) == {"minimax_video", "openai_image"}
    # and neither is offered to a member as a chat model
    assert [m["id"] for m in store.list_models() if m["provider_id"] in (image_prov["id"], video_prov["id"])] == []


def test_a_configured_id_that_no_longer_exists_is_reported_not_replaced(store):
    store.add_provider_from_preset("openai-image")
    prov, why = imagegen.pick_provider(store, {"image_provider_id": "gone", "external_calls_enabled": True})
    assert prov is None and "gone" in why


def test_a_non_local_provider_needs_outbound_calls(store):
    prov = store.add_provider_from_preset("openai-image")
    store.update_provider(prov["id"], {"base_url": "https://llm-api.mmchat.xyz/v1"})
    store.update_settings({"external_calls_enabled": False})
    p = store.get_provider(prov["id"])
    assert "outbound calls" in imagegen.blocked_by_offline(p, store.get_settings())
    store.update_settings({"external_calls_enabled": True})
    assert imagegen.blocked_by_offline(p, store.get_settings()) == ""


# --------------------------------------------------------------------- the tool
def test_the_tool_is_only_offered_when_it_can_work(env):
    orch, store, g, prov, srv = env
    specs = builtin_specs()
    assert specs["generate_image"]["risk"] == "exec", "it reaches a service outside this app"
    assert timeout_budget(store.get_settings(), specs["generate_image"]) == 30

    async def names():
        ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
        return set(ctx.tools), ctx.problems

    import asyncio
    got, _problems = asyncio.run(names())
    assert "generate_image" in got and "generate_video" not in got, "no video provider was added"

    store.update_settings({"image_enabled": False})
    got, _ = asyncio.run(names())
    assert "generate_image" not in got

    store.update_settings({"image_enabled": True, "external_calls_enabled": False})
    store.update_provider(prov["id"], {"is_local": False})
    got, problems = asyncio.run(names())
    assert "generate_image" not in got and any("outbound calls" in p for p in problems), \
        "a member is not handed a tool that the offline switch forbids, and the user is told why"


async def test_a_size_the_service_does_not_produce_is_refused_before_anything_is_sent(env):
    orch, store, g, prov, srv = env
    out = await draw(orch, store, g, {"size": "4096x4096"})
    assert not out.ok and "size" in out.text


# -------------------------------------------------------------------- probing
async def test_the_probe_reads_the_model_list_without_generating(env):
    orch, store, g, prov, srv = env
    fake = FakeServer()
    ok, detail = await imagegen.probe(prov, "gpt-image-1", client=fake.client())
    assert ok and "gpt-image-1" in detail and "/models" in fake.paths[0]
    assert fake.payloads == [], "the probe must not generate anything"

    ok, detail = await imagegen.probe(prov, "some-model-nobody-has", client=fake.client())
    assert ok and "some-model-nobody-has" in detail, "a name that is not on the list is worth saying"

    bad = FakeServer(status=401, body={"error": {"message": "bad key"}})
    ok, detail = await imagegen.probe({**prov, "base_url": "http://127.0.0.1:9/v1"}, "m", client=bad.client())
    assert not ok and "key" in detail
