"""What a model is for, and what follows from knowing it.

A gateway answers one endpoint with chat models, image models and models that live on a different
API altogether. Listing them together is not wrong — it is what the provider does — but "this model
exists" and "a member can be pointed at it" stopped being the same statement, and the difference
costs money and time when it is discovered after a round has already gone wrong.
"""

import asyncio
import json

import pytest

from app import imagegen, media, modelopts
from app.media import purpose_of
from app.store import Store


# ------------------------------------------------------------------ reading the provider's word
def test_the_providers_own_word_beats_the_name():
    """`mode` is the authority when it is there. MetaChat sends it; nothing else has to be guessed."""
    assert purpose_of("gpt-image-1.5", "image_generation") == "image"
    assert purpose_of("gpt-5.2-pro", "responses") == "responses"
    assert purpose_of("anything-at-all", "video_generation") == "video"
    # A name that *looks* like an image model still answers to what the provider said it is.
    assert purpose_of("gpt-image-1", "chat") == "chat"


def test_an_unknown_name_stays_a_chat_model():
    """The guess may only ever be wrong in the harmless direction: calling a chat model an image
    model would hide a model that works, which is worse than showing one that does not."""
    for name in ("gpt-4o", "deepseek-v4-pro", "claude-opus-5", "kimi-k3", "qwen3.8-max",
                 "chat-latest", "o3", "gemini-robotics-er-1.6-preview", "gemini-2.5-flash"):
        assert purpose_of(name) == "chat", name


def test_a_name_that_is_unmistakable_is_read_when_the_provider_says_nothing():
    assert purpose_of("dall-e-3") == "image"
    assert purpose_of("gemini-3-pro-image") == "image"
    assert purpose_of("sora-2") == "video"
    assert purpose_of("veo-3.1") == "video"
    assert purpose_of("kling-v2") == "video"


# ------------------------------------------------------------------ what a refresh stores
def test_a_refresh_remembers_what_each_model_is_for(store):
    """The listing is where the answer lives, so the refresh writes it down — including for the rows
    the user already added, whose purpose until now could only be guessed from the name."""
    store.add_provider("gateway", "openai_compatible", base_url="https://gw.example/v1")
    pid = "gateway"
    added = store.add_model(pid, "gpt-image-1.5")            # added before the provider said anything
    assert added["use"] == "image"                           # the name got it right this time

    # Nothing in this name says what it is — which is the case the provider's own answer exists for.
    rough = store.add_model(pid, "internal-ap-7b")
    assert rough["use"] == "chat"

    applied = store.sync_model_uses(pid, {"internal-ap-7b": "image_generation",
                                         "gpt-image-1.5": "image_generation"})
    assert applied == 1
    assert store.get_model("gateway/internal-ap-7b")["use"] == "image"
    assert store.sync_model_uses(pid, {"internal-ap-7b": "image_generation",
                                      "gpt-image-1.5": "image_generation"}) == 0, "idempotent"


def test_the_live_listing_carries_the_purposes(store):
    store.add_provider("gateway", "openai_compatible", base_url="https://gw.example/v1")
    store.set_model_live("gateway", ["gpt-5.2", "gpt-image-2"], {"gpt-image-2": "image_generation"})
    live = store.get_model_live("gateway")
    assert live["ids"] == ["gpt-5.2", "gpt-image-2"]
    assert live["modes"] == {"gpt-image-2": "image_generation"}


def test_a_listing_fetched_before_purposes_were_stored_still_works(tmp_path):
    """Upgrading must not need a refresh before the lists are usable: the column arrives empty, the
    reader treats that as "the provider did not say", and the name is read instead."""
    st = Store(tmp_path / "data")
    st.add_provider("gateway", "openai_compatible", base_url="https://gw.example/v1")
    st._x("UPDATE model_live SET modes='{}' WHERE 0")        # no-op, keeps the shape of an old row
    st._x("INSERT OR REPLACE INTO model_live(provider_id,ids,fetched_at) VALUES(?,?,?)",
          ("gateway", json.dumps(["gpt-image-1.5", "gpt-5.2"]), 0.0))   # modes defaults to '{}'
    assert st.get_model_live("gateway")["modes"] == {}
    assert st.provider_serves_use("gateway", "image") is True
    assert st.provider_serves_use("gateway", "video") is False


# ------------------------------------------------------------------ who may generate what
def test_a_gateway_is_offered_for_images_only_when_it_really_serves_them(store):
    """The point of the whole change: one MetaChat key can draw, and a key for a provider with no
    image models must not be offered — pointing the image tool at it buys a failure, and the user
    picks from this list."""
    store.add_provider("metachat", "openai_compatible", base_url="https://llm.example/v1")
    store.add_provider("plain", "openai_compatible", base_url="https://chat.example/v1")
    store.set_model_live("metachat", ["gpt-5.2", "gpt-image-1.5"], {"gpt-image-1.5": "image_generation"})
    store.set_model_live("plain", ["some-chat-model"], {})

    ids = [p["id"] for p in store.providers_for_use("image", imagegen.KINDS)]
    assert ids == ["metachat"], "a chat-only gateway is not an image service"


def test_a_real_image_provider_is_offered_whatever_it_lists(store):
    store.add_provider("myimage", "openai_image", base_url="https://img.example/v1")
    ids = [p["id"] for p in store.providers_for_use("image", imagegen.KINDS)]
    assert ids == ["myimage"]


def test_the_two_generators_never_borrow_each_other_s_providers(store):
    store.add_provider("myimage", "openai_image", base_url="https://img.example/v1")
    store.add_provider("myvideo", "minimax_video", base_url="http://127.0.0.1:9000")
    assert [p["id"] for p in store.providers_for_use("image", imagegen.KINDS)] == ["myimage"]
    from app import video
    assert [p["id"] for p in store.providers_for_use("video", video.KINDS)] == ["myvideo"]


def test_the_image_tool_picks_a_gateway_that_serves_image_models(store):
    """`pick_provider` is what the tool actually calls, so this is the end of the chain: the setting
    holds a gateway id, and the drawing goes to the gateway."""
    store.add_provider("metachat", "openai_compatible", base_url="https://llm.example/v1")
    store.set_model_live("metachat", ["gpt-image-1.5"], {"gpt-image-1.5": "image_generation"})
    store.update_settings({"image_provider_id": "metachat"})

    prov, why = imagegen.pick_provider(store, store.get_settings())
    assert why == "" and prov is not None and prov["id"] == "metachat"


# ------------------------------------------------------------------ what the pages are told
def test_the_model_picker_says_what_each_model_is_for(store):
    store.add_provider("metachat", "openai_compatible", base_url="https://llm.example/v1")
    store.set_model_live("metachat", ["gpt-5.2", "gpt-image-1.5", "gpt-5.2-pro"],
                         {"gpt-image-1.5": "image_generation", "gpt-5.2-pro": "responses"})
    opts = modelopts.model_options(store, "metachat")
    uses = {m["id"]: m["use"] for m in opts["models"]}
    assert uses["gpt-5.2"] == "chat"
    assert uses["gpt-image-1.5"] == "image"
    assert uses["gpt-5.2-pro"] == "responses"


def test_the_media_options_list_the_models_to_name(tmp_path):
    """What the image and video settings are built from — the provider, and the model names its own
    listing called that medium."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(tmp_path / "app" / "data", completion_fn=None, background=False),
                        base_url="http://127.0.0.1")
    client.post("/api/providers", json={"preset": "metachat", "api_key": "sk-not-a-real-key"})
    r = client.get("/api/media/options?use=image")
    assert r.status_code == 200
    body = r.json()
    assert body["use"] == "image"
    # Nothing has been refreshed in this test, so no gateway can prove it serves images yet.
    assert [p["id"] for p in body["providers"]] == []
    assert client.get("/api/media/options?use=video").json()["providers"] == []
    assert client.get("/api/media/options?use=everything").status_code == 422


def test_a_refresh_then_the_options_offer_the_gateway_and_its_models(store, monkeypatch):
    store.add_provider("metachat", "openai_compatible", base_url="https://llm.example/v1")

    async def fake(provider, timeout=15.0, client=None):
        return [{"id": "gpt-5.2", "mode": "chat"},
                {"id": "gpt-image-1.5", "mode": "image_generation"},
                {"id": "sora-2", "mode": "video_generation"}]

    monkeypatch.setattr("app.modelopts.fetch_models", fake)
    asyncio.run(modelopts.refresh_live(store, "metachat"))

    assert store.provider_serves_use("metachat", "image") is True
    assert store.provider_serves_use("metachat", "video") is True, "the same gateway serves both"
    imgs = modelopts.models_for_use(store, "metachat", "image")
    assert [m["id"] for m in imgs] == ["gpt-image-1.5"]
    vids = modelopts.models_for_use(store, "metachat", "video")
    assert [m["id"] for m in vids] == ["sora-2"]
