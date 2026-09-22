"""Images a user attaches to a message.

Two rules matter here, and both are about not trusting the caller:

  * the type is decided by the file's own magic bytes, never by the name or the declared
    Content-Type — a `.png` that is really a zip, or a `text/html` body declared as an image,
    must not become an image;
  * the size is checked before anything is written to disk, so an oversized upload cannot
    fill the data directory first and be validated afterwards.

The bytes live in `<data dir>/attachments/<id>.<ext>`; the row in the `attachments` table
carries the metadata the UI and the prompt need.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from . import i18n

# Magic bytes -> (mime, extension). Formats an OpenAI-compatible endpoint accepts.
SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)
WEBP_HEAD = (b"RIFF", b"WEBP")


def sniff(data: bytes) -> tuple[str, str] | None:
    """(mime, extension) from the content itself, or None when it is not an image we accept."""
    for sig, mime, ext in SIGNATURES:
        if data.startswith(sig):
            return mime, ext
    if len(data) >= 12 and data[:4] == WEBP_HEAD[0] and data[8:12] == WEBP_HEAD[1]:
        return "image/webp", "webp"
    return None


def folder(data_dir: Path) -> Path:
    d = Path(data_dir) / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(data_dir: Path, aid: str, ext: str) -> Path:
    return folder(data_dir) / f"{aid}.{ext}"


def find_file(data_dir: Path, aid: str) -> Path | None:
    """The stored file for an id, whatever extension it ended up with."""
    return next(iter(folder(data_dir).glob(f"{aid}.*")), None)


def check(data: bytes, settings: dict) -> str | None:
    """Returns a problem description, or None when the upload may be stored."""
    limit = int(settings.get("vision_max_mb") or 8) * 1024 * 1024
    if not data:
        return i18n.pick_now("The file is empty.", "文件是空的。")
    if len(data) > limit:
        return i18n.pick_now(
            f"The image is larger than {int(settings.get('vision_max_mb') or 8)} MB.",
            f"图片超过 {int(settings.get('vision_max_mb') or 8)} MB。",
        )
    if sniff(data) is None:
        return i18n.pick_now("Only PNG, JPEG, GIF and WebP images are accepted.", "只接受 PNG、JPEG、GIF、WebP 格式的图片。")
    return None


def data_uri(data: bytes, mime: str) -> str:
    """Inline form for the model request; providers accept a data: URL in image_url."""
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def display_name(filename: str) -> str:
    """A label for the transcript. The stored file is named by id, so this is cosmetic —
    but it must still not drag a directory path or a control character into the UI."""
    base = Path(filename or "").name.strip()
    return re.sub(r"[\x00-\x1f\x7f]", "", base)[:80] or "image"


def read(data_dir: Path, meta: dict) -> tuple[str, bytes] | None:
    """(mime, bytes) for one descriptor from a message's `meta.images`."""
    aid = str(meta.get("id") or "")
    f = find_file(data_dir, aid) if aid else None
    if not f:
        return None
    try:
        return str(meta.get("mime") or "image/png"), f.read_bytes()
    except OSError:
        return None


def sweep(store, data_dir: Path, days: int = 7) -> int:
    """Drop uploads the user never sent. Returns how many files went.

    Files uploaded since attachments became general live in the group's workspace, older ones
    still under `<data dir>/attachments/`; both are asked for their real location.
    """
    from . import attachments

    gone = 0
    for row in store.stale_attachments(time.time() - days * 86400):
        f = attachments.path_for_row(store, row)
        if f:
            try:
                f.unlink()
                gone += 1
            except OSError:
                continue
        store.delete_attachment(row["id"])
    return gone
