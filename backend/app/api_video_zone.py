"""The video zone: the parts of film-making that a person drives directly.

`make_music` lets a **member** ask for a track inside a group chat. This is the same thing for the
person at the keyboard: write the style tags, press compose, listen to it, keep it. Nothing here
re-implements composing — it calls `musicwork.compose`, which is exactly what the tool calls. The
one thing that differs is *where the file is written while it is being made* (see `compose_dir`).

Music is the first block of the zone; scenes and motion will sit beside it in the same router.
"""

from __future__ import annotations

import asyncio
import mimetypes
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import attachments, comfyui, i18n, music, musicprompt, musicwork, studio
# `_need_octet` / `_refuse_oversized` are imported rather than re-written: they are the two checks
# that keep a cross-origin form out and refuse a runaway body before it is buffered, and a second
# copy of either is a second answer to "may this arrive at all".
from .api_ext import UPLOAD_HARD_BYTES, Ctx, _need_octet, _refuse_oversized
from .store import new_id

# What a piece of the user's own material can be, mapped from what the **bytes** say it is. Not from
# the extension: a `.mov` that is really a still is a thing that happens, and this value decides
# which generator may be pointed at the asset at all.
STUDIO_KINDS = {attachments.IMAGE: "photo", attachments.VIDEO: "video", attachments.AUDIO: "audio"}

# What a browser may be handed, by container. Kept next to the shelf rather than guessed at the call
# site: a wrong media type makes a player refuse a file that is perfectly good.
AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                     ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
                     ".opus": "audio/opus"}


class MusicComposeIn(BaseModel):
    """Make music from **a description**, optional lyrics, and the two things a person is
    entitled to have an opinion about: who sings, and how long it is.

    ⚠️ There used to be six faders here — genre, instruments, production, mood, BPM. They are gone
    on purpose (2026-09-27, the user's words: 情绪也没必要放到面板上，这些都在算法里面). What replaced
    them is `musicprompt.read()`: one function that turns a sentence into the five things the model
    actually reads. Keeping the fields *and* the reading would be two answers to the same question —
    the panel's faders would silently win and the algorithm would never run.

    `vocals` is the exception, and deliberately: "" or `auto` means "you decide", anything else is
    the person overriding the reading. Voice is the one thing an author reliably has an opinion
    about.
    """
    prompt: str = ""
    lyrics: str = ""
    vocals: str = ""
    seconds: float = 0.0
    name: str = ""
    seed: int = 0
    language: str = "en"


# ⚠️⚠️ These two have to be **module-level**, like `MusicComposeIn` above. Declared inside
# `build_video_zone_router` they work fine for the request itself, but FastAPI cannot resolve a local
# class when it builds the OpenAPI schema — `/openapi.json` then raises
# `PydanticUserError: ForwardRef('AssetPatch') is not fully defined`, which takes the whole API
# description down with it. Caught by `test_the_api_surface_is_not_exposed_when_a_token_is_set`,
# which is the only test that opens that document.
class StudioAssetPatch(BaseModel):
    title: str = ""
    note: str = ""
    tags: list[str] = []


class StudioTakePatch(BaseModel):
    """`reviewed` and `chosen` are separate on purpose: watched is not the same as wanted."""
    reviewed: bool | None = None
    chosen: bool | None = None
    name: str = ""


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
    return {k: job.get(k) for k in ("id", "state", "prompt", "read", "name", "seconds", "bytes",
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
        """The words behind the music: genres (each with the BPM range it lives in), concrete
        instruments, production treatments, vocals, and the scene presets.

        Served rather than hard-coded in the page for two reasons: the panel needs the vocal options,
        and a member composing through `make_music` should be working against the same words the
        person at the keyboard sees.

        ⚠️ `moods` used to ride along here — the seven words the shelf validates against — because
        the panel had a mood selector. The selector is gone (the reading decides the mood now), and a
        field with no reader is the shape of thing this project keeps deleting. `music.MOODS` is
        still the one list; `musicprompt._MOOD_CUES` is held to it by a test.
        """
        return musicprompt.vocabulary()

    @r.post("/api/video-zone/music/preview")
    async def music_preview(body: MusicComposeIn) -> dict:
        """What the model would receive, and **what the description was read as**.

        A round trip rather than the page assembling the string itself: the reading rules and the
        assembling rules are `musicprompt`'s, and a second copy in the page would drift — the user
        would be shown one thing and sent another. It is a string join, so it costs nothing.
        """
        tags, d, warn = _direct(body)
        return {"tags": tags, "read": d, "warnings": warn}

    def _direct(body: MusicComposeIn) -> tuple[str, dict, list[str]]:
        """`(tags, read, warnings)` — **唯一一条**从描述到标签的路。

        The compose endpoint and the preview one both call this. Two call sites that assembled the
        tag string separately is exactly how the user ends up being shown one prompt and sent
        another — and this project has paid for that shape of bug more than once.
        """
        d = musicprompt.read(body.prompt, vocals=body.vocals, lyrics=body.lyrics)
        tags, warn = musicprompt.compose_tags(
            genre=d["genre"], mood=d["mood"], instruments=d["instruments"],
            production=d["production"], vocals=d["vocals"], bpm=d["bpm"])
        # `read()` 的 notes（「这句话里没读出流派」这类）也一并交给页面：它和 warnings 一样是
        # 「有件事你该知道」，分两个渠道只会让其中一个没人接。
        return tags, d, warn + list(d["notes"])

    # --------------------------------------------------------------- composing
    @r.post("/api/video-zone/music/compose")
    async def music_compose(body: MusicComposeIn) -> dict:
        """Start composing one track. Returns immediately; the page polls the job.

        Minutes, not seconds — ACE-Step loads ~13.7 GB of weights and then works. A request that
        waited for that would be a request that times out while the music is being made.
        """
        # ⚠️ The only thing that can be refused now is **an empty description**, because the
        # description is the only input left. That is a real improvement on what was here before:
        # the old check had to reason about "did they pick a genre, or did they type something"
        # because the two were alternatives — with one input there is nothing to disambiguate.
        if not str(body.prompt or "").strip():
            raise HTTPException(400, i18n.pick_now(
                "Describe the music first — say what it is for, what it is made of, how it should "
                "feel. One sentence is enough.",
                "先写一句描述 —— 它用在哪、由什么组成、什么感觉。一句话就够。"))
        text, read, warnings = _direct(body)
        busy = next((j for j in jobs.values() if j["state"] == "running"), None)
        if busy:
            raise HTTPException(409, i18n.pick_now(
                f"A track is already being composed (started {int(time.time() - busy['started'])}s "
                "ago). Wait for it to finish.",
                f"已经有一首在作曲(已开始 {int(time.time() - busy['started'])} 秒)。等它做完。"))
        job_id = new_id()
        job = {"id": job_id, "state": "running", "prompt": text, "read": read, "name": "",
               "seconds": 0.0, "bytes": 0, "error": "", "note": "", "warnings": warnings,
               "started": time.time(), "finished": 0.0, "task": None}
        jobs[job_id] = job

        async def run() -> None:
            try:
                made = await musicwork.compose(
                    store, c.toolhub.comfy_runtime, prompt=text, workspace=compose_dir(),
                    seconds=float(body.seconds or 0), bpm=int(read["bpm"]),
                    language=str(body.language or "en"), seed=int(body.seed or 0),
                    lyrics=str(body.lyrics or ""), name=str(body.name or ""),
                    mood=str(read["mood"]),
                    # The shelf's own metadata: which genre it was read as, and the first two
                    # instruments. Ids, not the tag string — `music.publish` validates `mood`
                    # against its closed vocabulary, and these are the same ids the page shows.
                    tags=[read["genre"], *read["instruments"][:2]])
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

    # ------------------------------------------------------------------ the private studio
    # 用户的**私有**素材与由它生成的每一次产出。⚠️ 这一段的边界与其它库**故意相反**：器械参考库是
    # 「拷进每个群、成员随便看」，而这里是「你自己的脸和你的录像」—— 它不在任何群的工作目录里、
    # 不进知识库、不参与导出；成员只能通过显式引用拿到**产物**，永远拿不到原始素材。
    def _asset_view(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "dir"}

    def _take_view(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "dir"}

    def _serve(path: Path, mime: str = "") -> FileResponse:
        """字节交给浏览器。媒体类型以记录下来的为准 —— 猜错会让播放器拒掉一个完好的文件。"""
        kind = mime or (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        return FileResponse(path, media_type=kind, filename=path.name)

    @r.get("/api/video-zone/studio/assets")
    async def studio_assets(kind: str = "", tag: str = "") -> dict:
        """The private shelf. `kind`/`tag` filter, and a sidecar that will not parse is **named**
        rather than skipped: material this app can no longer describe is material it must not
        generate from."""
        return {"assets": [_asset_view(x) for x in studio.assets(kind=kind, tag=tag).values()],
                "errors": studio.errors(), "kinds": list(studio.KINDS)}

    @r.post("/api/video-zone/studio/assets")
    async def studio_upload(request: Request, filename: str = "file", title: str = "",
                            note: str = "", tags: str = "") -> dict:
        """Raw bytes, like every other upload in this app.

        ⚠️ `kind` is **not** a parameter: it comes from the bytes (`attachments.classify`), so a
        `.mov` that is really a still cannot claim to be a video and be handed to a video generator.
        """
        _need_octet(request)
        cfg = store.get_settings()
        limit = min(int(cfg["upload_max_mb"]) * 1024 * 1024, UPLOAD_HARD_BYTES)
        _refuse_oversized(request, limit)
        data = await request.body()
        if not data:
            raise HTTPException(400, i18n.pick_now("That file was empty.", "这个文件是空的。"))
        if len(data) > limit:
            raise HTTPException(413, i18n.pick_now("That file is too large.", "文件太大。"))
        found, mime, _ext = attachments.classify(data, filename)
        kind = STUDIO_KINDS.get(found)
        if kind is None:
            raise HTTPException(400, i18n.pick_now(
                "The studio keeps photos, video and audio — that file is none of them.",
                "工作室只收照片、视频和音频 —— 这个文件都不是。"))
        try:
            row = await asyncio.to_thread(
                lambda: studio.add_asset(title=title, kind=kind, filename=filename, data=data,
                                         note=note, mime=mime,
                                         tags=[t for t in str(tags).split(",") if t.strip()]))
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from None
        return {"asset": _asset_view(row)}

    @r.get("/api/video-zone/studio/assets/{asset_id}/file")
    async def studio_asset_file(asset_id: str) -> FileResponse:
        row = studio.asset(asset_id)
        path = studio.file_of_asset(asset_id)
        if row is None or path is None:
            raise HTTPException(404, i18n.pick_now("No such material", "没有这份素材"))
        return _serve(path, str(row.get("mime") or ""))

    @r.patch("/api/video-zone/studio/assets/{asset_id}")
    async def studio_asset_patch(asset_id: str, body: StudioAssetPatch) -> dict:
        try:
            row = studio.update_asset(asset_id, title=body.title, note=body.note, tags=body.tags)
        except KeyError:
            raise HTTPException(404, i18n.pick_now("No such material", "没有这份素材")) from None
        return {"asset": _asset_view(row)}

    @r.delete("/api/video-zone/studio/assets/{asset_id}")
    async def studio_asset_delete(asset_id: str) -> dict:
        """Removes the material **and every take made from it** — see `studio.remove_asset`."""
        if not studio.remove_asset(asset_id):
            raise HTTPException(404, i18n.pick_now("No such material", "没有这份素材"))
        return {"ok": True}

    @r.get("/api/video-zone/studio/assets/{asset_id}/takes")
    async def studio_takes(asset_id: str) -> dict:
        """One asset's generation history, newest first. This is what "look at it and fix it" reads."""
        if studio.asset(asset_id) is None:
            raise HTTPException(404, i18n.pick_now("No such material", "没有这份素材"))
        return {"takes": [_take_view(t) for t in studio.takes(asset_id)]}

    @r.get("/api/video-zone/studio/takes/{take_id}/file")
    async def studio_take_file(take_id: str) -> FileResponse:
        row = studio.take(take_id)
        path = studio.file_of_take(take_id)
        if row is None or path is None:
            raise HTTPException(404, i18n.pick_now("That take produced no file", "这次生成没有产出文件"))
        return _serve(path)

    @r.patch("/api/video-zone/studio/takes/{take_id}")
    async def studio_take_patch(take_id: str, body: StudioTakePatch) -> dict:
        """「看过」与「选中」是两件事：看过 ≠ 满意，选中才是要进片子的那一个。"""
        try:
            row = studio.update_take(take_id, reviewed=body.reviewed, chosen=body.chosen,
                                     name=body.name or None)
        except KeyError:
            raise HTTPException(404, i18n.pick_now("No such take", "没有这次生成")) from None
        return {"take": _take_view(row)}

    @r.delete("/api/video-zone/studio/takes/{take_id}")
    async def studio_take_delete(take_id: str) -> dict:
        if not studio.remove_take(take_id):
            raise HTTPException(404, i18n.pick_now("No such take", "没有这次生成"))
        return {"ok": True}

    return r
