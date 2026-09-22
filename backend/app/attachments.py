"""Files a user adds to a group: what kind it is, where it is kept, and what the model gets.

An attachment is any file — a screenshot, a PDF, a spreadsheet, a video, a zip. Three rules:

  * the kind is decided by the file's own bytes first (magic) and only by the extension where the
    container is genuinely shared (OOXML is just a zip, so `.docx` cannot be told from `.xlsx` by
    magic alone);
  * the bytes live inside the **group's workspace**, under `uploads/`, so a member can reach them
    with its ordinary file tools instead of treating the chat as an opaque box;
  * text is pulled out locally at upload time (`.text` on the row) — a PDF or a spreadsheet needs
    no vision model at all. Images and videos are the only kinds that need one, and their
    description is cached on the row so it is paid for once.

Writing here never follows a link: a member can create symlinks inside its own workspace, so a
name planted in advance must become an error rather than a file written through it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import i18n, library

IMAGE, VIDEO, AUDIO, DOCUMENT, OTHER = "image", "video", "audio", "document", "other"
VISUAL = (IMAGE, VIDEO)

# Magic bytes -> (mime, extension, kind). Only families that can be told apart by content: a zip
# is deliberately absent, and handled by extension below, because every OOXML file starts `PK`.
SIGNATURES: tuple[tuple[bytes, str, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png", IMAGE),
    (b"\xff\xd8\xff", "image/jpeg", "jpg", IMAGE),
    (b"GIF87a", "image/gif", "gif", IMAGE),
    (b"GIF89a", "image/gif", "gif", IMAGE),
    (b"BM", "image/bmp", "bmp", IMAGE),
    (b"%PDF-", "application/pdf", "pdf", DOCUMENT),
    (b"{\\rtf", "application/rtf", "rtf", DOCUMENT),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage", "doc", DOCUMENT),
    (b"\x1aE\xdf\xa3", "video/webm", "webm", VIDEO),
    (b"ID3", "audio/mpeg", "mp3", AUDIO),
    (b"fLaC", "audio/flac", "flac", AUDIO),
    (b"OggS", "audio/ogg", "ogg", AUDIO),
    (b"#!AMR", "audio/amr", "amr", AUDIO),
    (b"\x00\x00\x01\xba", "video/mpeg", "mpg", VIDEO),
)
# Containers whose first bytes are a length or a brand rather than a fixed signature.
FTYP_VIDEO = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "3gp": "video/3gpp",
              "avi": "video/x-msvideo", "mkv": "video/x-matroska"}
FTYP_AUDIO = {"m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".amr", ".wma", ".aiff", ".aif"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ts"}
# OOXML and other zip containers: the extension is the only way to tell them apart.
ZIP_KINDS: dict[str, tuple[str, str]] = {
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", DOCUMENT),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", DOCUMENT),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", DOCUMENT),
    ".odt": ("application/vnd.oasis.opendocument.text", DOCUMENT),
    ".ods": ("application/vnd.oasis.opendocument.spreadsheet", DOCUMENT),
    ".epub": ("application/epub+zip", DOCUMENT),
    ".zip": ("application/zip", OTHER),
}
ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
# Extensions we can read as text with no library at all.
PLAIN_EXT = library.TEXT_EXT | {".html", ".htm", ".csv"}
MAX_NAME = 60


# Where a media tool may live when the app was started by the Finder rather than from a shell.
# A GUI-launched app gets launchd's minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), which does not
# include the Homebrew prefixes — so "ffmpeg is installed" and "the app can find ffmpeg" are
# different facts, and video frames would silently never work.
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def tool(name: str) -> str | None:
    """A media binary by name, from PATH or a usual install prefix."""
    found = shutil.which(name)
    if found:
        return found
    for folder in TOOL_DIRS:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def human_size(n: int) -> str:
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


TEXT_MAX_CHARS = 200_000          # what an extracted document is allowed to occupy on the row
MAX_PER_MESSAGE = 20              # how many files one message may carry (was ten, images only)


def display_name(filename: str) -> str:
    """The label shown in the transcript. Cosmetic, but it must not drag a directory path or a
    control character into the UI — and it must not be empty, or the file has no name at all."""
    base = Path(filename or "").name.strip()
    return re.sub(r"[\x00-\x1f\x7f]", "", base)[:80] or "file"


def _looks_like_text(data: bytes) -> bool:
    """A short sample decides it: valid UTF-8 and no NULs. Everything else is treated as binary,
    which is the safe direction — a binary misread as text would put garbage in the prompt."""
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def classify(data: bytes, filename: str) -> tuple[str, str, str]:
    """(kind, mime, extension) from the bytes, falling back to the extension for zip containers."""
    suffix = Path(filename or "").suffix.lower()
    ext = suffix.lstrip(".")
    for sig, mime, e, kind in SIGNATURES:
        if data.startswith(sig):
            return kind, mime, e
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return IMAGE, "image/webp", "webp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return AUDIO, "audio/wav", "wav"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return VIDEO, "video/x-msvideo", "avi"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].decode("ascii", errors="replace").strip().lower()
        if brand.startswith("m4a") or brand.startswith("mp4a"):
            return AUDIO, FTYP_AUDIO["m4a"], "m4a"
        mime = FTYP_VIDEO.get(ext) or (FTYP_AUDIO.get(ext)) or "video/mp4"
        return (AUDIO if ext in (".m4a", ".aac") and mime in FTYP_AUDIO.values() else VIDEO), mime, ext or "mp4"
    if data.startswith(b"\xff\xfb") or data.startswith(b"\xff\xf3") or data.startswith(b"\xff\xf2"):
        return AUDIO, "audio/mpeg", "mp3"
    if data.startswith(b"PK\x03\x04"):
        # Every OOXML file starts with the same four bytes, so *here* — and only here — the name
        # decides: a spreadsheet and a zip are the same container.
        if suffix in ZIP_KINDS:
            mime, kind = ZIP_KINDS[suffix]
            return kind, mime, ext
        return OTHER, "application/zip", ext or "zip"
    if suffix in ARCHIVE_EXT:
        return OTHER, "application/octet-stream", ext
    if _looks_like_text(data):
        return DOCUMENT, _text_mime(ext), ext or "txt"
    return OTHER, "application/octet-stream", ext or "bin"


def kind_of_name(name: str) -> str:
    """The kind of a file already inside the workspace, from its name alone.

    Listing a workspace cannot read every file to decide what it is — a video there may be
    hundreds of megabytes. This is for the panel, so it only has to be useful, not authoritative.
    """
    ext = Path(name or "").suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".tiff", ".svg"}:
        return IMAGE
    if ext in VIDEO_EXT:
        return VIDEO
    if ext in AUDIO_EXT:
        return AUDIO
    if ext in PLAIN_EXT or ext in ZIP_KINDS or ext in (".pdf", ".docx", ".xlsx", ".pptx", ".rtf"):
        return DOCUMENT
    if ext in ARCHIVE_EXT:
        return OTHER
    return OTHER


def _text_mime(ext: str) -> str:
    if ext in ("html", "htm"):
        return "text/html"
    if ext in ("md", "markdown"):
        return "text/markdown"
    if ext == "csv":
        return "text/csv"
    if ext == "json":
        return "application/json"
    return "text/plain"


def check(data: bytes, settings: dict) -> str | None:
    """A problem description, or None when the file may be stored."""
    limit = int(settings.get("upload_max_mb") or 128) * 1024 * 1024
    if not data:
        return i18n.pick_now("The file is empty.", "文件是空的。")
    if len(data) > limit:
        return i18n.pick_now(
            f"That file is larger than {int(settings.get('upload_max_mb') or 128)} MB.",
            f"文件超过 {int(settings.get('upload_max_mb') or 128)} MB。")
    return None


# ------------------------------------------------------------------ where the bytes live
def safe_stem(name: str) -> str:
    """A readable, filesystem-safe stem. CJK is kept: the user's own filenames are the best label
    a member can be given, and hand-slugging them to `file-1` loses that."""
    base = Path(name or "").stem
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", base).strip("-._")
    return (cleaned or "file")[:MAX_NAME]


def uploads_dir(workspace: Path) -> Path:
    """`<workspace>/uploads`, created if needed and never through a link."""
    target = workspace / "uploads"
    if target.is_symlink():
        raise ValueError(f"uploads/ in the workspace is a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def save(workspace: Path, aid: str, filename: str, ext: str, data: bytes) -> str:
    """Write the upload into the workspace. Returns the path relative to the workspace.

    O_EXCL|O_NOFOLLOW for the same reason `coderun` uses it: a member can plant a link at a
    predictable name, and a plain `write_bytes` would then truncate whatever it points at — a file
    outside the workspace, written by the app itself.
    """
    uploads_dir(workspace)          # created here, on purpose: the write below must not be the thing that makes it
    rel = f"uploads/{safe_stem(filename)}-{aid}.{ext or 'bin'}"
    target = workspace / rel
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return rel


def resolve(workspace: Path, rel: str) -> Path | None:
    """A path inside the workspace, or None. Refuses `..`, absolute paths and links that escape."""
    if not rel or rel.startswith(("/", "~")):
        return None
    root = workspace.resolve()
    target = (workspace / rel)
    try:
        real = target.resolve()
    except OSError:
        return None
    if real != root and root not in real.parents:
        return None
    return target


def path_for_row(store, row: dict) -> Path | None:
    """Where this attachment's bytes are now.

    New rows carry a workspace-relative path; rows written before attachments moved into the
    workspace still sit in `<data dir>/attachments/<id>.<ext>` and are found by id.
    """
    rel = (row.get("rel_path") or "").strip()
    if rel:
        from . import coderun
        try:
            workspace = coderun.workspace_path(store.data_dir, store.get_settings(), row["group_id"])
        except ValueError:
            return None
        found = resolve(workspace, rel)
        return found if found and found.is_file() else None
    from . import images
    return images.find_file(store.data_dir, row["id"])


# ------------------------------------------------------------------ reading the content
def is_visual(kind: str) -> bool:
    return kind in VISUAL


def blocks_text(kind: str) -> bool:
    """Kinds the model must be *told about*; the rest are only ever a path and a size."""
    return kind in (IMAGE, VIDEO, AUDIO, OTHER)


def extract_text(filename: str, data: bytes) -> str:
    """Pull the text out of a document locally. Returns "" when this type has no text to give."""
    ext = Path(filename).suffix.lower()
    if ext in PLAIN_EXT or ext == "":
        try:
            return library._decode(data)                       # the shared decoder: utf-8 / gb18030
        except Exception:                                      # noqa: BLE001
            return ""
    try:
        _kind, text = library.extract_text(filename, data)
        return text
    except library.LibraryError:
        return ""


def probe(path: Path) -> dict:
    """Duration, size and codecs of a media file, via ffprobe. Empty when it is not available —
    the caller then says "no duration known" instead of inventing one."""
    exe = tool("ffprobe")
    if not exe:
        return {}
    try:
        out = subprocess.run(                                      # noqa: S603
            [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30, check=False,
        )
        data = json.loads(out.stdout or b"{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    got: dict = {}
    for key, value in (("duration", fmt.get("duration")), ("width", video.get("width")),
                       ("height", video.get("height")), ("vcodec", video.get("codec_name")),
                       ("acodec", audio.get("codec_name"))):
        if value not in (None, ""):
            got[key] = value
    try:
        got["duration"] = round(float(got.get("duration") or 0), 1)
    except (TypeError, ValueError):
        got.pop("duration", None)
    return got


def frames(path: Path, out_dir: Path, count: int) -> list[Path]:
    """Evenly spaced stills from a video, so a model that cannot watch it can still see it.

    Eleven minutes of talking heads yields the same handful of frames as eleven seconds of action:
    the point is the gist, and every frame is paid for again on the next question.
    """
    exe = tool("ffmpeg")
    if not exe or count < 1:
        return []
    if out_dir.is_symlink():
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(path)
    span = float(info.get("duration") or 0)
    step = max(1.0, span / count) if span else 1.0
    try:
        subprocess.run(                                          # noqa: S603
            [exe, "-v", "error", "-y", "-i", str(path),
             "-vf", f"fps=1/{step:.4f},scale='min(1024,iw)':-2",
             "-frames:v", str(count), "-q:v", "4", str(out_dir / "f%02d.jpg")],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(out_dir.glob("f*.jpg"))[:count]


def shrink_image(data: bytes, mime: str, limit_bytes: int) -> tuple[str, bytes]:
    """Downscale an oversized picture instead of refusing it.

    A phone photo is several MB and mostly pixels nobody needs; sending it whole buys nothing and
    costs the context window. Falls back to the original bytes when Pillow is unavailable or the
    data is not really an image.
    """
    if len(data) <= limit_bytes:
        return mime, data
    try:
        import io as _io

        from PIL import Image
    except ImportError:
        return mime, data
    try:
        with Image.open(_io.BytesIO(data)) as im:
            im = im.convert("RGB") if im.mode in ("RGBA", "P", "LA") else im
            longest = max(im.size)
            if longest > 1568:
                scale = 1568 / longest
                im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
            for quality in (85, 70, 55, 40):
                buf = _io.BytesIO()
                im.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= limit_bytes or quality == 40:
                    return "image/jpeg", buf.getvalue()
    except Exception:                                              # noqa: BLE001
        return mime, data
    return mime, data


def file_key(workspace: Path, path: Path) -> str:
    """A cache key that changes when the file does: path + size + modification time."""
    try:
        stat = path.stat()
        raw = f"{path.relative_to(workspace)}:{int(stat.st_mtime)}:{stat.st_size}"
    except (OSError, ValueError):
        raw = str(path)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]      # noqa: S324 — a cache key, not a signature


def cache_dir(workspace: Path) -> Path:
    """`.extract/` — the app's own notes about files in the workspace (skipped by the file list)."""
    target = workspace / ".extract"
    if target.is_symlink():
        raise ValueError(f".extract in the workspace is a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def cached(workspace: Path, key: str, suffix: str) -> str | None:
    try:
        found = cache_dir(workspace) / f"{key}.{suffix}"
    except (OSError, ValueError):
        return None
    try:
        return found.read_text(encoding="utf-8") if found.is_file() else None
    except OSError:
        return None


def remember(workspace: Path, key: str, suffix: str, text: str) -> None:
    """Write a cached extraction/description. Created exclusively: a member can plant a link at a
    predictable name inside its own workspace, and a plain write would follow it."""
    try:
        target = cache_dir(workspace) / f"{key}.{suffix}"
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except (OSError, ValueError):
        return
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def text_of_file(workspace: Path, path: Path) -> str:
    """The text of a file inside the workspace, extracted once and then remembered.

    A referenced file is usually one a member wrote a moment ago, so its extraction is cached
    under a key that includes its mtime — the next turn about the same file does not re-parse it.
    """
    key = file_key(workspace, path)
    hit = cached(workspace, key, "txt")
    if hit is not None:
        return hit
    if kind_of_name(path.name) not in (DOCUMENT,):
        return ""
    try:
        text = extract_text(path.name, path.read_bytes())[:TEXT_MAX_CHARS]
    except OSError:
        return ""
    remember(workspace, key, "txt", text)
    return text


def describe_prompt(name: str, kind: str) -> str:
    """What the vision model is asked. Kept short and literal: this text is what every member will
    read instead of the picture, so it has to be *content*, not a style note."""
    if kind == VIDEO:
        return i18n.pick_now(
            f"These are frames taken from the video \"{name}\". Describe what happens, who and what "
            "is visible, any on-screen text, and how the scene changes — in plain prose. If there "
            "is speech you can infer from visible captions, quote it. Do not speculate.",
            f"这些是从视频「{name}」里抽出的画面。请用平实的语言描述:发生了什么、看得见谁和什么、"
            "画面里的文字、场景如何变化。如果有可见字幕,照抄下来。不要推测。")
    return i18n.pick_now(
        f"Describe this image (\"{name}\") for someone who cannot see it: what it shows, any text "
        "in it copied exactly, numbers and labels. Plain prose, no preamble, no speculation.",
        f"为看不到这张图片的人描述图片「{name}」:画面内容、图上的文字(逐字照抄)、数字与标注。"
        "平实叙述,不要开场白,不要推测。")


def heading(row: dict) -> str:
    """One line naming a file, as the model should see it."""
    bits = [row.get("kind") or OTHER, human_size(int(row.get("bytes") or 0))]
    meta = row.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("duration"):
        bits.append(f"{meta['duration']}s")
    if meta.get("width") and meta.get("height"):
        bits.append(f"{meta['width']}x{meta['height']}")
    return f"{row.get('name') or row.get('id')} ({', '.join(str(b) for b in bits if b)})"
