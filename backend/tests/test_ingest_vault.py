"""The vault ingest's two judgement calls, pinned.

The script itself is run by hand, and its end-to-end behaviour has been exercised on 13 real books.
What these tests protect is the part that would otherwise fail *silently* on some future book:

* a book longer than the library's limit has to be cut on page boundaries, and the parts have to
  *cover* every page exactly once — an off-by-one there means a chapter that is missing from the
  index while its neighbours are present, which no search result can reveal;
* a PDF whose text layer extracts as static has to be recognised as such: it indexes and retrieves
  exactly as confidently as prose, so it can only be caught before it goes in.

The PDF fixture is written by hand (base-14 Helvetica needs no embedded font) rather than shipped as
a binary, so the test shows what it is testing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script():
    spec = importlib.util.spec_from_file_location("ingest_vault", ROOT / "scripts" / "ingest-vault.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


iv = load_script()


def make_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one text line per page, xref and all."""
    objs: list[bytes] = []
    kids = []
    for i in range(len(pages)):
        content = ("BT /F1 12 Tf 20 100 Td (" + pages[i].replace("(", "").replace(")", "") + ") Tj ET").encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    # objects: 1 catalog, 2 pages, 3 font, then 4.. per page object and its content
    first_page = 4
    page_obj_ids = []
    contents = []
    for i in range(len(pages)):
        page_obj_ids.append(first_page + i * 2)
        contents.append(first_page + i * 2 + 1)
    for pid in page_obj_ids:
        kids.append(b"%d 0 R" % pid)
    body: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i in range(len(pages)):
        body.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % contents[i])
        body.append(objs[i])
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(body, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % idx + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(body) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(body) + 1, xref_at)
    return bytes(out)


# ------------------------------------------------------------------ reading a PDF
def test_text_comes_out_page_by_page(tmp_path):
    p = tmp_path / "book.pdf"
    p.write_bytes(make_pdf(["Cerebral vasospasm after subarachnoid haemorrhage",
                            "Moyamoya disease and revascularisation"]))
    pages = iv.pdf_pages(p)
    assert len(pages) == 2
    assert "vasospasm" in pages[0] and "Moyamoya" in pages[1]


def test_a_pdf_with_no_text_layer_reads_as_empty_pages(tmp_path):
    """The path a scanned book takes: pages come back empty, and it is the *caller* that turns that
    into "this needs OCR" and writes it into the report — never into the library as a silent blank."""
    from pypdf import PdfWriter

    p = tmp_path / "scan.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with p.open("wb") as fh:
        w.write(fh)
    pages = iv.pdf_pages(p)
    assert len(pages) == 1 and pages[0] == ""


# ------------------------------------------------------------------ cutting a long book
def test_a_short_book_is_left_alone():
    pages = ["page one", "page two"]
    assert iv.split_pages(pages, 1000) == [(1, 2, "page one\n\npage two")]


def test_a_long_book_is_cut_on_page_boundaries_and_loses_no_page():
    pages = ["x" * 60] * 10
    parts = iv.split_pages(pages, 150)
    assert len(parts) > 1, "it should have been cut"
    assert parts[0][0] == 1 and parts[-1][1] == 10, "the range covers the book"
    for (a, b, text), nxt in zip(parts, parts[1:]):
        assert b + 1 == nxt[0], "no gap and no overlap between parts"
        assert len(text) <= 150 + 62, "a part is roughly within the limit"
    assert sum(p[1] - p[0] + 1 for p in parts) == 10, "every page is in exactly one part"


def test_a_single_page_longer_than_the_limit_still_gets_its_own_part():
    """A page cannot be split, so it must not be dropped either: the honest outcome is a part that
    exceeds the limit and is refused by the library with a reason, not a missing page."""
    pages = ["y" * 500]
    parts = iv.split_pages(pages, 100)
    assert parts == [(1, 1, "y" * 500)]


# ------------------------------------------------------------------ static, not prose
def test_replacement_characters_are_recognised_as_static():
    assert iv.looks_garbled("normal English prose, with punctuation.") == 0.0
    assert iv.looks_garbled("") == 0.0
    assert iv.looks_garbled("\ufffd" * 5 + "abc") > iv.GARBLE_LIMIT
    assert iv.looks_garbled("a" * 1000 + "\ufffd") < iv.GARBLE_LIMIT, "one bad char in a page is not static"


# ------------------------------------------------------------------ which tiers to walk
def test_the_plan_takes_the_shape_each_tier_actually_has():
    spec = {
        "tier1_markdown_核心": {"roots": [{"path": "notes"}, {"no_path": 1}]},
        "tier2_pdf_文字层": {"files": [{"path": "a.pdf"}]},
        "tier3_扫描件_需OCR": {"pilot": [{"path": "scan.pdf"}], "全量候选": [{"path": "big.pdf"}]},
    }
    roots, files = iv.plan_of(spec, ["tier1"])
    assert [r["path"] for r in roots] == ["notes"] and files == [], "a tier2 file is not a tree"
    roots, files = iv.plan_of(spec, ["tier2"])
    assert roots == [] and [f["path"] for f in files] == ["a.pdf"]
    roots, files = iv.plan_of(spec, ["tier3"])
    assert [f["path"] for f in files] == ["scan.pdf", "big.pdf"], "the pilot and the rest both count"
    roots, files = iv.plan_of(spec, ["nope"])
    assert roots == [] and files == [], "an unknown tier is ignored, and says so on stderr"


def test_the_entries_in_a_tier_keep_the_order_they_were_written_in():
    """Order matters: a person reading the output compares it with the whitelist they wrote."""
    spec = {"tier2_pdf_文字层": {"files": [{"path": "b.pdf"}, {"path": "a.pdf"}]}}
    _roots, files = iv.plan_of(spec, ["tier2"])
    assert [f["path"] for f in files] == ["b.pdf", "a.pdf"]
