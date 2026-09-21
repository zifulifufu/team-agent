"""Images in the group chat: what gets stored, and who is allowed to see it."""

from __future__ import annotations

import struct
import time

import pytest
from fastapi.testclient import TestClient

from app import images
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_collab import setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64                  # magic bytes are all that is checked
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64


def webp(payload: bytes = b"\x00" * 64) -> bytes:
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload


OCTET = {"Content-Type": "application/octet-stream"}


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def upload(c, gid, data, name="shot.png"):
    return c.post(f"/api/groups/{gid}/attachments", params={"filename": name}, content=data, headers=OCTET)


def gid(c):
    return c.get("/api/groups").json()[0]["id"]


# ------------------------------------------------------------------ the bytes
def test_sniff_goes_by_content_not_by_name():
    assert images.sniff(PNG) == ("image/png", "png")
    assert images.sniff(JPEG) == ("image/jpeg", "jpg")
    assert images.sniff(GIF) == ("image/gif", "gif")
    assert images.sniff(webp()) == ("image/webp", "webp")
    # A zip with the right extension, an HTML page, a plain PDF: none are images
    assert images.sniff(b"PK\x03\x04rest of a zip") is None
    assert images.sniff(b"<html><body>hi</body></html>") is None
    assert images.sniff(b"%PDF-1.7\n") is None
    assert images.sniff(b"") is None
    assert images.sniff(b"RIFF\x00\x00\x00\x00AVI ") is None       # RIFF, but not WEBP


def test_check_reports_empty_oversize_and_wrong_type():
    cfg = {"vision_max_mb": 8}
    assert images.check(b"", cfg)
    assert images.check(PNG, cfg) is None
    assert images.check(PNG + b"\x00" * (2 * 1024 * 1024), {"vision_max_mb": 1})     # 1 MB cap, over it
    big = images.check(PNG + b"\x00" * (9 * 1024 * 1024), cfg)
    assert big and "MB" in big
    assert images.check(b"PK\x03\x04", cfg)


def test_display_name_strips_paths_and_control_characters():
    assert images.display_name("/etc/passwd") == "passwd"
    assert images.display_name("../../x.png") == "x.png"
    assert images.display_name("a\x00b\x07c.png") == "abc.png"
    assert images.display_name("") == "image"
    assert len(images.display_name("z" * 500)) == 80


# ------------------------------------------------------------------ the endpoints
def test_upload_serve_and_delete(api):
    g = gid(api)
    r = upload(api, g, PNG)
    assert r.status_code == 200
    row = r.json()
    assert row["mime"] == "image/png" and row["bytes"] == len(PNG) and row["name"] == "shot.png"

    got = api.get(f"/api/attachments/{row['id']}")
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"].startswith("image/png")
    assert api.delete(f"/api/attachments/{row['id']}").status_code == 200
    assert api.get(f"/api/attachments/{row['id']}").status_code == 404


def test_a_file_that_is_not_an_image_is_refused_whatever_it_is_called(api):
    g = gid(api)
    r = upload(api, g, b"<html>not an image</html>", name="trust-me.png")
    assert r.status_code == 400 and "PNG" in r.json()["detail"]
    assert api.get("/api/attachments/whatever").status_code == 404


def test_the_size_cap_is_checked_before_anything_is_written(api, tmp_path):
    api.put("/api/settings", json={"vision_max_mb": 1})
    g = gid(api)
    assert upload(api, g, PNG + b"\x00" * (2 * 1024 * 1024)).status_code == 400
    stored = list((tmp_path / "data" / "attachments").glob("*"))
    assert stored == [], "an oversized upload must not leave a file behind"


def test_upload_needs_a_group_that_exists(api):
    assert upload(api, "nope", PNG).status_code == 404


# ------------------------------------------------------------------ the message
def test_a_message_can_be_images_only(api):
    g = gid(api)
    aid = upload(api, g, PNG).json()["id"]
    assert api.post(f"/api/groups/{g}/messages", json={"text": "", "images": [aid]}).status_code == 200
    sent = next(m for m in api.get(f"/api/groups/{g}/messages").json() if m["sender_type"] == "user")
    assert sent["content"] == "" and [i["id"] for i in sent["meta"]["images"]] == [aid]


def test_an_empty_message_is_still_refused(api):
    g = gid(api)
    assert api.post(f"/api/groups/{g}/messages", json={"text": "  "}).status_code == 400


def test_an_attachment_from_another_group_is_dropped(api):
    """The client is not the authority on what an image is or where it came from."""
    a = gid(api)
    other = api.post("/api/groups", json={"name": "Second group"}).json()["id"]
    aid = upload(api, other, PNG).json()["id"]
    api.post(f"/api/groups/{a}/messages", json={"text": "look", "images": [aid, "made-up"]})
    sent = next(m for m in api.get(f"/api/groups/{a}/messages").json() if m["sender_type"] == "user")
    assert "images" not in sent["meta"]


# ------------------------------------------------------------------ who may see it
@pytest.fixture
def vision_setup(store, make_router):
    """A group whose host is pinned to a local multimodal model, plus a PNG on disk."""
    st = store
    st.update_settings({"memory_auto_extract": False})
    st.add_model("ollama", "qwen3.5:9b")                        # catalog lists it as vision-capable
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    assert "multimodal" in [m["strengths"] for m in st.list_models() if m["id"] == "ollama/qwen3.5:9b"][0]
    host = st.list_agents()[0]
    st.update_agent(host["id"], {"model_id": "ollama/qwen3.5:9b"})
    aid = "img1"
    images.path_for(st.data_dir, aid, "png").write_bytes(PNG)
    st.add_attachment(g["id"], aid, "shot.png", "image/png", len(PNG))
    return orch, st, g, host


def built(orch, st, g, host):
    st.add_message(g["id"], "user", "user", "me", "what is this?", meta={"images": [{"id": "img1", "name": "shot.png", "mime": "image/png", "bytes": len(PNG)}]})
    msgs = orch.build_messages(st.get_group(g["id"]), st.get_agent(host["id"]), st.list_agents())
    return msgs[-1]


def test_a_local_vision_model_gets_the_image(vision_setup):
    orch, st, g, host = vision_setup
    last = built(orch, st, g, host)
    assert isinstance(last["content"], list)
    parts = [p["type"] for p in last["content"]]
    assert parts == ["text", "image_url"]
    assert last["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_model_that_cannot_see_is_told_so_instead_of_guessing(vision_setup):
    orch, st, g, host = vision_setup
    st.add_model("ollama", "qwen2.5:7b")                        # same local provider, no multimodal tag
    st.update_agent(host["id"], {"model_id": "ollama/qwen2.5:7b"})
    last = built(orch, st, g, host)
    assert isinstance(last["content"], str) and "cannot see it" in last["content"]


def test_a_cloud_model_needs_the_image_switch(vision_setup):
    """`vision_cloud` is separate from `external_calls_enabled` on purpose: text leaving the
    machine and a picture the user attached leaving it are different decisions."""
    orch, st, g, host = vision_setup
    st.add_model("deepseek", "deepseek-flash")                  # cloud, catalog says vision
    st.update_agent(host["id"], {"model_id": "deepseek/deepseek-flash"})

    st.update_settings({"vision_cloud": False})
    last = built(orch, st, g, host)
    assert isinstance(last["content"], str) and "cannot see it" in last["content"]

    st.update_settings({"vision_cloud": True})
    msgs = orch.build_messages(st.get_group(g["id"]), st.get_agent(host["id"]), st.list_agents())
    assert isinstance(msgs[-1]["content"], list) and msgs[-1]["content"][1]["type"] == "image_url"


def test_images_come_from_the_most_recent_message_that_has_any(vision_setup):
    """Older images are not re-uploaded on every reply; only the newest set travels."""
    orch, st, g, host = vision_setup
    first = built(orch, st, g, host)
    assert isinstance(first["content"], list), "the image message itself carries the picture"
    # A later message without images still resolves back to the last set that had them
    st.add_message(g["id"], "user", "user", "me", "and now?")
    msgs = orch.build_messages(st.get_group(g["id"]), st.get_agent(host["id"]), st.list_agents())
    assert isinstance(msgs[-1]["content"], list) and msgs[-1]["content"][1]["type"] == "image_url"
    assert "and now?" in msgs[-1]["content"][0]["text"], "the new turn is still the one being answered"


def test_images_are_not_sent_to_a_group_that_never_had_any(vision_setup):
    orch, st, g, host = vision_setup
    st.add_message(g["id"], "user", "user", "me", "plain text question")
    msgs = orch.build_messages(st.get_group(g["id"]), st.get_agent(host["id"]), st.list_agents())
    assert isinstance(msgs[-1]["content"], str)


def test_a_missing_file_is_skipped_rather_than_breaking_the_turn(vision_setup):
    orch, st, g, host = vision_setup
    next(iter(images.folder(st.data_dir).glob("img1.*"))).unlink()
    last = built(orch, st, g, host)
    assert isinstance(last["content"], str), "no image parts left, but the turn still goes out"


# ------------------------------------------------------------------ housekeeping
def test_the_sweep_only_takes_uploads_that_were_never_sent(store):
    old = time.time() - 30 * 86400
    g = store.list_groups()[0]["id"]
    store.add_attachment(g, "used", "a.png", "image/png", 10)
    store.add_attachment(g, "orphan", "b.png", "image/png", 10)
    images.path_for(store.data_dir, "orphan", "png").write_bytes(PNG)
    for aid in ("used", "orphan"):
        store._x("UPDATE attachments SET created_at=? WHERE id=?", (old, aid))
    store.add_message(g, "user", "user", "me", "hi", meta={"images": [{"id": "used", "mime": "image/png"}]})

    assert images.sweep(store, store.data_dir) == 1
    assert store.get_attachment("used") and not store.get_attachment("orphan")
    assert not images.find_file(store.data_dir, "orphan")


def test_a_referenced_image_survives_however_old_the_message(store):
    g = store.list_groups()[0]["id"]
    store.add_attachment(g, "kept", "a.png", "image/png", 10)
    images.path_for(store.data_dir, "kept", "png").write_bytes(PNG)
    store._x("UPDATE attachments SET created_at=?", (time.time() - 999 * 86400,))
    store.add_message(g, "user", "user", "me", "hi", meta={"images": [{"id": "kept", "mime": "image/png"}]})
    assert images.sweep(store, store.data_dir) == 0
    assert images.find_file(store.data_dir, "kept")
