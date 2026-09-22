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

from . import i18n
from .coderun import inside as _inside

# Every provider kind that produces media instead of holding a conversation. This is the
# list `store.list_models()` filters by, so a member can never be pointed at a generator.
# Each generator imports its own subset for picking a provider (`video.KINDS`,
# `imagegen.KINDS`) — add the new kind here **and** to that subset.
MEDIA_KINDS: tuple[str, ...] = ("minimax_video", "openai_image")

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
PURPOSES: tuple[str, ...] = ("chat", "image", "video", "responses")

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
