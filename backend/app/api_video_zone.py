"""The video zone: the parts of film-making that a person drives directly.

`make_music` lets a **member** ask for a track inside a group chat. This is the same thing for the
person at the keyboard: write the style tags, press compose, listen to it, keep it. Nothing here
re-implements composing — it calls `musicwork.compose`, which is exactly what the tool calls. The
one thing that differs is *where the file is written while it is being made* (see `compose_dir`).

Music is the first block of the zone; scenes and motion will sit beside it in the same router.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import comfyui, i18n, music, musicwork
from .api_ext import Ctx
from .store import new_id

# What a browser may be handed, by container. Kept next to the shelf rather than guessed at the call
# site: a wrong media type makes a player refuse a file that is perfectly good.
AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                     ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
                     ".opus": "audio/opus"}


class MusicComposeIn(BaseModel):
    prompt: str
    seconds: float = 0.0
    bpm: int = 120
    language: str = "en"
    seed: int = 0
    lyrics: str = ""
    name: str = ""
    mood: str = "neutral"
    tags: list[str] = []


def _track_view(t: dict) -> dict:
    """A shelf row as the page reads it. `file`/`path` stay here — the page asks for bytes by name."""
    return {"name": t["name"], "title": t["title"], "mood": t["mood"], "tags": t["tags"],
            "seconds": t["seconds"], "bytes": t["bytes"], "source": t["source"],
            "licence": t["licence"]}


def _job_view(job: dict) -> dict:
    """Only the fields the page reads.

    ⚠️ The asyncio task lives in the same dict so it can be found again; serialising the whole dict
    would try to send it over the wire.
    """
    return {k: job[k] for k in ("id", "state", "prompt", "name", "seconds", "bytes",
                                "error", "note", "started", "finished")}


def build_video_zone_router(c: Ctx) -> APIRouter:
    r = APIRouter()
    store = c.store
    jobs = c.music_job

    def compose_dir() -> Path:
        """Where a track is written while it is being made.

        ⚠️ A **subfolder** of the shelf, never the shelf itself: the generator names what it
        downloads as a clip (`.mp4`, measured) while its bytes are an MP3 — and a stray `.mp4`
        sitting in the shelf folder is read straight back as a track whose name lies about its
        container. The shelf scans files only, so a dotted subfolder is invisible to it.
        """
        d = store.data_dir / music.FOLDER_NAME / ".compose"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --------------------------------------------------------------- the shelf
    @r.get("/api/video-zone/music")
    async def music_shelf() -> dict:
        """What is on the shelf, and whether this machine can compose another one right now."""
        shelf = music.available()
        prov = musicwork.local_comfyui(store)
        return {
            "tracks": [_track_view(t) for t in sorted(shelf.values(), key=lambda t: t["name"])],
            # Named, never hidden: a sidecar that does not parse is why a track looks like it has
            # no metadata, and the file that is wrong is the only useful thing to say.
            "errors": music.errors(),
            "composer": {
                "ready": prov is not None,
                "base_url": str((prov or {}).get("base_url") or ""),
                "why": "" if prov else i18n.pick_now(
                    "No local ComfyUI is set up, so nothing can be composed yet. Add one under "
                    "Settings → Media, then download the ACE-Step weights it names.",
                    "还没有可用的本地 ComfyUI,暂时作不了曲。先在「设置 → 媒体」里加一个,"
                    "再按它点名的文件下载 ACE-Step 权重。"),
            },
            "limits": {"min_seconds": 10.0, "max_seconds": comfyui.MUSIC_MAX_SECONDS,
                       "default_seconds": comfyui.MUSIC_DEFAULT_SECONDS},
        }

    @r.get("/api/video-zone/music/{name}/audio")
    async def music_audio(name: str) -> FileResponse:
        """The bytes, so the page can play it.

        Handed over as a file response rather than read into memory: a five-minute track is tens of
        megabytes, and a browser wants to start playing before it has all of them.
        """
        path = music.file_of(name)
        if path is None or not path.is_file():
            raise HTTPException(404, i18n.pick_now("That track is not on the shelf",
                                                   "音乐库里没有这首"))
        return FileResponse(path, media_type=AUDIO_MEDIA_TYPES.get(path.suffix.lower(), "audio/mpeg"),
                            filename=path.name)

    # --------------------------------------------------------------- composing
    @r.post("/api/video-zone/music/compose")
    async def music_compose(body: MusicComposeIn) -> dict:
        """Start composing one track. Returns immediately; the page polls the job.

        Minutes, not seconds — ACE-Step loads ~13.7 GB of weights and then works. A request that
        waited for that would be a request that times out while the music is being made.
        """
        text = str(body.prompt or "").strip()
        if not text:
            raise HTTPException(400, i18n.pick_now("Describe the music first — genre, instruments, "
                                                   "mood, tempo.",
                                                   "先写一句音乐描述 —— 流派、乐器、情绪、速度。"))
        busy = next((j for j in jobs.values() if j["state"] == "running"), None)
        if busy:
            raise HTTPException(409, i18n.pick_now(
                f"A track is already being composed (started {int(time.time() - busy['started'])}s "
                "ago). Wait for it to finish.",
                f"已经有一首在作曲(已开始 {int(time.time() - busy['started'])} 秒)。等它做完。"))
        job_id = new_id()
        job = {"id": job_id, "state": "running", "prompt": text, "name": "", "seconds": 0.0,
               "bytes": 0, "error": "", "note": "", "started": time.time(), "finished": 0.0,
               "task": None}
        jobs[job_id] = job

        async def run() -> None:
            try:
                made = await musicwork.compose(
                    store, c.toolhub.comfy_runtime, prompt=text, workspace=compose_dir(),
                    seconds=float(body.seconds or 0), bpm=int(body.bpm or 120),
                    language=str(body.language or "en"), seed=int(body.seed or 0),
                    lyrics=str(body.lyrics or ""), name=str(body.name or ""),
                    mood=str(body.mood or "neutral"),
                    tags=[str(t) for t in (body.tags or []) if str(t).strip()])
            except musicwork.MusicError as e:
                # Everything a person can act on (no ComfyUI, weights missing, engine refused)
                # arrives as a sentence — pass it through untouched.
                job.update(state="failed", error=str(e), finished=time.time())
                return
            except Exception as e:  # noqa: BLE001 — a background job must not die silently
                job.update(state="failed", error=f"{type(e).__name__}: {e}", finished=time.time())
                return
            job.update(state="done", name=made.name, seconds=made.seconds, bytes=made.bytes,
                       note=made.shelf_note, finished=time.time())

        job["task"] = asyncio.create_task(run())
        return {"job": _job_view(job)}

    @r.get("/api/video-zone/music/jobs/{job_id}")
    async def music_job(job_id: str) -> dict:
        found = jobs.get(job_id)
        if not found:
            raise HTTPException(404, i18n.pick_now("No such job", "没有这个任务"))
        return {"job": _job_view(found)}

    @r.delete("/api/video-zone/music/{name}")
    async def music_delete(name: str) -> dict:
        if not music.remove(name):
            raise HTTPException(404, i18n.pick_now("That track is not on the shelf",
                                                   "音乐库里没有这首"))
        return {"ok": True}

    return r
