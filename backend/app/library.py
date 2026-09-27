"""Document library: chunks documents and indexes them so members in a group chat can
search them through the library_search tool.

Only the extracted text is kept (the original file is not). Retrieval is local BM25
(Chinese is tokenized into bigrams) and calls no cloud endpoint. Supports txt / md /
csv / json / html / pdf (with a text layer) / docx; scanned PDFs need OCR and are not
supported yet.
"""

from __future__ import annotations

from . import embed, i18n

import io
import json
import os
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import unquote, urlparse

import httpx

from .store import Store
from .textindex import BM25, chunk_text, join_chunks, rrf, tokenize

# numpy is optional in this environment (see embed.py): without it the keyword half of the
# search still works, so the import is guarded rather than required.
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

MAX_BYTES = 30 * 1024 * 1024
MAX_CHARS = 3_000_000
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml", ".xml", ".ini",
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".sql", ".sh", ".srt", ".vtt"}


class LibraryError(Exception):
    pass


def _check_text(text: str) -> str:
    """The checks every incoming text has to pass, extracted so a caller that must not lose an
    existing document can run them *before* replacing it (see `add_dir`)."""
    text = text.strip()
    if not text:
        raise LibraryError(i18n.pick_now("There is no usable text content", "没有可用的文本内容"))
    if len(text) > MAX_CHARS:
        raise LibraryError(i18n.pick_now(f"The text is too long (limit {MAX_CHARS} characters); split it before importing", f"文本太长(上限 {MAX_CHARS // 10000} 万字),请拆分后再导入"))
    return text


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):  # type: ignore[no-untyped-def]
        if not self.skip:
            self.parts.append(data)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Returns (kind, text)."""
    ext = Path(filename).suffix.lower()
    if len(data) > MAX_BYTES:
        raise LibraryError(i18n.pick_now(f"File is too large (limit {MAX_BYTES // 1024 // 1024} MB)", f"文件太大(上限 {MAX_BYTES // 1024 // 1024} MB)"))
    if ext in TEXT_EXT or not ext:
        text = _decode(data)
        if ext == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
            except ValueError:
                pass
        return (ext.lstrip(".") or "txt"), text
    if ext in (".html", ".htm"):
        p = _Text()
        p.feed(_decode(data))
        return "html", re.sub(r"\n\s*\n+", "\n\n", "".join(p.parts))
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise LibraryError(i18n.pick_now("Reading PDF files needs pypdf: pip install pypdf", "读取 PDF 需要安装 pypdf:pip install pypdf")) from None
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages]
        except Exception as e:  # noqa: BLE001
            raise LibraryError(i18n.pick_now(f"Could not parse the PDF: {e}", f"PDF 解析失败:{e}")) from None
        text = "\n\n".join(pages).strip()
        if not text:
            raise LibraryError(i18n.pick_now("This PDF has no extractable text (it may be a scan; OCR is not supported yet)", "这个 PDF 没有可提取的文字(可能是扫描件,暂不支持 OCR)"))
        return "pdf", text
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise LibraryError(i18n.pick_now("Reading docx files needs python-docx: pip install python-docx", "读取 docx 需要安装 python-docx:pip install python-docx")) from None
        try:
            d = docx.Document(io.BytesIO(data))
        except Exception as e:  # noqa: BLE001
            raise LibraryError(i18n.pick_now(f"Could not parse the docx: {e}", f"docx 解析失败:{e}")) from None
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "docx", "\n".join(parts)
    if ext == ".xlsx":
        return "xlsx", _xlsx_text(data)
    if ext == ".pptx":
        return "pptx", _pptx_text(data)
    if ext in (".xls", ".ppt"):
        raise LibraryError(i18n.pick_now(
            f"The old binary format ({ext}) cannot be read; save it as {ext}x (or csv) first",
            f"旧版二进制格式({ext})读不了,请先另存为 {ext}x(或 csv)格式"))
    raise LibraryError(i18n.pick_now(f"{ext} files are not supported yet (convert to txt / md / pdf / docx / xlsx / pptx first)", f"暂不支持 {ext} 格式(可以先转成 txt / md / pdf / docx / xlsx / pptx)"))


def _xlsx_text(data: bytes) -> str:
    """Every sheet as rows. Capped per sheet: a workbook can hold a million rows, and the model
    only ever sees a clipped prefix anyway — better a readable first page than a 30MB string."""
    try:
        import openpyxl
    except ImportError:
        raise LibraryError(i18n.pick_now("Reading xlsx files needs openpyxl: pip install openpyxl", "读取 xlsx 需要安装 openpyxl:pip install openpyxl")) from None
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise LibraryError(i18n.pick_now(f"Could not parse the workbook: {e}", f"表格解析失败:{e}")) from None
    out: list[str] = []
    try:
        for sheet in book.worksheets:
            out.append(f"## {sheet.title}")
            rows = 0
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    out.append(" | ".join(cells))
                    rows += 1
                if rows >= XLSX_MAX_ROWS:
                    out.append(i18n.pick_now(f"(first {XLSX_MAX_ROWS} rows only)", f"(只取了前 {XLSX_MAX_ROWS} 行)"))
                    break
    finally:
        book.close()
    text = "\n".join(out).strip()
    if not text:
        raise LibraryError(i18n.pick_now("This workbook has no readable cells", "这个表格里没有可读的内容"))
    return text


def _pptx_text(data: bytes) -> str:
    """Each slide's body text, then its tables. Speaker notes are included: they often carry the
    actual argument when the slide itself is one line."""
    try:
        from pptx import Presentation
    except ImportError:
        raise LibraryError(i18n.pick_now("Reading pptx files needs python-pptx: pip install python-pptx", "读取 pptx 需要安装 python-pptx:pip install python-pptx")) from None
    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise LibraryError(i18n.pick_now(f"Could not parse the presentation: {e}", f"演示文稿解析失败:{e}")) from None
    out: list[str] = []
    for i, slide in enumerate(deck.slides, 1):
        out.append(f"## {i}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    out.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append(i18n.pick_now("(notes) ", "(备注) ") + slide.notes_slide.notes_text_frame.text.strip())
    text = "\n".join(out).strip()
    if not text:
        raise LibraryError(i18n.pick_now("This presentation has no readable text", "这个演示文稿里没有可读的文字"))
    return text


XLSX_MAX_ROWS = 500
DIR_EXT = TEXT_EXT | {".html", ".htm", ".pdf", ".docx", ".xlsx", ".pptx"}
MAX_DIR_FILES = 300

# ------------------------------------------------------------------ where material came from
# How a document got into the library. A **closed vocabulary of tokens**, not a free string,
# because this is a fact about the row rather than a label somebody chose: it is derived from the
# row itself and can therefore be trusted when it is used to *group* things. One base holding two
# kinds of material is exactly what a user needs to be told, and it can only be told if "kind of
# material" is a value and not a sentence.
#
#   import      a folder on this machine, imported by the user
#   capture     material the app fetched for itself and stored under its own data directory
#   upload      a file that came in through a form (no path: a bare name)
#   link        a page, fetched from a URL
#   attachment  read out of a group's attachment (`attachment:<id>`)
#   workspace   a document sitting in a group's workspace (`workspace:<rel path>`)
#   written     typed or composed in the app, with no file behind it
ORIGINS = ("import", "capture", "upload", "link", "attachment", "workspace", "written")
MIXED = "mixed"                    # only a knowledge base can be this: it is not one of the above

# What a knowledge base is *for*. These are **suggestions, not a closed vocabulary** — unlike
# `ORIGINS`, which is a fact about a row, a purpose is a decision, and the decisions a person makes
# about their own shelf are not ours to enumerate. The list exists so the field opens with something
# to pick and a group's own base files itself under "project" without being asked; anything typed in
# its place is stored as typed and grouped as typed.
#
#   project    the material a project (a group chat) works from
#   reference  background to look things up in
#   method     how to do something: technique, procedure, craft
#   data       tables, figures, measurements
#   writing    material to quote, reuse or write from
PURPOSES = ("project", "reference", "method", "data", "writing")

# What a piece of fetched skill material is *for*. Like `ORIGINS` this is closed and stored, because
# it is derived from the material's own words rather than chosen by the user — and a label the user
# cannot see the basis of is a label they will not trust.
#
# This exists because a hundred-odd skills fetched off a platform are, to a reader, one grey pile:
# they are named "电影级长镜头", "AI演员微表情导演", "一图成片-电影广告全能导演" — every one of them
# says what it is for, and none of that was a field. Grouped, they are twelve short lists.
#
#   camera      镜头怎么动:运镜、长镜头、景别、机位
#   storyboard  分镜、故事板、镜头脚本
#   performance 表演与微表情
#   director    导演风格、大师风格、布光
#   commercial  广告、TVC、带货、营销
#   trailer     预告片、片头、混剪
#   effect      特效、转场、变身
#   character   角色、数字人、口播、人像
#   story       剧情、短剧、故事开发
#   edit        剪辑、字幕、配音、成片
#   design      平面与设计:海报、logo、版式
#   visual      影像风格与美学:电影感、质感、氛围
CATEGORIES = ("camera", "storyboard", "performance", "director", "commercial", "trailer",
              "effect", "character", "story", "edit", "design", "visual")

# Keyword → category, **in priority order**: the first hit wins, and the order is the whole
# specification. It is load-bearing in two places, both measured on the 114 skills fetched here:
#   * 电影级长镜头运镜 must land in `camera`, not in `visual` — so camera is tested before the
#     "电影感/电影级" bucket, which would otherwise swallow every cinematic skill there is;
#   * 微表情导演 must land in `performance`, not in `director` — the distinguishing word is 微表情.
_CATEGORY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("storyboard", ("分镜", "故事板", "镜头脚本", "storyboard")),
    ("performance", ("微表情", "表情", "演技", "演员", "表演", "expression")),
    # The platform's own tags are English tokens (`commercial_ads`, `ecommerce_updated`,
    # `film_short`, `graphic_design`), so both spellings are listed. Only seven of the 114 skills
    # carried a tag at all — the names carry the rest.
    ("commercial", ("广告", "TVC", "带货", "营销", "宣传", "电商", "种草", "直播",
                    "commercial", "ecommerce")),
    ("trailer", ("预告片", "预告", "片头", "混剪", "trailer")),
    ("camera", ("运镜", "镜头", "长镜头", "景别", "机位", "推拉", "摇移", "跟拍", "航拍",
                "一镜到底", "手持", "camera")),
    ("director", ("名导", "导演", "大师", "风格", "光影", "布光", "director")),
    ("character", ("数字人", "口播", "人像", "角色", "拟人", "换脸", "character")),
    ("effect", ("特效", "转场", "法宝", "魔法", "变身", "光效", "vfx", "effect")),
    ("story", ("剧情", "短剧", "故事", "剧本", "叙事", "连续剧", "film_short")),
    ("edit", ("剪辑", "字幕", "配音", "成片", "卡点", "edit")),
    ("design", ("设计", "海报", "logo", "平面", "拼贴", "图形", "图标", "graphic_design")),
    ("visual", ("电影感", "电影级", "美学", "质感", "唯美", "氛围", "油彩", "油画")),
)

# How much of a body to read before deciding. The name and the one-line description settle 111 of
# the 114 skills here; the body is only a fallback for the handful whose name says nothing
# ("惊喜镜头"), and reading all of it would let a passing mention ("把分镜表渲染成视频") decide the
# label for a skill that is about something else entirely.
CATEGORY_TEXT_CHARS = 3000


def tags_in(text: str) -> list[str]:
    """The `tags: [a, b]` line a fetched note carries in its own header, if any.

    Seven of the 114 skills fetched here carried one — the platform's own category tokens
    (`commercial_ads`, `film_short`, `graphic_design`). They are read back out of the note rather
    than passed around separately, so a re-run of the ingest produces the same category as the
    backfill over what it wrote last time.
    """
    out: list[str] = []
    for m in re.finditer(r"^\s*tags:\s*\[([^\]]*)\]", str(text or ""), re.M):
        out += [t.strip() for t in m.group(1).split(",") if t.strip()]
    return out


def category_of(title: str, text: str = "", tags: "Sequence[str] | tuple[str, ...]" = ()) -> str:
    """Which of `CATEGORIES` this material is for, or `""` — "nothing in its own words says".

    Read in three passes, in order of how much they mean: **the name** (what the author called it),
    then **the platform's own tags**, then **the opening of the body**. The first pass that says
    anything wins, so a name is never overruled by a phrase half way down its instructions.

    `""` is a real answer and is not the same as "other": the classifier only ever labels material
    whose own words name its function. A medical guideline does not, and getting `design` because its
    title happens to contain 试验设计 is exactly the kind of confident wrong label this avoids.
    """
    haystacks = [str(title or "")]
    if tags:
        haystacks.append(" ".join(str(t) for t in tags))
    if text:
        haystacks.append(str(text)[:CATEGORY_TEXT_CHARS])
    for hay in haystacks:
        if not hay.strip():
            continue
        for cat, words in _CATEGORY_WORDS:
            if any(w.lower() in hay.lower() for w in words):
                return cat
    return ""


def origin_of(filename: str, kind: str = "", *, data_dir: "Path | None" = None) -> str:
    """Which of `ORIGINS` a document came from, read off the row rather than asked for.

    `filename` is the load-bearing field, and every form it takes means exactly one thing:

    * `attachment:<id>` / `workspace:<rel>` are keys this app writes itself;
    * a URL, or the `link` kind, is a page;
    * **an absolute path is a folder that was imported** — and a path *inside the app's own data
      directory* is material the app fetched and stored itself (the ingest scripts write there),
      which is a different promise from a folder the user pointed at;
    * a bare name is a file that came in through a form;
    * nothing at all is something typed in here.

    Deliberately one function: a second copy of these rules would drift, and the label would start
    disagreeing with the row it describes.
    """
    name = str(filename or "").strip()
    if name.startswith("attachment:"):
        return "attachment"
    if name.startswith("workspace:"):
        return "workspace"
    if not name:
        return "written"
    if kind == "link":
        return "link"
    if name.startswith(("http://", "https://")):
        return "link"
    if os.path.isabs(name):
        if data_dir is not None:
            try:
                Path(name).resolve().relative_to(Path(data_dir).resolve())
                return "capture"
            except (ValueError, OSError):
                pass
        return "import"
    return "upload"


def shelf_of(origins: "list[str]") -> str:
    """The one word for a whole knowledge base's worth of origins.

    A base whose documents all came the same way *is* that word; one that mixes them is `mixed`, and
    saying so is the point — "this base is a folder you imported" and "this base is a folder you
    imported plus 114 notes fetched off a website" are different situations, and only one of them
    means somebody should probably split it.
    """
    seen = {o for o in origins if o}
    if not seen:
        return ""
    return next(iter(seen)) if len(seen) == 1 else MIXED
MAX_URL_BYTES = 5 * 1024 * 1024

# --------------------------------------------------------------- the pictures a note came with
# A note that came from an atlas, an article or somebody's case collection usually embeds its own
# figures, and those figures are the most valuable thing in it: they are already correct, already
# captioned, and already reviewed by somebody who knows the subject. The library used to index the
# words and throw that away, which is why a group could search 300 documents and still have nothing
# to put on screen.
FIGURE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
CLIP_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi"}
# `![[name|240]]` is Obsidian's embed; `![](path)` is the ordinary markdown one. Both appear in the
# wild, and a link can carry a width hint and an anchor after `|` or `#`.
EMBED_RE = re.compile(r"!\[\[([^\[\]]+?)\]\]|!\[[^\]]*\]\(([^)\s]+)\)")
MAX_FIGURES = 120
MAX_NOTE_BYTES = 400_000


def _clean_link(raw: str) -> str:
    link = str(raw or "").strip().strip("<>").strip('"').strip("'")
    link = link.split("|", 1)[0].split("#", 1)[0].strip()
    return unquote(link)


def _frontmatter(text: str) -> dict:
    """The `key: value` pairs at the top of a note, which is where the provenance lives."""
    out: dict[str, str] = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    for line in text[3:end if end > 0 else 0].splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip().strip('"').strip("'")
    return out


def _vault_root(start: Path) -> Path | None:
    """The folder Obsidian treats as the vault: the nearest one up that contains `.obsidian`.

    Found rather than assumed. Assuming it (`… + three parents`) is what let a note reach past the
    vault and into the folder above it — see the note in `note_figures`. `None` means "this is not
    inside a vault", and the caller then keeps the boundary as narrow as possible.
    """
    for p in [start, *start.parents]:
        if (p / ".obsidian").is_dir():
            return p
    return None


def note_figures(filename: str, *, want_clips: bool = True) -> tuple[list[dict], dict]:
    """(the media a note embeds, its front matter) — read from the note's own file on disk.

    Resolved rather than trusted: every link is refused unless it lands on a real file inside the
    note's own tree. A note is data from somewhere else, and the one thing this must never do is turn
    a line in it into a path this app will happily read from anywhere on the machine.

    `OBSIDIAN` links come in two shapes and both are here: a path relative to the vault root
    (`Atlas/shots/x.jpg`), and a bare file name (`some-case.mp4`) that sits next to the note. The
    vault root is found by walking up from the note, which is also the containment boundary.
    """
    src = Path(str(filename or ""))
    if src.suffix.lower() not in (TEXT_EXT | {".markdown"}) or not src.is_file():
        return [], {}
    try:
        raw = src.read_bytes()[:MAX_NOTE_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return [], {}
    base = src.parent
    # The search space, and — because the containment test below uses the same list — the boundary.
    # It runs from the note's own folder up to the vault root, and never above it.
    #
    # ⚠️ It used to be "the note's folder plus three parents", which made the boundary depend on how
    # deep a note happened to sit: for a note two levels down, the third parent is the folder
    # *above* the vault, so a line in the note could embed any picture in the user's Documents
    # folder. Nothing could exploit it while every `..` link was refused, so the two bugs hid each
    # other — fixing one is what made the other visible.
    #
    # A folder of notes that is not an Obsidian vault gets the note's own folder and no more: there
    # is no marker to say where such a collection ends, and a boundary that cannot be located is
    # not a boundary.
    vault = _vault_root(base)
    roots: list[Path] = [base]
    if vault is None:
        pass
    elif vault != base:
        for p in base.parents:
            roots.append(p)
            if p == vault:
                break
    allowed = FIGURE_EXT | (CLIP_EXT if want_clips else set())
    found: list[dict] = []
    seen: set[str] = set()
    for m in EMBED_RE.finditer(raw):
        link = _clean_link(m.group(1) or m.group(2) or "")
        # ⚠️ `..` is **not** refused here, and that is a fix rather than a loosening. In an
        # Obsidian vault a note in a subfolder reaches the vault's shared attachments folder as
        # `../attachments/x.png`, which is the normal shape — measured over one real vault, 4104 of
        # 4106 references were written that way and every one of them was being skipped in silence,
        # so a note's figures were reachable only when the note happened to sit at the vault root.
        #
        # What actually keeps a note from reading the machine is the containment test below: the
        # resolved path has to land inside the note's own tree (`roots`), and that test does not
        # care whether the link spelled `..` or not. Refusing the spelling as well was redundant
        # and it broke the common case. Absolute paths and URLs are still refused up front — those
        # are not a spelling question, they are a different kind of link.
        if not link or link.startswith(("http://", "https://", "/", "~", "file:")):
            continue
        tries = [r / link for r in roots]
        if "/" not in link:
            tries = [base / link] + tries
        for cand in tries:
            try:
                if not cand.is_file() or cand.suffix.lower() not in allowed:
                    continue
                real = cand.resolve()
            except OSError:
                continue
            if not any(real == r.resolve() or r.resolve() in real.parents for r in roots):
                continue
            if str(real) in seen:
                break
            seen.add(str(real))
            found.append({"name": cand.name, "path": str(real), "rel": link,
                          "kind": "clip" if cand.suffix.lower() in CLIP_EXT else "figure",
                          "bytes": cand.stat().st_size})
            break
        if len(found) >= MAX_FIGURES:
            break
    return found, _frontmatter(raw)


def fetch_url(url: str, timeout: float = 15.0) -> tuple[str, str, bytes]:
    """Download a web page or document -> (final url, content-type, content). Only http(s) is
allowed, at most 3 redirects, 5MB maximum."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise LibraryError(i18n.pick_now("A link has to start with http:// or https://", "链接需要以 http:// 或 https:// 开头"))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=3,
                          headers={"User-Agent": "Mozilla/5.0 TeamAgent"}) as c:
            with c.stream("GET", url.strip()) as r:
                r.raise_for_status()
                buf = bytearray()
                for part in r.iter_bytes():
                    buf += part
                    if len(buf) > MAX_URL_BYTES:
                        raise LibraryError(i18n.pick_now(f"The page is too large (over {MAX_URL_BYTES // 1024 // 1024} MB)", f"页面太大(超过 {MAX_URL_BYTES // 1024 // 1024} MB)"))
                return str(r.url), r.headers.get("content-type", "").split(";")[0].strip().lower(), bytes(buf)
    except httpx.HTTPStatusError as e:
        raise LibraryError(i18n.pick_now(f"The server answered {e.response.status_code}", f"对方返回了 {e.response.status_code}")) from None
    except httpx.HTTPError as e:
        raise LibraryError(i18n.pick_now(f"Could not open this link: {type(e).__name__}", f"打不开这个链接:{type(e).__name__}")) from None


# `watch_workspace` used to live here: the switch that decided how much of a group's own
# workspace was copied into its knowledge base. It is gone with the copying itself — see
# the note above `workspace_kb` for why nothing enters the library by itself any more.
class Library:
    def __init__(self, store: Store):
        self.store = store
        self._bm: BM25 | None = None
        self._chunks: list[dict] = []
        self._dirty = True
        # The vector half. `_vec_mat is None` means "not built yet"; an empty matrix means "built,
        # and there is nothing to search with" — the difference is what stops a search from
        # rebuilding the index on every call when a library simply has no vectors.
        self._vec_mat: "np.ndarray | None" = None
        self._vec_pos: list[int] = []
        self._vec_doc: list[str] = []
        self._vec_dim = 0
        self._vec_model = ""
        self._vec_note = ""
        # Retrieval runs in a thread pool (see toolhub / api_ext), so rebuilding the index must
# hold a lock:
        # otherwise two threads both see _dirty=True and each builds one, and a reader may get a
# _chunks and a _bm that are not from the same build.
        self._index_lock = threading.Lock()

    def invalidate(self) -> None:
        self._dirty = True
        self._vec_mat = None       # the passage vectors belong to the same build as `_chunks`
        self._vec_pos = []
        self._vec_doc = []

    def _ensure(self) -> None:
        if not self._dirty and self._bm is not None:
            return
        with self._index_lock:
            if not self._dirty and self._bm is not None:   # another thread already built it while we waited for the lock
                return
            chunks = self.store.all_chunks()
            bm = BM25([tokenize(c["title"] + " " + c["text"]) for c in chunks])
            self._chunks, self._bm, self._dirty = chunks, bm, False

    # ------------------------------------------------------------------ write
    def add_text(self, title: str, text: str, filename: str = "", kind: str = "note", size: int | None = None,
                 did: str | None = None, kb_id: str = "", origin: str = "", category: str = "") -> dict:
        # Refused rather than quietly stored somewhere: a document with no knowledge base is
        # invisible to every group, which is impossible to notice from the outside. Callers go
        # through `workspace_kb` / `shared_kb` (the API's `_kb_for_new_doc`) to pick one.
        if not kb_id:
            raise LibraryError(i18n.pick_now("A document needs a knowledge base", "文档必须归属某个知识库"))
        text = _check_text(text)
        chunks = chunk_text(text)
        # `origin` is passed in only when the caller knows better than the row does; normally it is
        # read off the row (see `origin_of`), so there is no second place to keep in step.
        came_from = origin or origin_of(filename, kind, data_dir=self.store.data_dir)
        # Fetched material gets a category from its own words (`category_of`), because that is the
        # material whose name states its function — a hundred skills off one platform are one grey
        # pile otherwise. Nothing else does: see `Store._classify_library_material`. An explicit
        # `category` wins, so an ingester that knows better can say so.
        cat = category or (category_of(title, text, tags_in(text)) if came_from == "capture" else "")
        doc = self.store.add_doc(title.strip() or filename or i18n.pick_now("Untitled", "未命名"), filename, kind,
                                 size if size is not None else len(text.encode()), chunks, did, kb_id,
                                 origin=came_from, category=cat)
        self.invalidate()
        return doc

    def add_file(self, filename: str, data: bytes, title: str | None = None, kb_id: str = "") -> dict:
        kind, text = extract_text(filename, data)
        return self.add_text(title or Path(filename).stem, text, filename, kind, len(data), kb_id=kb_id)

    def add_url(self, url: str, kb_id: str = "") -> dict:
        final, ctype, data = fetch_url(url)
        if ctype == "application/pdf" or final.lower().split("?")[0].endswith(".pdf"):
            kind, text = extract_text("x.pdf", data)
            title = Path(urlparse(final).path).stem or urlparse(final).netloc
        elif ctype in ("", "text/html", "application/xhtml+xml") or "html" in ctype:
            kind, text = extract_text("x.html", data)
            m = re.search(rb"<title[^>]*>(.*?)</title>", data[:200_000], re.S | re.I)
            title = re.sub(r"\s+", " ", _decode(m.group(1))).strip() if m else ""
            title = title or urlparse(final).netloc + urlparse(final).path
        elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            text = _decode(data)
            title = Path(urlparse(final).path).name or urlparse(final).netloc
        else:
            raise LibraryError(i18n.pick_now(f"This content type is not supported yet: {ctype or 'unknown'}", f"暂不支持这种内容类型:{ctype or '未知'}"))
        return self.add_text(title[:120], text, final, "link", len(data), kb_id=kb_id)

    def add_dir(self, path: str, recursive: bool = True, kb_id: str = "") -> dict:
        """Bulk import the documents in a folder into one knowledge base. Re-importing the same folder:
skipped, files whose size changed are replaced with the new version."""
        root = Path(path.strip()).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise LibraryError(i18n.pick_now("Give the full path of a folder that exists", "请填写一个存在的文件夹的完整路径"))
        root = root.resolve()
        # Only this group's own documents and the shared ones take part in "already imported":
        # the same file may legitimately live in two groups' libraries.
        by_name = {d["filename"]: d for d in self.store.list_docs(kb_id) if d["filename"]}
        added: list[dict] = []
        skipped: list[dict] = []
        seen = 0
        for p in sorted(root.rglob("*") if recursive else root.glob("*")):
            if p.is_symlink() or not p.is_file() or any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            if p.suffix.lower() not in DIR_EXT:
                continue
            seen += 1
            if seen > MAX_DIR_FILES:
                skipped.append({"name": "…", "reason": i18n.pick_now(f"Too many files; only the first {MAX_DIR_FILES} were processed",  # i18n-keep: placeholder row name; U+2026 is not Chinese
                                       f"文件太多,只处理前 {MAX_DIR_FILES} 个")})
                break
            rel = str(p.relative_to(root))
            try:
                size = p.stat().st_size
                old = by_name.get(str(p))
                if old and old["size"] == size:
                    skipped.append({"name": rel, "reason": i18n.pick_now("Already up to date", "已是最新")})
                    continue
                if size > MAX_BYTES:
                    raise LibraryError(i18n.pick_now(f"File is too large (limit {MAX_BYTES // 1024 // 1024} MB)", f"文件太大(上限 {MAX_BYTES // 1024 // 1024} MB)"))
                kind, text = extract_text(p.name, p.read_bytes())
                if not text.strip():
                    raise LibraryError(i18n.pick_now("There is no usable text content", "没有可用的文本内容"))   # check before touching the old version: if the new one is empty, keep the old
                if old:   # changed files: replaced in place, keeping document id, title and enabled state, so
# documents already selected in a group are not lost
                    # Every check that can still reject the new text has to run before the old row is
                    # deleted: `add_text` deletes nothing itself, so a rejection after `delete` would
                    # leave the document simply gone, with a "skipped" line as the only trace.
                    _check_text(text)
                    self.delete(old["id"])
                    doc = self.add_text(old["title"], text, str(p), kind, size, did=old["id"], kb_id=kb_id)
                    if not old["enabled"]:
                        doc = self.update(doc["id"], {"enabled": False}) or doc
                else:
                    doc = self.add_text(p.stem, text, str(p), kind, size, kb_id=kb_id)
                added.append(doc)
            except (LibraryError, OSError) as e:
                skipped.append({"name": rel, "reason": str(e)})
        return {"added": added, "skipped": skipped}

    def update(self, did: str, patch: dict) -> dict | None:
        doc = self.store.update_doc(did, patch)
        self.invalidate()
        return doc

    def delete(self, did: str) -> None:
        self.store.delete_doc(did)
        self.invalidate()

    # ------------------------------------------------------------------- read
    # Both retrievers are reduced to one entry per *document* before they are fused. A note averages
    # seventeen passages in a real vault, so a head of a few dozen passages is one or two notes: a
    # five-line answer made of five passages of the same page has answered once. Collapsing also
    # reaches further — a page whose best passage ranks 214th overall is around the thirtieth
    # document, which is inside any result list worth reading, while a head of twenty-four passages
    # never sees it at all.
    POOL = 3000       # passages examined per retriever before collapsing to documents
    DOCS = 200        # documents nominated per retriever; RRF only reads the head of a ranking

    def _ensure_vectors(self) -> bool:
        """Build the vector index, once, from the passages that have one. False when there are none.

        Built separately from the keyword index and on demand: a passage vector is 2 KB and a
        library of 60k passages would carry 120 MB of them on every write, while the keyword index
        is rebuilt whenever anything changes. Passages whose vector came from a *different* model
        are left out rather than mixed in — two models' vectors are not comparable, and a library
        that averaged them would answer confidently and wrongly.
        """
        if self._vec_mat is not None:
            return self._vec_mat.shape[0] > 0
        self._vec_mat = np.zeros((0, 0), dtype="float32")      # "asked already"; replaced below
        self._vec_pos = []
        self._vec_doc = []
        self._vec_note = ""
        if not embed.available():
            self._vec_note = embed.reason_missing(self.store.get_settings())
            return False
        want = embed.model_of(self.store.get_settings())
        rows = self.store.chunk_vectors()
        if not rows:
            return False
        pos = {(c["doc_id"], c["idx"]): i for i, c in enumerate(self._chunks)}
        cand: list[tuple[int, bytes]] = []
        other = 0
        for r in rows:
            if want and (r.get("embed_model") or "") not in ("", want):
                other += 1
                continue
            at = pos.get((r["doc_id"], r["idx"]))
            if at is None or not r.get("vec"):
                continue
            cand.append((at, r["vec"]))
        if not cand:
            self._vec_note = i18n.pick_now(
                f"{other} passage(s) carry vectors from another model; set the embedding model back, "
                "or re-index the library.", f"有 {other} 段的向量是另一个模型生成的;请把嵌入模型改回去,或者重新索引。") if other else ""
            return False
        dim = len(cand[0][1]) // 2
        mat, keep = embed.matrix([b for _at, b in cand], dim)
        self._vec_mat = mat
        self._vec_pos = [cand[i][0] for i in keep]
        self._vec_doc = [self._chunks[p]["doc_id"] for p in self._vec_pos]
        self._vec_dim = dim
        self._vec_model = want
        return mat.shape[0] > 0

    def vector_status(self) -> dict:
        """Everything a screen needs to say what vector search is doing, without running one."""
        cfg = self.store.get_settings()
        cov = self.store.vector_coverage()
        self._ensure()
        ok = self._ensure_vectors()
        return {**cov, "enabled": embed.enabled(cfg), "model": embed.model_of(cfg),
                "address": embed.base_url(cfg), "searchable": ok,
                "in_index": int(self._vec_mat.shape[0]) if self._vec_mat is not None else 0,
                "dim": int(getattr(self, "_vec_dim", 0)), "note": self._vec_note or "",
                "numpy": embed.available(), "venv": bool(embed.venv_python(cfg))}

    def search(self, query: str, top_k: int = 5, doc_ids: list[str] | None = None,
               query_vec: "np.ndarray | None" = None) -> list[dict]:
        """The most relevant *documents*, one passage each: keywords, and meaning when a vector is given.

        The two rankings are merged by rank (see `textindex.rrf`), so the **order** of the result is
        what to trust. `score` and `sim` are the two inputs to that order — a BM25 score and a
        cosine — and neither is comparable to the other; `via` says which retrievers found the
        document at all. Reading `score` as "how good this is" was only ever true while keywords
        were the only thing searching.

        `query_vec` is passed in rather than computed here on purpose: getting one is an HTTP call
        to the local model, and a synchronous search that quietly blocks a caller's event loop for
        it would be a worse bug than a search that used keywords alone.
        """
        self._ensure()
        assert self._bm is not None
        allowed = set(doc_ids) if doc_ids is not None else None
        kw_order: list[str] = []
        kw_best: dict[str, tuple[int, float]] = {}
        q = tokenize(query)
        if q and self._chunks:
            scored = [(i, s) for i, s in self._bm.scores(q).items()
                      if allowed is None or self._chunks[i]["doc_id"] in allowed]
            scored.sort(key=lambda x: (-x[1], x[0]))
            for i, s in scored[: self.POOL]:
                did = self._chunks[i]["doc_id"]
                if did not in kw_best:
                    kw_best[did] = (i, s)
                    kw_order.append(did)
        vec_order: list[str] = []
        vec_best: dict[str, tuple[int, float]] = {}
        if query_vec is not None and self._ensure_vectors():
            # `best_per_document` filters by *passage position*, while a caller's scope is a set of
            # document ids. Translating here — over the rows that carry a vector — is what keeps the
            # filter exact; passing the ids straight through made every scoped search find nothing
            # through this half, silently, which is the one outcome nobody would notice.
            allow_pos = None
            if allowed is not None:
                allow_pos = {p for p in self._vec_pos if self._chunks[p]["doc_id"] in allowed}
            for did, at, sim in embed.best_per_document(
                    query_vec, self._vec_mat, self._vec_pos, self._vec_doc,
                    pool=self.POOL, allow=allow_pos):
                vec_order.append(did)
                vec_best[did] = (at, sim)
        if kw_order and vec_order:
            order = [d for d, _ in rrf([kw_order[: self.DOCS], vec_order[: self.DOCS]])][:top_k]
        elif vec_order:
            order = vec_order[:top_k]
        else:
            order = kw_order[:top_k]
        out = []
        for did in order:
            in_kw, in_vec = did in kw_best, did in vec_best
            at, score = kw_best.get(did, (None, 0.0))
            sim = vec_best.get(did, (None, 0.0))[1]
            if in_kw and in_vec:
                # Show the passage the retriever that ranked this document higher thought was best:
                # when the words matched it is usually the passage with the terms in it, and when the
                # meaning matched it is the one about the topic.
                at = vec_best[did][0] if vec_order.index(did) < kw_order.index(did) else kw_best[did][0]
            elif in_vec:
                at = vec_best[did][0]
            c = self._chunks[at]
            out.append({"doc_id": did, "title": c["title"], "idx": c["idx"],
                        "text": c["text"], "score": round(score, 3), "sim": round(sim, 3),
                        "via": "both" if (in_kw and in_vec) else ("vector" if in_vec else "keyword")})
        return out

    async def index_vectors(self, doc_ids: list[str] | None = None, *, page: int = 128,
                            progress: "Callable[[int, int], None] | None" = None,
                            max_chunks: int = 0) -> dict:
        """Give every passage that lacks a vector one, in pages, until there are none left.

        Written as a loop over pages rather than one big batch because this is the step that can
        legitimately take an hour: the caller gets progress, a crash keeps everything already
        indexed, and re-running resumes at the first passage that still has no vector.

        Nothing is written for a page the model failed on — a half-indexed passage would be a
        passage that is silently unsearchable by meaning while looking indexed from the outside.
        """
        model = embed.model_of(self.store.get_settings())
        started = time.time()
        indexed = 0
        docs: set[str] = set()
        fails: list[str] = []
        before = self.store.vector_coverage()
        while True:
            if max_chunks and indexed >= max_chunks:
                break
            take = page if not max_chunks else min(page, max_chunks - indexed)
            rows = self.store.chunks_without_vectors(doc_ids, take)
            if not rows:
                break
            try:
                vecs = await embed.embed_texts(self.store.get_settings(), [r["text"] for r in rows])
            except embed.EmbedError as e:
                fails.append(str(e))
                break
            packed = embed.pack(vecs)
            by_doc: dict[str, dict[int, bytes]] = {}
            for r, blob in zip(rows, packed):
                by_doc.setdefault(r["doc_id"], {})[int(r["idx"])] = blob
            for did, blobs in by_doc.items():
                self.store.set_chunk_vectors(did, blobs, model)
                docs.add(did)
            indexed += len(rows)
            if progress is not None:
                progress(indexed, before["missing"])
        self.invalidate()          # the cached matrix (if any) is now behind the database
        return {"indexed": indexed, "documents": len(docs), "model": model,
                "seconds": round(time.time() - started, 1), "errors": fails,
                "left": max(0, self.store.vector_coverage()["missing"])}

    def read(self, did: str, start: int = 0, limit: int = 3000) -> dict:
        doc = self.store.get_doc(did)
        if not doc:
            raise LibraryError(i18n.pick_now("That document does not exist", "文档不存在"))
        text = join_chunks([c["text"] for c in self.store.doc_chunks(did)])
        return {"doc": doc, "start": start, "end": min(start + limit, len(text)),
                "total": len(text), "text": text[start:start + limit]}

    def find_by_title(self, key: str) -> dict | None:
        key = key.strip().lower()
        docs = self.store.list_docs()
        return next((d for d in docs if d["id"] == key or d["title"].lower() == key), None) or \
            next((d for d in docs if key and key in d["title"].lower()), None)

    # ------------------------------------------------------- the pictures a document came with
    def figures(self, doc_or_id: str | dict) -> tuple[list[dict], dict]:
        """(the pictures and clips this document embeds, its provenance).

        The bridge that was missing: a group can search a library, read a document's words, and — with
        this — also reach the figures that came with it. Everything a caller needs to use one is here:
        the name to refer to it by, the file it really is, and the `source` the note recorded, which
        is what a credit line is built from.

        Read from disk each time rather than indexed: a note is a few kilobytes of text, the answer is
        never stale, and it needs no schema change — the note's own file has been the source of truth
        since it was imported.
        """
        doc = self.store.get_doc(doc_or_id) if isinstance(doc_or_id, str) else doc_or_id
        if not doc:
            return [], {}
        return note_figures(str(doc.get("filename") or ""))

    def find_figure(self, doc: dict, want: str) -> dict | None:
        """One figure of a document by name, by number, or by part of its name.

        A model cannot quote a 60-character Obsidian path reliably, and it should not have to: the
        listing gives it names, and "the 3rd one" is as good a way to say it as any.
        """
        figs, _ = self.figures(doc)
        key = str(want or "").strip().lower()
        if not key:
            return None
        if key.isdigit():
            i = int(key) - 1
            return figs[i] if 0 <= i < len(figs) else None
        for f in figs:
            if f["name"].lower() == key or f["rel"].lower() == key:
                return f
        for f in figs:
            if key in f["name"].lower():
                return f
        return None

    # ------------------------------------------------------- knowledge bases and scope
    def own_kbs(self, gid: str) -> list[dict]:
        """The knowledge bases that belong to a group's workspace."""
        return [k for k in self.store.list_kbs() if k["group_id"] == gid] if gid else []

    def visible_kbs(self, gid: str) -> list[dict]:
        """Everything a group may reach: its workspace's knowledge bases plus every shared one.
        Attaching is a separate, narrower decision — see `scope_kbs`."""
        return self.store.list_kbs(gid or "")

    def scope_kbs(self, ext_library: dict, gid: str = "") -> list[dict]:
        """Which knowledge bases a group actually searches.

        * `off`      -- none, whatever is attached
        * `all`      -- everything it can reach (its workspace's own + the shared ones)
        * `selected` -- the knowledge bases it listed plus the members of the collections it
                        listed, filtered through the same visibility rule. A collection is a
                        convenience for the user, **not** a way around the boundary: one that
                        happens to contain another workspace's knowledge base does not hand it
                        over.
        """
        mode = (ext_library or {}).get("mode", "all")
        if mode == "off":
            return []
        visible = {k["id"]: k for k in self.visible_kbs(gid)}
        if mode != "selected":
            return list(visible.values())
        wanted = [str(x) for x in (ext_library.get("kb_ids") or [])]
        members = self.store.collection_kb_ids()
        for cid in ext_library.get("collection_ids") or []:
            wanted += members.get(str(cid), [])
        out, seen = [], set()
        for kid in wanted:
            if kid in visible and kid not in seen:
                seen.add(kid)
                out.append(visible[kid])
        return out

    def scope_docs(self, ext_library: dict, gid: str = "") -> list[dict]:
        """The documents a group may search, with their titles.

        `scope_ids` is this without the rows; both go through `scope_kbs`, so the boundary is decided
        in one place. The rows are what something needs when it has to say *what* a group can reach —
        a search that came back empty, or a member asking what is in there.
        """
        kbs = self.scope_kbs(ext_library, gid)
        if not kbs:
            return []
        return [d for d in self.store.list_docs(kb_ids=[k["id"] for k in kbs]) if d["enabled"]]

    def scope_ids(self, ext_library: dict, gid: str = "") -> list[str]:
        """The document ids a group may search.

        Always an explicit list — never None. `library_read` and `find_by_title` treat None as
        "no restriction", so returning it here would let a member open any document in the
        database by title.
        """
        return [d["id"] for d in self.scope_docs(ext_library, gid)]

    def workspace_kb(self, gid: str, create: bool = True) -> dict | None:
        """The knowledge base a new document goes into when it is added from a group's library.

        Created on first use and owned by that workspace, so a document added in a group never
        lands somewhere another group can read.
        """
        for kb in self.own_kbs(gid):
            return kb
        group = self.store.get_group(gid) if (create and gid) else None
        return self.store.add_kb(group["name"], "This group's own documents", gid) if group else None

    def own_kb_size(self, gid: str) -> int:
        """How many documents this group's own knowledge base holds (0 if it has none yet)."""
        kb = self.workspace_kb(gid, create=False)
        return len(self.store.list_docs(kb["id"])) if kb else 0

    def shared_kb(self, create: bool = True) -> dict | None:
        """The knowledge base new material goes into when no group is involved."""
        for kb in self.store.list_kbs(""):
            return kb
        return self.store.add_kb("Shared knowledge base", "Documents every group can search", "") if create else None

    # ------------------------------------------------- a group's own material stays in the group
    # This used to be the join that made a group's own material searchable: before every turn, the
    # text already read out of its attachments and the documents sitting in its workspace were copied
    # into a knowledge base, and every group was handed a base the moment it was created. Both are
    # gone, deliberately.
    #
    # The reason is that a library is a shelf somebody decided to build, and a project's working files
    # are not that. Every project dumping its workspace into one list made the list unreadable long
    # before it made it useful — this app's own database reached sixty-four bases, sixty of them
    # empty, for ten projects — and it put a half-finished draft on the same footing as material a
    # user had deliberately collected. A group's members can already read the group's own attachments
    # and files directly; what they *cannot* do is read a document the user chose to file, and that is
    # the only thing a knowledge base should be.
    #
    # So: nothing enters the library by itself. Either a user put it there, or it is not in there.
    # Uploading from a group's own library page still works and still creates that group's base on
    # first use (`workspace_kb`) — the decision was about *automatic* entry, not about the feature.
