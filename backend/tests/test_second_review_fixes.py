"""Regressions from the second read-only review (Claude Code, four themed passes).

Each case maps to one finding: the docstring names what is protected and why the obvious
"check it somewhere else" fix is not enough. Findings the review reported that turned out to be
by design (the app is single-subject: the token is the only boundary, and a group is a project
context rather than a principal) are deliberately not encoded here.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import coderun
from app.api_ext import MAX_UPLOAD_BYTES, _refuse_oversized
from app.library import MAX_CHARS
from app.main import create_app
from tests.conftest import FakeLLM, chunk
from tests.test_collab import Collector, setup

OCT = {"Content-Type": "application/octet-stream"}


def client(tmp_path, fake=None, name="data", token=None):
    app = create_app(tmp_path / name, completion_fn=fake or FakeLLM(default="OK"), token=token)
    return TestClient(app, base_url="http://127.0.0.1"), app


def two_groups(store) -> tuple[dict, dict]:
    """Two independent workspaces: the default data already holds one, so only a second is made."""
    first = store.list_groups()[0]
    store.create_group("第二群")
    second = next(g for g in store.list_groups() if g["id"] != first["id"])
    return first, second


# ======================================================= video route: symlink escape
def test_video_route_refuses_a_symlink_out_of_the_workspace(tmp_path):
    """The name regex only constrains the *requested* name. A member's code tool can leave a symlink
    called x.mp4 in the workspace, and following it would make this route read any file the account
    can read — so the resolved path has to be checked as well, not just the name."""
    cl, app = client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    ws = coderun.workspace_path(Path(store.data_dir), store.get_settings(), gid)
    (ws / "video").mkdir(parents=True, exist_ok=True)
    (ws / "video" / "x.mp4").symlink_to(secret)

    r = cl.get(f"/api/groups/{gid}/video/x.mp4")
    assert r.status_code == 404
    assert "TOP SECRET" not in r.text

    # a real file in the video folder still works
    (ws / "video" / "real.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    assert cl.get(f"/api/groups/{gid}/video/real.mp4").status_code == 200


def test_video_route_refuses_a_symlinked_video_folder(tmp_path):
    """The parent directory can be the symlink instead of the leaf; the resolved-path check covers
    that case too."""
    cl, app = client(tmp_path)
    store = app.state.store
    gid = store.list_groups()[0]["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "y.mp4").write_bytes(b"not really a video")
    ws = coderun.workspace_path(Path(store.data_dir), store.get_settings(), gid)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "video").symlink_to(outside, target_is_directory=True)

    assert cl.get(f"/api/groups/{gid}/video/y.mp4").status_code == 404


# ======================================================= upload size before buffering
def test_an_oversized_body_is_refused_from_the_declared_length(tmp_path):
    """The per-format size checks inside `images` / `library` run on a body that is already in memory,
    so they cap what gets stored, not what gets allocated."""

    def request_with(length: str) -> Request:
        scope = {
            "type": "http", "method": "POST", "path": "/x", "query_string": b"",
            "headers": [(b"content-length", length.encode())],
        }
        return Request(scope)

    with pytest.raises(HTTPException) as err:
        _refuse_oversized(request_with(str(MAX_UPLOAD_BYTES + 1)), MAX_UPLOAD_BYTES)
    assert err.value.status_code == 413

    _refuse_oversized(request_with("1024"), MAX_UPLOAD_BYTES)          # a normal upload passes
    # a chunked upload declares no length: it falls through to the per-format checks, which is why
    # those callers verify the byte count once more after reading
    _refuse_oversized(Request({"type": "http", "method": "POST", "path": "/x",
                               "query_string": b"", "headers": []}), MAX_UPLOAD_BYTES)


# ======================================================= knowledge base ownership
def test_a_knowledge_base_cannot_be_moved_to_another_group(tmp_path):
    """`group_id` is the ownership field: rewriting it hands one workspace's documents to another
    (or orphans a shared base from every group). It is not a rename, so it is not patchable."""
    cl, app = client(tmp_path)
    store = app.state.store
    first, second = two_groups(store)
    kb = store.add_kb("私人库", "", first["id"])

    r = cl.patch(f"/api/knowledge-bases/{kb['id']}", json={"name": "改个名", "group_id": second["id"]})
    assert r.status_code == 200
    assert store.get_kb(kb["id"])["name"] == "改个名"                 # ordinary fields still patch
    assert store.get_kb(kb["id"])["group_id"] == first["id"]           # ownership does not move

    store.update_kb(kb["id"], {"group_id": second["id"]})              # nor through the store API
    assert store.get_kb(kb["id"])["group_id"] == first["id"]


def test_a_document_cannot_be_moved_into_another_groups_knowledge_base(tmp_path):
    """Moving a document between the bases of one workspace is normal. Moving it into another
    workspace's private base changes who may read it, which is the boundary the libraries exist for."""
    cl, app = client(tmp_path)
    store = app.state.store
    first, second = two_groups(store)
    mine = store.add_kb("我的库", "", first["id"])
    theirs = store.add_kb("他的库", "", second["id"])
    shared = store.add_kb("共享库", "", "")

    doc = cl.post("/api/library/note", json={"title": "病历", "content": "内容", "kb_id": mine["id"]}).json()

    assert cl.patch(f"/api/library/{doc['id']}", json={"kb_id": theirs["id"]}).status_code == 403
    assert store.get_doc(doc["id"])["kb_id"] == mine["id"]
    assert cl.patch(f"/api/library/{doc['id']}", json={"kb_id": shared["id"]}).status_code == 200
    assert store.get_doc(doc["id"])["kb_id"] == shared["id"]


# ======================================================= import must not eat the old document
def test_reimporting_keeps_the_old_document_when_the_new_text_is_too_long(tmp_path, monkeypatch):
    """`add_dir` replaces a changed file by deleting the old row first, and `add_text` is what rejects
    text over the character limit. Rejection after the delete means the document is simply gone, with
    a "skipped" line as the only trace — so every check has to run before the delete."""
    cl, app = client(tmp_path)
    store = app.state.store
    kb = store.add_kb("库", "", "")
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.txt"
    f.write_text("原始内容", encoding="utf-8")

    first = cl.post("/api/library/dir", json={"path": str(src), "recursive": True, "kb_id": kb["id"]})
    assert first.status_code == 200
    doc = first.json()["added"][0]

    monkeypatch.setattr("app.library.extract_text", lambda name, data: ("text", "x" * (MAX_CHARS + 1)))
    f.write_text("改过的内容", encoding="utf-8")                        # different size, so it is treated as changed
    again = cl.post("/api/library/dir", json={"path": str(src), "recursive": True, "kb_id": kb["id"]})
    assert again.json()["added"] == []
    assert again.json()["skipped"], "the over-long replacement has to be reported as skipped"
    assert store.get_doc(doc["id"]) is not None, "the old document was deleted for a replacement that never landed"


# ======================================================= rounds claim their message
def test_a_round_stands_down_when_a_later_user_message_arrives(store):
    """A round stores its user message and broadcasts it *before* taking the group lock, so a later
    message can arrive while it waits. That later round reads both (the history is the whole group) and
    answers both, so the earlier one stands down instead of answering the same conversation twice."""
    gid = store.list_groups()[0]["id"]
    m1 = store.add_message(gid, "user", "user", "我", "甲")
    assert store.has_later_user_message(gid, m1["id"]) is False
    m2 = store.add_message(gid, "user", "user", "我", "乙")
    assert store.has_later_user_message(gid, m1["id"]) is True     # the later round answers both
    assert store.has_later_user_message(gid, m2["id"]) is False    # the newest message goes ahead
    assert store.has_later_user_message(gid, "does-not-exist") is False

    # a member's reply is not a user message, and the history is shared: it must never make a round
    # stand down, or every message after the first member reply would go unanswered
    store.add_message(gid, "agent", "a1", "Aide", "回答")
    assert store.has_later_user_message(gid, m2["id"]) is False


def test_two_messages_waiting_for_the_lock_are_answered_once(store, make_router):
    """The real bug: because the store-and-broadcast happen outside the lock, a round that started
    later could find a newer user message at the end of its history and answer *that*, leaving its own
    question unanswered. Now the round that reaches the lock stands down if a later message exists, and
    the later round carries both — one answer, covering both messages."""
    fake = FakeLLM(default="好的")
    orch, group = setup(store, make_router, fake, plan_mode="off")
    if not store.group_members(group["id"]):
        pytest.skip("the default workspace has no members to run a round with")

    async def run() -> None:
        collector = Collector()
        lock = orch._lock(group["id"])
        await lock.acquire()                       # hold the group so both messages queue behind it
        try:
            first = asyncio.create_task(orch.handle_user_message(group["id"], "问题甲", collector))
            await asyncio.sleep(0)
            second = asyncio.create_task(orch.handle_user_message(group["id"], "问题乙", collector))
            await asyncio.sleep(0)                 # both are stored now, both are waiting for the lock
        finally:
            lock.release()
        await asyncio.gather(first, second)

    asyncio.run(run())

    prompts = ["".join(m["content"] for m in msgs if m["role"] == "user") for _, msgs in fake.calls]
    answered = [p for p in prompts if "问题甲" in p or "问题乙" in p]
    assert answered, "no round ran at all"
    assert all("问题乙" in p for p in answered), \
        "a round answered the earlier message on its own after a later one had arrived"
    assert any("问题甲" in p and "问题乙" in p for p in answered), \
        "the round that ran did not carry both messages, so the first question was dropped"


# ======================================================= stream budget
def test_the_request_timeout_covers_the_whole_stream(store, make_router):
    """`timeout` is the budget for the request, not per chunk: waiting on each `__anext__` with the
    full budget lets an endpoint that dribbles one token just under the limit hold a turn (and with
    it the group lock) open for as long as it likes."""
    slow_every = 0.02
    ticks = 100

    async def fn(**kw):
        async def gen():
            for _ in range(ticks):
                await asyncio.sleep(slow_every)
                yield chunk("x")
        return gen()

    router = make_router(fn)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(router._stream_one({}, [], 0.2, None, {}))
    elapsed = time.monotonic() - started
    assert elapsed < ticks * slow_every / 2, f"the stream ran for {elapsed:.2f}s; the budget did not apply to the whole request"


# ======================================================= docs outside the token
def test_the_api_surface_is_not_exposed_when_a_token_is_set(tmp_path):
    """The generated docs live outside /api, which is all the token middleware guards: with a token in
    use, any local page could still read every route and field name from /openapi.json."""
    guarded, _ = client(tmp_path, name="guarded", token="s3cret")
    assert guarded.get("/openapi.json").status_code == 404
    assert guarded.get("/docs").status_code == 404
    assert guarded.get("/redoc").status_code == 404
    assert guarded.get("/api/groups").status_code == 401                       # the API itself still guards
    assert guarded.get("/api/groups", headers={"x-team-agent-token": "s3cret"}).status_code == 200

    dev, _ = client(tmp_path, name="dev")                                       # no token: open by design
    assert dev.get("/openapi.json").status_code == 200
