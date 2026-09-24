"""The deliverable channel: a finished draft becomes a file the user can open.

Four of the things this app is asked for — an office document, a slide deck, a paper, a poster's
text — end as a file, and before this tool existed the only route to one was `run_code` importing
`python-docx` by hand (with code execution off by default). A group could discuss a deck for forty
turns and the task board called it done because somebody had replied.

So the claims defended here are about the *file*, not about "no exception was raised": read it back
with the same library that wrote it and check the words, the headings, the slides and the sheet names
are where they were asked to be, and that the containment rules hold (nothing outside the workspace,
never through a symlink). The last test drives the tool through the real hub, because the tool is
what the members actually call.
"""

from __future__ import annotations

import pathlib

import pytest

from app import docwrite


BODY = """# 颅内动脉瘤介入治疗

本文面向门诊患者与家属,用「水管上的鼓包」一条比喻讲清四种术式。

## 一、为什么会破

- 血流持续冲击管壁
- 薄弱处逐渐鼓出

## 二、四种术式

| 术式 | 怎么做 | 适合谁 |
|---|---|---|
| 单纯弹簧圈 | 用圈填满瘤腔 | 瘤颈较窄 |
| 血流导向支架 | 改变血流方向 | 宽颈、大型 |

1. 先做 CTA
2. 再由术者判断
"""


# ------------------------------------------------------------------ the four formats
def test_docx_keeps_headings_paragraphs_lists_and_the_table(tmp_path):
    path, count = docwrite.write(tmp_path, "交付/科普.docx", "docx", "颅内动脉瘤介入治疗", BODY)

    assert path.name == "科普.docx" and path.parent.name == "交付"
    assert path.is_file() and path.stat().st_size > 0
    from docx import Document
    doc = Document(str(path))
    texts = [p.text for p in doc.paragraphs]
    assert "颅内动脉瘤介入治疗" in texts
    # `#` is Heading 1 and `##` is Heading 2 — the level the text was written at is kept.
    heads = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert ("Heading 2", "一、为什么会破") in heads, heads
    assert ("Heading 1", "颅内动脉瘤介入治疗") in heads
    assert "血流持续冲击管壁" in texts                      # the bullet survived as text
    assert len(doc.tables) == 1 and doc.tables[0].rows[1].cells[0].text == "单纯弹簧圈"
    assert count > 0


def test_pptx_makes_a_cover_and_one_slide_per_heading(tmp_path):
    path, slides = docwrite.write(tmp_path, "课件.pptx", "pptx", "四种介入方式", BODY)

    from pptx import Presentation
    deck = Presentation(str(path))
    titles = [s.shapes.title.text for s in deck.slides if s.shapes.title is not None]
    assert titles[0] == "四种介入方式"                      # the cover carries the title
    assert "一、为什么会破" in titles and "二、四种术式" in titles   # one slide per `##`
    assert len(titles) == len(set(titles)), titles     # and no slide repeats the cover
    assert slides == len(deck.slides) - 1                   # the cover is not counted as content
    body_text = "\n".join(sh.text_frame.text for s in deck.slides for sh in s.shapes
                          if sh.has_text_frame and sh != s.shapes.title)
    assert "血流持续冲击管壁" in body_text


def test_xlsx_puts_each_heading_on_its_own_sheet_with_the_table(tmp_path):
    path, sheets = docwrite.write(tmp_path, "清单.xlsx", "xlsx", "术式对照", BODY)

    from openpyxl import load_workbook
    book = load_workbook(str(path))
    assert sheets == len(book.sheetnames) >= 2
    assert "一、为什么会破" in book.sheetnames
    rows = [c.value for row in book["二、四种术式"].iter_rows() for c in row if c.value]
    assert "血流导向支架" in rows and "宽颈、大型" in rows


def test_md_is_written_as_text_with_the_title_as_its_heading(tmp_path):
    path, _ = docwrite.write(tmp_path, "笔记", "md", "纪要", "- 甲\n- 乙\n")

    text = path.read_text(encoding="utf-8")
    assert path.name == "笔记.md" and text.startswith("# 纪要")
    assert "- 甲" in text


# ------------------------------------------------------------------ containment and refusal
@pytest.mark.parametrize("bad", ["/etc/passwd.docx", "~/x.docx", "C:/x.docx", "../../x.docx", "a/../../x.docx"])
def test_a_path_that_leaves_the_workspace_is_refused(tmp_path, bad):
    with pytest.raises(docwrite.DocError):
        docwrite.write(tmp_path, bad, "docx", "t", "x")
    assert not any(tmp_path.rglob("*.docx"))


def test_a_symlinked_folder_is_not_written_through(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "交付").symlink_to(outside, target_is_directory=True)

    with pytest.raises(docwrite.DocError):
        docwrite.write(workspace, "交付/报告.docx", "docx", "t", "x")
    assert list(outside.iterdir()) == []                     # nothing landed outside


def test_the_suffix_comes_from_the_format_and_an_empty_body_is_refused(tmp_path):
    path, _ = docwrite.write(tmp_path, "报告", "docx", "标题", "正文")
    assert path.name == "报告.docx"

    with pytest.raises(docwrite.DocError) as e:
        docwrite.write(tmp_path, "空.docx", "docx", "", "")
    assert "empty" in str(e.value).lower() or "空" in str(e.value)


def test_a_format_without_its_library_says_which_package_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(docwrite, "_installed", lambda mod: mod != "pptx")
    assert "pptx" not in docwrite.formats()

    with pytest.raises(docwrite.DocError) as e:
        docwrite.write(tmp_path, "课件.pptx", "pptx", "t", "x")
    assert "python-pptx" in str(e.value)
    assert "docx" in docwrite.formats()                       # the others still work


def test_an_unknown_format_is_named_back_to_the_caller(tmp_path):
    with pytest.raises(docwrite.DocError) as e:
        docwrite.write(tmp_path, "x.pdf", "pdf", "t", "x")
    assert "pdf" in str(e.value) and "docx" in str(e.value)


def test_rewriting_the_same_name_delivers_a_second_draft(tmp_path):
    docwrite.write(tmp_path, "报告.docx", "docx", "第一稿", "甲")
    docwrite.write(tmp_path, "报告.docx", "docx", "第二稿", "乙")

    from docx import Document
    texts = [p.text for p in Document(str(tmp_path / "报告.docx")).paragraphs]
    assert "第二稿" in texts and "第一稿" not in texts
    assert len(list(tmp_path.glob("*.docx"))) == 1             # replaced, not duplicated


# ------------------------------------------------------------------ through the tool the members call
def test_the_tool_writes_into_the_group_workspace_and_indexes_it(store, make_router):
    """The end-to-end claim: a member calls `write_document` and the file is in the workspace, named in
    the answer, and indexed into the group's library in the same turn — that is what lets the next
    member read the whole thing instead of a clipped excerpt."""
    import asyncio

    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]

    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    assert "write_document" in ctx.tools, list(ctx.tools)
    # It writes a file and runs nothing, so it is a `write` — no approval prompt, and it survives a
    # read-only round (a network-triggered turn keeps only `read` tools).
    assert ctx.tools["write_document"]["risk"] == "write"
    assert not any("write_document" in p for p in ctx.problems), ctx.problems

    out = asyncio.run(orch.toolhub.call(
        ctx, "write_document",
        {"path": "交付/报告.docx", "format": "docx", "title": "总结", "body": "## 结果\n- 完成"}))
    assert out.ok, out.text
    assert [f["name"] for f in out.files] == [str(pathlib.Path("交付/报告.docx"))]
    written = store.workspace_dir(g["id"]) / "交付" / "报告.docx"
    assert written.is_file() and written.stat().st_size > 0
    assert "交付/报告.docx" in out.text

    # Every answer says outright that the caller cannot see the result, so it cannot be tempted to
    # describe a layout it never looked at.
    assert "layout" in out.text or "版面" in out.text

    # Same turn, same group: the file is in the library too (this is what lets the next member
    # read the whole document instead of the clipped hand-off text).
    synced = orch.library.sync_group_material(g["id"], store.workspace_dir(g["id"]))
    # The tool already indexed it (the second sync is a no-op, hence `skipped`), so what is
    # asserted is that the document is in the library at all.
    assert synced["documents"] >= 1, synced
