"""Write a finished draft out as a real file: .docx, .pptx, .xlsx or .md.

Why this exists: four of the things this app is asked for — an office document, a slide deck, a
paper, a poster's text — end as *a file the user opens*, and until now the only way to make one was
`run_code` (off by default) with `python-docx` imported by hand. A group could talk for forty turns
about a deck that never existed, and the task board called it done because somebody had replied. The
missing piece was never eloquence; it was a place for the words to land.

The input is deliberately one shape for all four formats — a small Markdown-ish body — because a
model writing a deck and a model writing a report should not have to learn two languages, and
because keeping one parser here means the headings, lists and tables come out the same in Word as
they do in the slides.

Nothing is generated: this only lays out text that already exists. It cannot check whether the text
is right, and it cannot show the result to the member that asked for it — which is why every answer
says the file's size and path, and tells the caller not to describe a page it has not seen.
"""

from __future__ import annotations

from . import i18n

import importlib
import re
from pathlib import Path
from typing import Any

KINDS = ("docx", "pptx", "xlsx", "md")
SUFFIX = {"docx": ".docx", "pptx": ".pptx", "xlsx": ".xlsx", "md": ".md"}
# Two names for the same thing, and mixing them up is silent: the module you import (`docx`) is not
# the package you install (`python-docx`). `importlib.import_module("python-docx")` raises, so a
# first version of this file reported "python-docx is not installed" on a machine that had it.
_MODULE = {"docx": "docx", "pptx": "pptx", "xlsx": "openpyxl", "md": ""}
_PACKAGE = {"docx": "python-docx", "pptx": "python-pptx", "xlsx": "openpyxl", "md": ""}


class DocError(Exception):
    """Anything that stops a file from being written, phrased for the member that asked."""


def _installed(mod: str) -> bool:
    try:
        importlib.import_module(mod)
    except Exception:  # noqa: BLE001 — a missing optional library is not an error here
        return False
    return True


def formats() -> list[str]:
    """Which formats can be written on this machine, in a stable order. `md` always can."""
    return [k for k in KINDS if not _MODULE[k] or _installed(_MODULE[k])]


def available() -> tuple[bool, str]:
    """The tool is offered as long as it can produce something; a format that is missing its library
    is named by the tool's own answer, not by the group panel (a missing pip package is not a thing
    the user is expected to go and install, and `problems` is posted into the chat every turn)."""
    if formats():
        return True, ""
    return False, i18n.pick_now("No document writer is available on this machine.",
                                "这台机器上没有任何可用的文档写出器。")


# ------------------------------------------------------------------ parsing
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\d+[.)]\s+(.*)$")
_RULE = re.compile(r"^-{3,}$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$")


def _cells(line: str) -> list[str]:
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [c.strip() for c in raw.split("|")]


def parse(body: str) -> list[tuple[str, Any]]:
    """A Markdown-ish body into nodes: ("h", level, text) / ("p", text) / ("li", text, ordered) /
    ("table", rows). Deliberately small: headings, paragraphs, two kinds of list, pipe tables.
    Anything else is kept as a paragraph, so no text is ever silently dropped."""
    out: list[tuple[str, Any]] = []
    lines = str(body or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped or _RULE.match(stripped):
            i += 1
            continue
        head = _HEAD.match(stripped)
        if head:
            out.append(("h", len(head.group(1)), head.group(2).strip()))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            rows = [_cells(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append(("table", rows))
            continue
        bullet = _BULLET.match(stripped)
        if bullet:
            out.append(("li", bullet.group(1).strip(), False))
            i += 1
            continue
        number = _NUMBER.match(stripped)
        if number:
            out.append(("li", number.group(1).strip(), True))
            i += 1
            continue
        out.append(("p", stripped))
        i += 1
    return out


def _heading_sections(nodes: list[tuple[str, Any]], level: int = 2) -> list[tuple[str, list]]:
    """Group nodes into (title, nodes) by headings of at most `level`: what a deck's slides and a
    workbook's sheets are made of. Text before the first heading becomes a section with an empty
    title, and is never dropped."""
    sections: list[tuple[str, list]] = []
    current: tuple[str, list] = ("", [])
    for node in nodes:
        if node[0] == "h" and node[1] <= level:
            if current[1] or current[0]:
                sections.append(current)
            current = (node[2], [])
        else:
            current[1].append(node)
    if current[1] or current[0]:
        sections.append(current)
    return sections


# ------------------------------------------------------------------ writing
def resolve(workspace: Path, path: str, fmt: str) -> Path:
    """Where the file goes: inside the workspace, with the right suffix, never through a symlink.

    A member asking for `报告` and `docx` means `报告.docx` — the suffix is the format, so the caller
    does not have to remember to type it. A path that climbs out of the workspace, or a directory
    that is a symlink, is refused rather than followed: this tool writes files on the user's disk.
    """
    from .coderun import inside                    # re-exported to keep the containment rule in one place
    root = Path(workspace).resolve()
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise DocError(i18n.pick_now("Give a file name for the document, for example 报告.docx",
                                     "请给这份文档一个文件名,例如 报告.docx"))
    if raw.startswith("/") or raw.startswith("~") or re.match(r"^[A-Za-z]:", raw):
        raise DocError(i18n.pick_now(f"\"{raw}\" is an absolute path; write into the group's workspace "
                                     "with a relative name instead.",
                                     f"「{raw}」是绝对路径;请用相对名字写进本群工作目录。"))
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise DocError(i18n.pick_now(f"\"{raw}\" climbs out of the workspace.",
                                     f"「{raw}」会跑到工作目录外面去。"))
    name = parts[-1]
    stem = Path(name).stem or "document"
    rel = Path(*parts[:-1], stem + SUFFIX[fmt]) if parts[:-1] else Path(stem + SUFFIX[fmt])
    parent = root / rel.parent
    if any(p.is_symlink() for p in (parent, *parent.parents) if p != root.parent):
        raise DocError(i18n.pick_now("A folder on the way is a symlink, so nothing was written.",
                                     "路径上有符号链接,所以没有写。"))
    parent.mkdir(parents=True, exist_ok=True)
    dest = (root / rel).resolve() if not (root / rel).exists() else (root / rel)
    if not inside(root, parent / rel.name) or not inside(root, dest):
        raise DocError(i18n.pick_now("That path is outside this group's workspace.",
                                     "这个路径不在本群工作目录里。"))
    if dest.is_symlink():
        raise DocError(i18n.pick_now(f"\"{rel}\" is a symlink; refusing to write through it.",
                                     f"「{rel}」是符号链接;拒绝透过它写文件。"))
    return root / rel


def _docx(dest: Path, title: str, nodes: list[tuple[str, Any]]) -> int:
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    if title:
        doc.add_heading(title, level=0)
    for node in nodes:
        kind = node[0]
        if kind == "h":
            doc.add_heading(node[2], level=min(int(node[1]), 4))
        elif kind == "li":
            style = "List Number" if node[2] else "List Bullet"
            try:
                doc.add_paragraph(node[1], style=style)
            except KeyError:                       # a template without the list styles
                doc.add_paragraph(("• " if not node[2] else "- ") + node[1])
        elif kind == "table":
            rows = node[1]
            table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            table.style = "Table Grid"
            for y, row in enumerate(rows):
                for x, cell in enumerate(row):
                    table.cell(y, x).text = cell
        else:
            para = doc.add_paragraph(node[1])
            para.paragraph_format.space_after = Pt(6)
    doc.save(str(dest))
    return len(nodes)


def _pptx(dest: Path, title: str, nodes: list[tuple[str, Any]]) -> int:
    from pptx import Presentation
    from pptx.util import Inches
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)   # 16:9, what a projector is
    first = title or next((n[2] for n in nodes if n[0] == "h"), "")
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.text = first or i18n.pick_now("Untitled", "未命名")
    subtitle = next((n[1] for n in nodes if n[0] == "p"), "")
    if subtitle:
        cover.placeholders[1].text = subtitle
    slides = 0
    for head, body in _heading_sections(nodes):
        # A level-1 heading that is just the deck's title again would become a divider slide showing
        # what the cover already says; the cover stands in for it.
        if head and title and head == title:
            continue
        if not head and not body:
            continue
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = head or i18n.pick_now("Continued", "续表")
        frame = slide.placeholders[1].text_frame
        frame.clear()
        used = False
        for node in body:
            if node[0] == "table":
                rows, cols = len(node[1]), max(len(r) for r in node[1])
                shape = slide.shapes.add_table(rows, cols, Inches(1), Inches(2),
                                               deck.slide_width - Inches(2), Inches(0.4) * rows)
                for y, row in enumerate(node[1]):
                    for x, cell in enumerate(row):
                        shape.table.cell(y, x).text = cell
                used = True
                continue
            text = f"• {node[1]}" if node[0] == "li" else node[1]
            para = frame.paragraphs[0] if not used and not frame.paragraphs[0].text else frame.add_paragraph()
            para.text = text
            if node[0] == "li":
                para.level = 0
            used = True
        slides += 1
    deck.save(str(dest))
    return slides


def _xlsx(dest: Path, title: str, nodes: list[tuple[str, Any]]) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    book = Workbook()
    book.remove(book.active)
    sections = _heading_sections(nodes) or [(title or "Sheet1", [])]
    seen: set[str] = set()
    for head, body in sections:
        name = re.sub(r"[\\/*?:\[\]]", "_", head or title or "Sheet1")[:31] or "Sheet1"
        base, n = name, 2
        while name in seen:
            name = f"{base[:28]}-{n}"
            n += 1
        seen.add(name)
        sheet = book.create_sheet(name)
        if head and title and head != title:
            sheet.append([head])
            sheet["A1"].font = Font(bold=True)
        for node in body:
            if node[0] == "table":
                for row in node[1]:
                    sheet.append(row)
                sheet.append([])
            else:
                sheet.append([node[1]])
    book.save(str(dest))
    return len(seen)


def write(workspace: Path, path: str, fmt: str, title: str, body: str) -> tuple[Path, int]:
    """Write one file and say how much of it there is. Raises `DocError` with a readable reason."""
    fmt = str(fmt or "").strip().lower().lstrip(".")
    if fmt not in KINDS:
        raise DocError(i18n.pick_now(f"format must be one of {', '.join(KINDS)}; got \"{fmt}\".",
                                     f"format 只能是 {', '.join(KINDS)} 之一;收到的是「{fmt}」。"))
    ok = formats()
    if fmt not in ok:
        raise DocError(i18n.pick_now(
            f"Cannot write .{fmt} on this machine: {_PACKAGE[fmt]} is not installed. Available: "
            f"{', '.join(ok)}.", f"这台机器写不了 .{fmt}:没有安装 {_PACKAGE[fmt]}。可用的是:{', '.join(ok)}。"))
    dest = resolve(workspace, path, fmt)
    nodes = parse(body)
    title = str(title or "").strip()
    if fmt == "md":
        text = (f"# {title}\n\n" if title else "") + str(body or "").rstrip() + "\n"
        dest.write_text(text, encoding="utf-8")
        return dest, len(nodes)
    if not nodes and not title:
        raise DocError(i18n.pick_now("Nothing to write: the body is empty.",
                                     "没有内容可写:正文是空的。"))
    count = {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx}[fmt](dest, title, nodes)
    return dest, count
