"""Composing a track: **the one implementation**, shared by the tool and the video zone.

`make_music` grew this first — a member asks for a track inside a group chat, and it lands on the
music shelf so the film being made *right now* can be scored with it. The video zone needs exactly
the same thing behind a button, and a second copy of "find ComfyUI, check ACE-Step, build the graph,
generate, shelve the result" is how the two would drift apart. So it lives here, once.

⚠️ Everything about *which* ComfyUI and *what* the refusal says is load-bearing:
  * ComfyUI **specifically**, not "whichever video provider is set" — music is composed on this
    machine, and a group pointed at a cloud video service must not quietly send the request there;
  * when it cannot run, the refusal **names the weight file that is missing and where to get it**
    (`comfyui.music_diagnosis`). A ComfyUI traceback tells a person nothing they can act on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import comfyui, i18n, media, music, net, video


class MusicError(Exception):
    """Something a person can act on — the message always names the missing piece."""


@dataclass
class Composition:
    """What one run produced. `shelved` is None when the file exists but never reached the shelf."""

    path: Path                  # the audio, inside the caller's workspace
    name: str                   # the title it is known by (the shelf's stem when it got there)
    seconds: float
    bytes: int
    shelved: "Path | None" = None
    shelf_note: str = ""        # why it did not reach the shelf, said out loud rather than silently


def local_comfyui(store: Any) -> "dict | None":
    """The local ComfyUI this machine composes music with, or None when there is not one.

    Asked once, here: the tool and the zone must not disagree about which instance gets the job.
    """
    rows = store.providers_for_use("video", video.KINDS)
    return next((p for p in rows if p.get("kind") == comfyui.KIND and p.get("enabled")), None)


async def compose(
    store: Any,
    runtime: Any,
    *,
    prompt: str,
    workspace: Path,
    seconds: float = 0.0,
    bpm: int = 120,
    language: str = "en",
    seed: int = 0,
    lyrics: str = "",
    name: str = "",
    mood: str = "neutral",
    tags: "tuple[str, ...] | list[str]" = (),
    timeout: "float | None" = None,
    max_bytes: int = 0,
) -> Composition:
    """Compose one track with ACE-Step and put it on the music shelf.

    `prompt` is **style tags** (genre, instruments, mood, tempo) because that is what ACE-Step's
    encoders were trained on. Instrumental unless `lyrics` is given — and lyrics are usually wrong
    for a bed under a narrator. This takes minutes, not seconds.
    """
    cfg = store.get_settings()
    prov = local_comfyui(store)
    if prov is None:
        raise MusicError(i18n.pick_now(
            "Music is composed on this machine, and no local ComfyUI is set up for it yet. Add one "
            "under Settings → Media, then download the ACE-Step weights it names.",
            "音乐是在本机作的,但还没有可用的本地 ComfyUI。先在「设置 → 媒体」里加一个,"
            "再按它点名的文件下载 ACE-Step 权重。"))
    prompt = str(prompt or "").strip()
    if not prompt:
        raise MusicError(i18n.pick_now("The description was empty, so nothing was composed.",
                                       "描述是空的,没有作曲。"))
    length = max(10.0, min(float(seconds or comfyui.MUSIC_DEFAULT_SECONDS), comfyui.MUSIC_MAX_SECONDS))
    ready, why = await runtime.ensure(prov, cfg)
    if not ready:
        raise MusicError(why)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    base = str(prov.get("base_url") or "")
    # Ask the instance what it can load, rather than looking in a folder: ComfyUI builds each
    # loader's list from the folders it was started with, so this answers "can it run".
    async with net.client(base, timeout=comfyui.INTROSPECT_TIMEOUT) as c:
        try:
            info = await c.get(media.api_url(base, comfyui.OBJECT_INFO_ROUTE),
                               headers=comfyui._headers(prov))
            schemas = info.json() if info.status_code < 400 else {}
        except (httpx.HTTPError, ValueError) as e:
            raise MusicError(i18n.pick_now(
                f"Could not ask ComfyUI what it can run ({type(e).__name__}: {e}), so nothing was "
                f"composed. Is it still up at {base}?",
                f"问不到 ComfyUI 能跑什么({type(e).__name__}: {e}),所以没有作曲。{base} 还活着吗?")) from None
    if not comfyui.music_supported(schemas) or comfyui.music_missing(schemas):
        raise MusicError(comfyui.music_diagnosis(schemas))
    graph = comfyui.music_graph(tags=prompt, lyrics=str(lyrics or ""), seconds=length,
                                bpm=int(bpm or 120), language=str(language or "en"),
                                seed=int(seed or 0))
    budget = int(max_bytes) or max(1, int(cfg["video_max_mb"])) * 1024 * 1024
    try:
        r = await video.generate(prov, {"graph": graph, "workflow": "ace-step-1.5"},
                                 workspace=workspace, max_bytes=budget,
                                 deadline_s=float(timeout or cfg["music_timeout"]))
    except video.VideoError as e:
        raise MusicError(str(e)) from None
    path = Path(str(r["path"]))
    got_seconds = float(r.get("seconds") or length)
    title = str(name or "").strip() or prompt[:40]
    shelved: "Path | None" = None
    note = ""
    try:
        data = await asyncio.to_thread(path.read_bytes)
        shelved = await asyncio.to_thread(lambda: music.publish(
            title, data,
            # ⚠️ Not `path.suffix`: the download helper names what it fetches as a *clip*, so this
            # file arrives as `.mp4` while its bytes are an MP3. The graph's last node is
            # `SaveAudioMP3`, so the container is known from the graph rather than guessed from a
            # name we did not choose.
            suffix=".mp3", mood=str(mood or "neutral"), tags=list(tags or ()),
            seconds=got_seconds,
            source=i18n.pick_now("composed here with ACE-Step (local ComfyUI)",
                                 "本机用 ACE-Step 生成(本地 ComfyUI)"),
            licence="ACE-Step 1.5 (MIT)", title=title))
    except (OSError, ValueError, KeyError) as e:
        # The file is still in the workspace, so a film can be scored with it by path; what is lost
        # is finding it later. Say exactly that instead of failing a render that worked.
        note = i18n.pick_now(
            f"(It could not be put on the music shelf: {type(e).__name__}: {e}. The file is in "
            "the workspace, so a film can still use it by path.)",
            f"(没能上架到音乐库:{type(e).__name__}: {e}。文件在工作目录里,片子仍可按路径用它。)")
    return Composition(path=path, name=(shelved.stem if shelved else title), seconds=got_seconds,
                       bytes=int(r["bytes"]), shelved=shelved, shelf_note=note)
