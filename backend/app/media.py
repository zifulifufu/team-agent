"""What every media generator shares: which provider kinds are generators rather than chat
models, where their output is allowed to land, and whether a non-local one may run at all.

There are two generators now (video and image) with more than one provider kind between
them, so the questions "is this a chat model?" and "can the video tool drive this provider?"
stopped being the same question. Conflating them would let the video tool pick an image
provider — a failure that looks like a broken server rather than a wrong lookup.
"""

from __future__ import annotations

import itertools
import os
import time
from pathlib import Path

import httpx

from . import i18n
from .coderun import inside as _inside

# Every provider kind that produces media instead of holding a conversation. This is the
# list `store.list_models()` filters by, so a member can never be pointed at a generator.
# Each generator imports its own subset for picking a provider (`video.KINDS`,
# `imagegen.KINDS`) — add the new kind here **and** to that subset.
#
# `metachat_media` covers both media: its one key reaches an image API *and* a video API, so it
# belongs in both subsets rather than being configured twice under two names.
MEDIA_KINDS: tuple[str, ...] = ("minimax_video", "metachat_media", "openai_image")

# MetaChat's open media API, one row per model: what it is for, where its job is submitted and
# polled, which parameters its own page of the documentation lists, and where the finished file
# is in the reply.
#
# A table rather than one request shape for the whole prefix, because the paths and the parameters
# genuinely differ. Grok and the image families share the generic `image/generate` and
# `video/generate`; Midjourney has its own `midjourney/imagine` and `midjourney/video` and polls
# both on `midjourney/result/{id}`. The knobs are not the same words either — `duration` means
# nothing to Midjourney (its clips are 5 seconds), whose own parameters are motion / loop / count.
#
# `params` lists **only** what that model's documentation shows. Anything left out takes the
# provider's documented default, which is the safe direction: a parameter a model does not
# recognise comes back as a failure the user has paid for.
#
# `file` is where the result is: `video_url` for a clip, `image_urls` (a list) on the generic image
# path, `image_url` (one picture) for Midjourney.
#
# ⚠️ These ids are MetaChat's *API* catalogue. Its website also offers Seedance, Sora, Kling and
# Veo video; none of those is reachable with a key, which is why they are absent.
MEDIA_MODELS: dict[str, dict] = {
    # ---- video
    "grok-imagine-video-1.5-preview": {
        "use": "video", "submit": "video/generate", "result": "video/result/{vid}",
        "params": ("duration", "ratio", "resolution"), "file": "video_url",
    },
    "mj-video-v1": {
        "use": "video", "submit": "midjourney/video", "result": "midjourney/result/{vid}",
        "params": ("resolution",), "file": "video_url",
    },
    # ---- images
    "grok-imagine-image-2.0": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num", "aspect"), "file": "image_urls",
    },
    "grok-imagine-image-quality": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num", "aspect"), "file": "image_urls",
    },
    "flux-2-pro": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "flux-2-max": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "flux-kontext": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "z-image-turbo": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "doubao-seedream-5-0-pro-260628": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num",), "file": "image_urls",
    },
    "doubao-seedream-5-0-260128": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num",), "file": "image_urls",
    },
    "doubao-seedream-4-5-251128": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num",), "file": "image_urls",
    },
    # Midjourney's own image endpoint returns one four-up picture rather than a list, and takes its
    # own parameter set (stylize, quality, chaos, style, seed…); only the aspect ratio is wired, so
    # the rest stays at Midjourney's defaults.
    "mj-v82": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "mj-v81": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "mj-v7": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
              "params": ("aspect",), "file": "image_url"},
    "mj-v61": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "niji-7": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "niji-6": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
}

# The one media API whose model list cannot be asked for: `/open/v1/models` and
# `/open/v1/video/models` both answer 404. So there is nothing to refresh against and these ids
# ship with the app — the preset seeds from here and `discovery` answers a refresh with the same
# list. Grouped by what a model is for, which is what the image and video settings ask for.
BUILTIN_MEDIA_MODELS: dict[str, dict[str, tuple[str, ...]]] = {
    "metachat_media": {
        use: tuple(mid for mid, m in MEDIA_MODELS.items() if m["use"] == use)
        for use in ("image", "video")
    },
}

# For `purpose_of`: a model we ship a job for is what that job says it is. Neither "mj-v82" nor
# "z-image-turbo" is a word the name rules below could be expected to recognise.
_MEDIA_MODEL_USES: dict[str, str] = {mid.lower(): m["use"] for mid, m in MEDIA_MODELS.items()}

# The generic paths, for a model name typed by hand rather than picked from the shipped list. The
# names on the settings pages are free text (a newer model should not need a release of this app),
# so a model we have no description for still gets a request — just one with no parameters, which
# is the only thing that can be sent without knowing what the model accepts.
_MEDIA_GENERIC: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "image": ("image/generate", "image/result/{vid}", (), "image_urls"),
    "video": ("video/generate", "video/result/{vid}", (), "video_url"),
}


def job_of(model: str, use: str) -> dict:
    """How to run one media job: where it is submitted, where it is polled, which parameters it
    takes, and where the finished file is in the reply.

    `use` is checked rather than trusted: pointing the image tool at a video model is a configuration
    mistake, and answering it with the image job's paths would turn that into a confusing HTTP error.
    """
    m = MEDIA_MODELS.get((model or "").strip())
    if m and m.get("use") == use:
        return m
    submit, result, params, field = _MEDIA_GENERIC[use]
    return {"use": use, "submit": submit, "result": result, "params": params, "file": field}


# --------------------------------------------------- MetaChat's reply envelope, read in both tools
#
# The video and image tools talk to the same service and get the same envelope back, so the three
# ways of reading it live here rather than being written twice and drifting apart.
def meta_ok(body: object) -> bool:
    """`status: "Success"` means the call worked. An absent one counts as success too: refusing a
    body that simply does not carry the field would fail over a cosmetic difference."""
    if not isinstance(body, dict):
        return False
    return str(body.get("status") or "").strip().lower() in ("", "success")


def meta_said(body: object) -> str:
    """The sentence out of the envelope.

    MetaChat answers 200 even when it refuses, putting the verdict in `status` and the reason in
    `message`, so a status code is no help and the body is the only place the real reason exists.
    """
    if isinstance(body, dict):
        said = " ".join(str(body.get("message") or "").split())
        return said[:300] or str(body)[:200]
    return str(body)[:200]


def job_links(data: object, field: str) -> list[str]:
    """The finished files in a job's `data`, as a list.

    One field per family: `video_url` for a clip, `image_urls` on the generic image path where
    several may come back, and `image_url` for Midjourney's single four-up picture. Normalising here
    keeps the tools from each growing their own "is it a list or a string" branch.
    """
    if not isinstance(data, dict):
        return []
    got = data.get(field)
    if isinstance(got, str):
        return [got] if got else []
    if isinstance(got, list):
        return [u for u in got if isinstance(u, str) and u]
    return []


# Where the probe asks about a task that cannot exist. Any documented path proves the same two
# things — the service answers, and it takes this key — and this one is shared by every model
# family, so the probe does not need to know which model is configured. It is a *video* result
# path even when the question is about drawing: the point is the service, not the medium.
META_PROBE = "video/result/{vid}"


async def probe_meta(base: str, key: str, *, client: "httpx.AsyncClient") -> tuple[bool, str]:
    """Is MetaChat's media API there, and does it accept this key? Renders and draws nothing.

    Asking about a task id that cannot exist. What comes back for that, measured against the live
    service with a real key, is **HTTP 500 carrying `{"status": "Fail", "message": "视频任务查询失败:
    Video task not found"}`** — not the 404 an H3-style server would give, and not the 200-with-Fail
    the rest of this API uses. So the **status code is no help here** and the only answer that means
    "something is really wrong" is 401/403; everything else, 500 included, proves the request was
    authenticated and processed, which is all this probe claims to establish.

    ⚠️ Treating `>= 400` as unreachable was the earlier mistake, and it was invisible for a while
    because the only key to hand then was an invalid one that answered 401. **A working key reported
    "the service answered HTTP 500", i.e. a healthy provider called broken.** When a probe's verdict
    depends on the failure convention of the service, assert the convention you actually observed.

    Shared by both tools rather than written twice: one key, one host, one question — and a second
    copy could only drift into asking it differently.
    """
    url = api_url(base, META_PROBE.format(vid="team-agent-probe"))
    try:
        r = await client.get(url, headers=auth_headers(key))
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"Could not reach {base}: {type(e).__name__}: {e}", f"连不上 {base}:{type(e).__name__}: {e}"
        )
    if r.status_code in (401, 403):
        return False, i18n.pick_now(
            f"{base} is up but rejected the key ({r.status_code}). MetaChat's key is created under "
            "your avatar → API management; the media API uses the same one.",
            f"{base} 是活的,但密钥被拒绝了({r.status_code})。MetaChat 的密钥在头像 → API 管理里创建,"
            "媒体接口用的是同一把。",
        )
    said = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            said = " ".join(str(body.get("message") or "").split())[:160]
    except ValueError:
        said = " ".join((r.text or "").split())[:160]
    tail = f" · {said}" if said else ""
    if r.status_code >= 400:
        # Reachable, and the key was accepted — otherwise this would have been 401/403. The body is
        # quoted so a service that is up but broken is not silently called healthy either.
        return True, i18n.pick_now(
            f"{base} accepted the key: it answered {r.status_code} for a task that does not exist{tail}.",
            f"{base} 接受了密钥:对不存在的任务返回了 {r.status_code}{tail}。",
        )
    return True, i18n.pick_now(
        f"{base} accepted the key and answered {r.status_code}{tail}.",
        f"{base} 接受了密钥,返回 {r.status_code}{tail}。",
    )


# ------------------------------------------------------------------ what a model is for
#
# A gateway lists everything on one endpoint, so a provider's model list mixes chat models with
# models that cannot hold a conversation at all: asking `gpt-image-1.5` to answer a question, or
# pointing a member at it, fails — and it fails after the user has picked it, which is the worst
# moment. The listing itself is the authority when it says what a model is for (MetaChat sends
# `mode` per entry); reading the name is only a fallback, and it is treated as the guess it is.
#
# Guessing wrong in the "chat" direction costs nothing; guessing wrong towards "image" would make
# a perfectly good chat model look unusable. So the name rules are narrow and specific, and
# anything unrecognised stays `chat`.
#
# The four answers `purpose_of` can give are `chat`, `image`, `video` and `responses` (OpenAI's
# other API shape — not a medium, but the reason a member pointed at one would fail).

# `mode` values as providers spell them. Unknown values fall through to the name rules rather
# than being forced into a bucket.
_MODE_PURPOSE: dict[str, str] = {
    "chat": "chat", "completion": "chat", "completions": "chat", "conversation": "chat",
    "image_generation": "image", "image": "image", "images": "image",
    "video_generation": "video", "video": "video", "videos": "video",
    "responses": "responses",
}

# Whole words and distinctive stems, not substrings: `-image` catches gemini-2.5-flash-image and
# qwen-image, and nothing in the chat catalogue happens to contain it.
_NAME_PURPOSE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("image", ("dall-e", "gpt-image", "-image", "image-", "flux", "seedream", "midjourney",
               "stable-diffusion", "sdxl", "imagen-", "nano-banana", "seededit")),
    ("video", ("sora", "veo-", "veo2", "veo3", "kling", "runway", "pika", "luma",
               "seedance", "hailuo", "cogvideo", "mochi", "-video", "video-", "wan2")),
)


def purpose_of(model_name: str, mode: str | None = None) -> str:
    """What a model is for: `chat` / `image` / `video` / `responses`.

    The provider's own answer wins. `responses` is not a medium but a different API shape — those
    models answer on `/responses` rather than `/chat/completions`, so a member pointed at one
    would fail for a reason nobody could guess from the name.
    """
    said = _MODE_PURPOSE.get((mode or "").strip().lower())
    if said:
        return said
    low = (model_name or "").strip().lower()
    known = _MEDIA_MODEL_USES.get(low)
    if known:
        return known
    for purpose, needles in _NAME_PURPOSE:
        if any(n in low for n in needles):
            return purpose
    return "chat"


def is_chat_model(model_name: str, mode: str | None = None) -> bool:
    """Can a member be pointed at this model?"""
    return purpose_of(model_name, mode) == "chat"

# Names for common unit sizes, so a failure reads "3.2 MB" instead of "3355 KB".
_KB = 1024


def size_label(n: int) -> str:
    return f"{n / _KB:.0f} KB" if n < _KB * _KB else f"{n / _KB / _KB:.1f} MB"


def offline_reason(provider: dict, cfg: dict, what: str, what_zh: str) -> str:
    """The rule the routing layer uses: a non-local provider is out of bounds while outbound
    calls are off. Empty string = allowed."""
    if provider["is_local"] or cfg.get("external_calls_enabled"):
        return ""
    return i18n.pick_now(
        f"\"{provider['name']}\" is not marked as local and outbound calls are switched off, so no "
        f"{what} was generated. Mark it local if it really runs on your own machine, or turn "
        "outbound calls back on.",
        f"「{provider['name']}」不是本地服务,而「允许外呼」是关的,所以没有生成{what_zh}。"
        "如果它确实跑在你自己的机器上,请把它标为本地;否则请打开「允许外呼」。",
    )


def providers_of_kind(store, kinds: tuple[str, ...]) -> list[dict]:
    """The configured providers that generate this kind of media (`imagegen.KINDS`,
    `video.KINDS`) — never a chat model, and never the other generator's kind."""
    return [p for p in store.list_providers() if p["kind"] in kinds]


def api_url(base: str, path: str) -> str:
    """Join a base URL and an API path, tolerating a base that already ends in /v1."""
    b = (base or "").strip().rstrip("/")
    p = path.lstrip("/")
    if p.startswith("v1/") and b.endswith("/v1"):
        p = p[3:]
    return f"{b}/{p}"


def auth_headers(key: str) -> dict[str, str]:
    """The headers both media generators send: JSON, plus a bearer token when there is one."""
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def slug(text: str, limit: int = 40) -> str:
    """A filename-safe fragment of a prompt, so an output file is recognisable later."""
    import re
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip(), flags=re.UNICODE).strip("-")
    return s[:limit].strip("-")


def save_bytes(data: bytes, workspace: Path, *, subdir: str, ext: str, stem_source: str,
               fallback: str, what: str, what_zh: str) -> Path:
    """Write generated media into the group's workspace, and nowhere else.

    Four properties, each for a concrete failure:

    * the directory is checked, not assumed — a member can create `video` (or `image`) as a
      symlink to somewhere else with `run_code`, and `mkdir(exist_ok=True)` would happily
      write through it;
    * **the check and the write are the same object.** Testing the path and then using it again
      leaves a window: a member's own process runs beside this one and can replace the
      directory with a symlink in between, and `O_NOFOLLOW` on the file only covers the last
      component. So the directory is opened `O_NOFOLLOW|O_DIRECTORY` and everything after that
      goes through that descriptor — a directory swapped in mid-flight makes the open fail, or
      is simply not the one the bytes are written into, instead of redirecting them;
    * the staging file is created with O_EXCL|O_NOFOLLOW, so a symlink planted at that name
      cannot make this truncate something outside the workspace, and two simultaneous saves
      cannot share one staging file;
    * publishing uses `link`, which fails if the destination already exists, so a file never
      overwrites something that appeared while it was downloading, and never appears
      partially — the name only comes into existence when the bytes are complete.
    """
    root = workspace.resolve()
    out_dir = workspace / subdir
    if out_dir.is_symlink():
        raise ValueError(i18n.pick_now(
            f"\"{out_dir}\" is a symlink, so the {what} was not saved. Runs keep their output in a "
            "real directory inside the workspace.",
            f"「{out_dir}」是一个符号链接,所以{what_zh}没有保存。产物必须放在工作目录里的真实目录中。",
        ))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not _inside(root, out_dir):
        raise ValueError(i18n.pick_now(
            f"\"{out_dir}\" resolves outside the workspace, so the {what} was not saved.",
            f"「{out_dir}」解析后在工作目录之外,{what_zh}没有保存。",
        ))
    # Opened once and used for every operation below: opening a symlink with O_NOFOLLOW fails,
    # which is also the re-check of the two tests above at the moment that matters.
    try:
        dir_fd = os.open(out_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    except OSError as e:
        raise ValueError(i18n.pick_now(
            f"\"{out_dir}\" could not be opened for writing ({e}), so the {what} was not saved.",
            f"无法打开「{out_dir}」写入({e}),{what_zh}没有保存。",
        )) from None
    try:
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(stem_source) or slug(fallback) or 'output'}"
        staging = f".{stem}.{os.getpid()}.part"
        try:
            fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
                         dir_fd=dir_fd)
        except OSError as e:
            raise ValueError(i18n.pick_now(
                f"Could not create the staging file in {out_dir}: {e}",
                f"无法在 {out_dir} 里创建临时文件:{e}",
            )) from None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            for n in itertools.count(1):
                name = f"{stem}{ext}" if n == 1 else f"{stem}-{n}{ext}"
                try:
                    os.link(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except FileExistsError:
                    continue
                return out_dir / name
        finally:
            try:
                os.unlink(staging, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(dir_fd)
