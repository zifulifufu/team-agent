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

from . import comfyui, i18n, music, musicprompt, musicwork
from .api_ext import Ctx
from .store import new_id

# What a browser may be handed, by container. Kept next to the shelf rather than guessed at the call
# site: a wrong media type makes a player refuse a file that is perfectly good.
AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                     ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
                     ".opus": "audio/opus"}


class MusicComposeIn(BaseModel):
    """What to compose. Two ways in, and they are **added together**: the faders (`genre` …
    `vocals`) are composed into a well-formed tag string by `musicprompt.compose_tags`, and
    `prompt` is free text appended after it for anything the vocabulary has no word for.

    ⚠️ `prompt` alone still works — that is what the tool and the first version of this page sent —
    but a free sentence cannot carry the five things the model actually reads (genre first,
    concrete instruments, production, BPM that matches the genre, no contradictory tags).
    """
    prompt: str = ""
    genre: str = ""
    instruments: list[str] = []
    production: list[str] = []
    vocals: str = ""
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
    # `.get`, not `[...]`: a job row built by a test (or by an older page) may predate `warnings`,
    # and a missing display field must not turn a working job into a 500.
    return {k: job.get(k) for k in ("id", "state", "prompt", "name", "seconds", "bytes",
                                    "error", "note", "warnings", "started", "finished")}


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

    @r.get("/api/video-zone/music/vocabulary")
    async def music_vocabulary() -> dict:
        """The faders: genres (with the BPM range each one actually lives in), concrete instruments,
        production treatments, vocals, and the scene presets. Served rather than hard-coded in the
        page so that a member using `make_music` is composing against the same words the person at
        the keyboard is clicking.

        `moods` comes from `music.MOODS` — the shelf's closed vocabulary — rather than being listed a
        second time here: those seven words are what `publish()` validates against, and two lists
        that drift apart give a track the shelf then refuses.
        """
        return {**musicprompt.vocabulary(), "moods": list(music.MOODS)}

    @r.post("/api/video-zone/music/preview")
    async def music_preview(body: MusicComposeIn) -> dict:
        """The tag string the model would actually receive, and anything wrong with it.

        Why this is a round trip rather than the page assembling the string itself: the composition
        rules (genre first, the keyword ceiling, the BPM range each genre lives in) are
        `musicprompt.compose_tags`'s, and a second copy in the page would drift from it — the user
        would be shown one thing and sent another. It is a string join, so it costs nothing.
        """
        tags, warnings = musicprompt.compose_tags(
            genre=body.genre, mood=body.mood, instruments=body.instruments,
            production=body.production, vocals=body.vocals, bpm=int(body.bpm or 0),
            extra=body.prompt)
        return {"tags": tags, "warnings": warnings}

    # --------------------------------------------------------------- composing
    @r.post("/api/video-zone/music/compose")
    async def music_compose(body: MusicComposeIn) -> dict:
        """Start composing one track. Returns immediately; the page polls the job.

        Minutes, not seconds — ACE-Step loads ~13.7 GB of weights and then works. A request that
        waited for that would be a request that times out while the music is being made.
        """
        text, warnings = musicprompt.compose_tags(
            genre=body.genre, mood=body.mood, instruments=body.instruments,
            production=body.production, vocals=body.vocals, bpm=int(body.bpm or 0),
            extra=body.prompt)
        # ⚠️ What is refused here is **an empty choice**, not "an empty text box". `mood` has a
        # default (`neutral`), so checking the composed string alone let a user who picked nothing
        # submit a one-word prompt and get a track the model improvised off nothing — the same
        # generic-piano outcome this whole vocabulary exists to avoid. A genre is the anchor the
        # official guidance is built around; free text is the escape hatch.
        if not (str(body.genre or "").strip() or str(body.prompt or "").strip()):
            raise HTTPException(400, i18n.pick_now(
                "Pick a genre, or describe the music yourself — instruments, mood, tempo.",
                "先选一个流派；或者自己写一句描述 —— 乐器、情绪、速度。"))
        busy = next((j for j in jobs.values() if j["state"] == "running"), None)
        if busy:
            raise HTTPException(409, i18n.pick_now(
                f"A track is already being composed (started {int(time.time() - busy['started'])}s "
                "ago). Wait for it to finish.",
                f"已经有一首在作曲(已开始 {int(time.time() - busy['started'])} 秒)。等它做完。"))
        job_id = new_id()
        job = {"id": job_id, "state": "running", "prompt": text, "name": "", "seconds": 0.0,
               "bytes": 0, "error": "", "note": "", "warnings": warnings,
               "started": time.time(), "finished": 0.0, "task": None}
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
