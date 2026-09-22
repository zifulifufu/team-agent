"""Images in the group chat: what gets stored, and who is allowed to see it."""

from __future__ import annotations

import struct
import time

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import images
from app.main import create_app
from app.orchestrator import RunState
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


def test_an_html_page_named_png_is_stored_as_what_it_is(api):
    """Attachments are any kind of file now, and the kind comes from the bytes: calling it .png
    must not make a member try to look at it as a picture."""
    g = gid(api)
    r = upload(api, g, b"<html>not an image</html>", name="trust-me.png")
    assert r.status_code == 200
    # The bytes win over the name, so an HTML page is not treated as a picture — and the type it
    # is given is the text it actually contains, not the image the name claims.
    assert r.json()["kind"] == "document" and r.json()["mime"] == "text/plain"
    assert api.get("/api/attachments/whatever").status_code == 404


def test_the_size_cap_is_checked_before_anything_is_written(api, tmp_path):
    """`upload_max_mb` bounds any kind of file — an upload may be a video, so the cap is what the
    user sets, and it is enforced before a single byte is written."""
    assert api.put("/api/settings", json={"upload_max_mb": 1}).status_code == 200
    g = gid(api)
    assert upload(api, g, PNG + b"\x00" * (2 * 1024 * 1024)).status_code in (400, 413)
    stored = list((tmp_path / "data" / "workspaces" / g).rglob("*"))
    assert [p for p in stored if p.is_file()] == [], "an oversized upload must not leave a file behind"


def test_upload_needs_a_group_that_exists(api):
    assert upload(api, "nope", PNG).status_code == 404


# ------------------------------------------------------------------ the message
def test_a_message_can_be_images_only(api):
    g = gid(api)
    aid = upload(api, g, PNG).json()["id"]
    assert api.post(f"/api/groups/{g}/messages", json={"text": "", "attachments": [aid]}).status_code == 200
    sent = next(m for m in api.get(f"/api/groups/{g}/messages").json() if m["sender_type"] == "user")
    assert sent["content"] == "" and [i["id"] for i in sent["meta"]["files"]] == [aid]


def test_an_empty_message_is_still_refused(api):
    g = gid(api)
    assert api.post(f"/api/groups/{g}/messages", json={"text": "  "}).status_code == 400


def test_an_attachment_from_another_group_is_dropped(api):
    """The client is not the authority on what an image is or where it came from."""
    a = gid(api)
    other = api.post("/api/groups", json={"name": "Second group"}).json()["id"]
    aid = upload(api, other, PNG).json()["id"]
    api.post(f"/api/groups/{a}/messages", json={"text": "look", "attachments": [aid, "made-up"]})
    sent = next(m for m in api.get(f"/api/groups/{a}/messages").json() if m["sender_type"] == "user")
    assert "files" not in sent["meta"]


# ------------------------------------------------------------------ who may see it
@pytest.fixture
def vision_setup(store, make_router):
    """A group whose host is pinned to a local multimodal model, plus a PNG on disk."""
    st = store
    st.update_settings({"memory_auto_extract": False})
    st.add_model("ollama", "qwen3.5:9b")                        # catalog lists it as vision-capable
    # A scripted answer for the local model, so a test can tell a description apart from a reply.
    orch, g = setup(st, make_router, FakeLLM(script={"ollama": "描述:一张化验单"}, default="好"))
    assert "multimodal" in [m["strengths"] for m in st.list_models() if m["id"] == "ollama/qwen3.5:9b"][0]
    host = st.list_agents()[0]
    st.update_agent(host["id"], {"model_id": "ollama/qwen3.5:9b"})
    aid = "img1"
    images.path_for(st.data_dir, aid, "png").write_bytes(PNG)
    st.add_attachment(g["id"], aid, "shot.png", "image/png", len(PNG))
    return orch, st, g, host


def prepared(orch, st, g, host, aid: str = "img1"):
    """What this member is given for the message that carries `aid`.

    The lookup used to happen inside `build_messages` (walking back through the transcript for the
    newest message with images). It is done once per round now, by `_files_for_turn`, so the tests
    drive that — which is also what decides between "look at it" and "be told about it".
    """
    run = RunState(g["id"], "what is this?")
    run.files = [{"id": aid, "name": "shot.png", "kind": "image"}]
    return asyncio.run(orch._files_for_turn(st.get_group(g["id"]), st.get_agent(host["id"]), run))


def test_a_local_vision_model_gets_the_image(vision_setup):
    orch, st, g, host = vision_setup
    got = prepared(orch, st, g, host)
    assert got.pictures and not got.block
    assert got.pictures[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_model_that_cannot_see_is_told_so_instead_of_guessing(vision_setup):
    """No model here can look: the member is told, in words it can act on, rather than left to
    invent what the picture shows."""
    orch, st, g, host = vision_setup
    st.add_model("ollama", "qwen2.5:7b")                        # same local provider, no multimodal tag
    st.update_agent(host["id"], {"model_id": "ollama/qwen2.5:7b"})
    for m in st.list_models():
        st.update_model(m["id"], {"strengths": ["chinese"]})
    got = prepared(orch, st, g, host)
    assert not got.pictures and "no model here can look at images" in got.block


def test_a_cloud_model_needs_the_image_switch(vision_setup):
    """`vision_cloud` is separate from `external_calls_enabled` on purpose: text leaving the
    machine and a picture the user attached leaving it are different decisions."""
    orch, st, g, host = vision_setup
    st.add_model("deepseek", "deepseek-flash")                  # cloud, catalog says vision
    st.update_agent(host["id"], {"model_id": "deepseek/deepseek-flash"})
    # Nothing local can look either, so the only thing that could see is a cloud model.
    st.update_model("ollama/qwen3.5:9b", {"strengths": ["chinese"]})

    st.update_settings({"vision_cloud": False})
    got = prepared(orch, st, g, host)
    assert not got.pictures, "a picture must not leave the machine before the switch is on"
    assert "not allowed to send images to a cloud model" in got.block

    st.update_settings({"vision_cloud": True})
    got = prepared(orch, st, g, host)
    assert got.pictures and got.pictures[0]["type"] == "image_url"


def test_a_local_vision_model_describes_it_instead_of_shipping_it_off(vision_setup):
    """The whole point of preferring a local model: the member that cannot see still gets a
    description, and the picture never leaves the machine."""
    orch, st, g, host = vision_setup
    st.add_model("ollama", "qwen2.5:7b")                        # the member's own model: text only
    st.update_agent(host["id"], {"model_id": "ollama/qwen2.5:7b"})
    got = prepared(orch, st, g, host)
    assert not got.pictures
    assert "描述:一张化验单" in got.block, "the member reads the local model's description"


def test_a_later_message_does_not_re_send_an_older_picture(vision_setup):
    """Only the files of the message being answered travel: a follow-up question costs no picture."""
    orch, st, g, host = vision_setup
    run = RunState(g["id"], "and now?")                          # no files on this one
    got = asyncio.run(orch._files_for_turn(st.get_group(g["id"]), st.get_agent(host["id"]), run))
    assert not got.pictures and got.block == ""


def test_a_missing_file_is_skipped_rather_than_breaking_the_turn(vision_setup):
    orch, st, g, host = vision_setup
    next(iter(images.folder(st.data_dir).glob("img1.*"))).unlink()
    got = prepared(orch, st, g, host)
    assert not got.pictures and "gone from disk" in got.block


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
