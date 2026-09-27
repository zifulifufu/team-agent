"""The video zone's music block: the shelf, the bytes, and what happens when it cannot compose.

Composing a real track is minutes of GPU work, so what is pinned here is the **HTTP surface** —
what a page may ask for and what it is told when it cannot have it — plus the one judgement
`musicwork.compose` makes before it touches ComfyUI at all.

⚠️ `music.use_folder` is module-global and `create_app` sets it from the store's data dir, so each
test gets its own shelf simply by using its own tmp dir — no fixture has to reach into the module.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import music, musicwork
from app.main import create_app
from tests.conftest import FakeLLM


def open_app(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM())
    return app, TestClient(app, base_url="http://127.0.0.1")


def put_track(tmp_path, stem="测试曲", seconds=12.5, mood="calm"):
    """A track on the shelf: the audio bytes plus the sidecar the shelf reads."""
    folder = tmp_path / "data" / music.FOLDER_NAME
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.mp3").write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 64)
    (folder / f"{stem}.json").write_text(json.dumps({
        "title": "一首测试曲", "mood": mood, "tags": ["instrumental", "piano"],
        "seconds": seconds, "source": "composed here", "licence": "ACE-Step 1.5 (MIT)",
    }, ensure_ascii=False), encoding="utf-8")
    return folder / f"{stem}.mp3"


# ------------------------------------------------------------------ the shelf
def test_the_shelf_lists_a_track_with_its_metadata(tmp_path):
    with open_app(tmp_path)[1] as c:
        put_track(tmp_path)
        body = c.get("/api/video-zone/music").json()

    assert [t["title"] for t in body["tracks"]] == ["一首测试曲"]
    track = body["tracks"][0]
    assert track["mood"] == "calm" and track["seconds"] == 12.5
    assert track["tags"] == ["instrumental", "piano"]
    # The page asks for bytes by name; a path crossing the wire is not part of the contract.
    assert "file" not in track and "path" not in track


def test_the_shelf_says_whether_this_machine_can_compose(tmp_path):
    """"Ready" and "why not" are two halves of one answer — never one without the other."""
    with open_app(tmp_path)[1] as c:
        composer = c.get("/api/video-zone/music").json()["composer"]

    assert set(composer) >= {"ready", "why", "base_url"}
    assert bool(composer["why"]) is not composer["ready"]


def test_the_audio_is_served_with_a_type_a_player_accepts(tmp_path):
    with open_app(tmp_path)[1] as c:
        path = put_track(tmp_path)
        r = c.get("/api/video-zone/music/测试曲/audio")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.content == path.read_bytes()


def test_a_track_that_is_not_on_the_shelf_is_404_not_a_guess(tmp_path):
    with open_app(tmp_path)[1] as c:
        assert c.get("/api/video-zone/music/没有这首/audio").status_code == 404
        assert c.delete("/api/video-zone/music/没有这首").status_code == 404
        # Nothing may reach outside the shelf: a name that climbs out is simply not a key on it.
        assert c.delete("/api/video-zone/music/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_deleting_takes_the_audio_and_its_sidecar(tmp_path):
    with open_app(tmp_path)[1] as c:
        put_track(tmp_path)
        assert c.delete("/api/video-zone/music/测试曲").json() == {"ok": True}
        assert c.get("/api/video-zone/music").json()["tracks"] == []

    folder = tmp_path / "data" / music.FOLDER_NAME
    assert not list(folder.glob("测试曲*")), "the sidecar must go with the audio"


# ------------------------------------------------------------------ composing
def test_composing_without_a_description_is_refused(tmp_path):
    with open_app(tmp_path)[1] as c:
        r = c.post("/api/video-zone/music/compose", json={"prompt": "   "})
    assert r.status_code == 400


def test_only_one_track_is_composed_at_a_time(tmp_path):
    """Two at once would fight over the same ComfyUI queue and the same shelf name."""
    app, client = open_app(tmp_path)
    with client as c:
        c.app.state.ctx.music_job["busy"] = {
            "id": "busy", "state": "running", "prompt": "piano", "name": "", "seconds": 0.0,
            "bytes": 0, "error": "", "note": "", "started": 0.0, "finished": 0.0, "task": None}
        r = c.post("/api/video-zone/music/compose", json={"prompt": "calm piano"})

    assert r.status_code == 409
    assert "already" in r.json()["detail"] or "已经" in r.json()["detail"]


def test_a_unknown_job_is_404(tmp_path):
    with open_app(tmp_path)[1] as c:
        assert c.get("/api/video-zone/music/jobs/nope").status_code == 404


# ---------------------------------------------------- the composer's own judgement
def test_composing_names_the_missing_comfyui(tmp_path):
    """Before it touches anything: no local ComfyUI means the person is told which piece is missing."""

    class Store:
        def get_settings(self):
            return {"video_max_mb": 512, "music_timeout": 1800}

        def providers_for_use(self, use, kinds):
            return []

    with pytest.raises(musicwork.MusicError) as err:
        asyncio.run(musicwork.compose(Store(), None, prompt="calm piano", workspace=tmp_path))

    assert "ComfyUI" in str(err.value)
