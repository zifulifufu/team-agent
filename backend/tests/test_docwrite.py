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
def test_word_formats_emphasis_without_losing_medical_qualifiers(tmp_path):
    from docx import Document

    body = """## **适用边界**
该研究**不能据此推论**所有患者的风险，**任何**结论需区分人群。
- **以非卒中方式发现**的病例
| 条件 | 含义 |
|---|---|
| **例外** | 保留 |
未闭合 **字样与 `**代码**` 原样保留。
"""
    path, _ = docwrite.write(tmp_path, "综述.docx", "docx", "**综述**", body)
    doc = Document(path)
    paragraphs = doc.paragraphs + [c.paragraphs[0] for row in doc.tables[0].rows for c in row.cells]
    bold = [r.text for p in paragraphs for r in p.runs if r.bold]
    assert bold == ["综述", "适用边界", "不能据此推论", "任何", "以非卒中方式发现", "例外"]
    assert doc.paragraphs[2].text == "该研究不能据此推论所有患者的风险，任何结论需区分人群。"
    assert doc.paragraphs[-1].text == "未闭合 **字样与 `**代码**` 原样保留。"
    assert doc.tables[0].cell(1, 0).text == "例外"


def test_markdown_title_already_in_body_is_not_duplicated(tmp_path):
    path, _ = docwrite.write(tmp_path, "索引.md", "md", "核验索引", "# 核验索引\n\n正文")
    assert path.read_text() == "# 核验索引\n\n正文\n"


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
    assert texts.count("颅内动脉瘤介入治疗") == 1
    assert doc.paragraphs[0].style.name == "Title"
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

    # Every answer says outright that the caller cannot see the result, so it cannot be tempted to
    # describe a layout it never looked at.
    assert "layout" in out.text or "版面" in out.text

    # …and it is *not* in the library. Nothing of a group's own is filed there any more (`library`
    # says why, above `workspace_kb`); what this asserts is the new promise, and it is the one that
    # has to keep holding — the delivered file is on disk, where the group reads it.
    assert store.list_docs() == []
    assert written.is_file()


def test_a_workspace_reached_through_a_symlink_can_still_be_written(store, make_router, tmp_path):
    """⚠️ Regression: `write_document` reported failure *after* writing the file.

    `docwrite.write` resolves the workspace internally — it must, because refusing to write through a
    symlink is one of its jobs — so the path it hands back is canonical. The caller compared that
    against the spelling the settings were written with. On a workspace under a symlinked directory
    (macOS: `/tmp`, `/var`, `/etc`) the two disagree, `relative_to` raises, and the tool's catch-all
    turns a **successful** write into "写文件失败".

    The cost is not the wording. A member told its file was not written calls the tool again — one
    measured round had six identical `write_document(failed)` entries for a file that was on disk the
    whole time — and the round's tool budget is spent on retries of something that already worked.

    So the claim here is the whole path: a symlinked workspace root still returns `ok`, and the file
    is really there afterwards.
    """
    import asyncio

    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "linked-root"
    link.symlink_to(real, target_is_directory=True)
    store.update_settings({"code_workdir": str(link)})

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    out = asyncio.run(orch.toolhub.call(
        ctx, "write_document",
        {"path": "报告.md", "format": "md", "title": "总结", "body": "正文"}))
    assert out.ok, out.text
    assert "报告.md" in out.text
    assert (store.workspace_dir(g["id"]) / "报告.md").is_file()
    # The answer names the path and the size, which is what the next member relays to the reviewer.
    assert str(pathlib.Path("报告.md")) in out.text
    assert "layout" in out.text or "版面" in out.text
    assert store.list_docs() == []


# ---------------------------------------------- the format, when it is not spelled out
def test_the_format_is_read_off_the_path_when_it_is_not_given(tmp_path):
    """Measured: a correct `.md` path with no `format` was refused as "missing required argument" and
    the whole delivery had to be redone. The suffix already says what to write."""
    from app import docwrite

    assert docwrite.kind_of("科普短文.md", "") == "md"
    assert docwrite.kind_of("交付/报告.docx", None) == "docx"
    assert docwrite.kind_of("讲稿", "pptx") == "pptx"          # a bare name still works if told
    # Spelled with a word rather than a suffix — the other half of the same tolerance.
    assert docwrite.kind_of("a", "markdown") == "md"
    assert docwrite.kind_of("a", "Word") == "docx"
    assert docwrite.kind_of("a.pptx", "excel") == "xlsx"       # what was asked for wins
    assert docwrite.kind_of("a", "") == ""                     # nothing to go on: the caller refuses
    dest, _n = docwrite.write(tmp_path, "笔记.md", "", "t", "正文")
    assert dest.name == "笔记.md" and dest.read_text(encoding="utf-8").endswith("正文\n")


def test_library_read_reads_a_file_the_group_itself_produced(store, make_router):
    """The acceptance step of every plan runs on this. Everything a group makes is a file in its
    workspace and nothing of its own is filed into a library — so before this, the member whose job
    was to check a deliverable could not open it. The ledger recorded it verbatim: "协调员看不到工作
    目录里的 md 文件内容,无法独立核验文案交付物"."""
    import asyncio

    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    wrote = asyncio.run(orch.toolhub.call(
        ctx, "write_document",
        {"path": "交付/短文.md", "title": "短文", "body": "动脉瘤的介入治疗有三种主流术式。"}))
    assert wrote.ok, wrote.text

    # ① by path — what the member relays from the task folder
    got = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "交付/短文.md"}))
    assert got.ok, got.text
    assert "三种主流术式" in got.text
    # …and the answer says which of the two places it read from: a member that thinks a workspace
    # file came out of a library will later cite it as library material, which is a different claim.
    assert "workspace" in got.text or "工作目录" in got.text
    # ② by bare name — what it types when it just saw the name
    got = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "短文.md"}))
    assert got.ok and "三种主流术式" in got.text
    # ③ a file that is not there is still "not found", and the two answers stay distinguishable
    missing = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "不存在.md"}))
    assert not missing.ok and "资料库" in missing.text or "library" in missing.text
    # ④ a path that climbs out of the workspace is not read
    out = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "../../etc/hosts"}))
    assert not out.ok


def test_reading_a_workspace_file_refuses_what_it_should(store, make_router):
    """The containment rules of the read-back path, in one place.

    ⚠️ `lstrip("./")` used to do the stripping, and `lstrip` removes *characters* from the left: an
    illegal `../a.md` came back as `a.md` and was then happily read. A path guard that rewrites the
    path instead of refusing it is not a guard. Two more rules live here for the same reason: a
    **path** that is not there is not answered from somewhere else (that would hand back another
    task's deliverable looking like the right one), and a name with glob characters is refused
    outright rather than handed to `rglob` as a pattern.
    """
    import asyncio

    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    assert asyncio.run(orch.toolhub.call(
        ctx, "write_document", {"path": "tasks/甲/报告.md", "body": "甲的报告"})).ok

    # climbs out of the workspace: refused, not rewritten
    for bad in ("../报告.md", "tasks/../../报告.md", "/etc/hosts", "../../etc/hosts"):
        out = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": bad}))
        assert not out.ok, bad
    # a bare name is found where it lives…
    ok = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "报告.md"}))
    assert ok.ok and "甲的报告" in ok.text
    # …but a *path* that does not exist is never answered with somebody else's file
    other = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "tasks/乙/报告.md"}))
    assert not other.ok, other.text
    assert "tasks/甲/报告.md" in other.text  # useful candidate, not a silent substitution
    assert "甲的报告" not in other.text and "NoneType" not in other.text
    assert "exact" in other.text or "准确路径" in other.text
    # and a pattern is refused rather than expanded
    pat = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "*.md"}))
    assert not pat.ok


async def test_workspace_listing_finds_exact_handoff_paths_without_reading_or_leaving_group(store, make_router, tmp_path):
    import json
    from tests.conftest import FakeLLM
    from tests.test_collab import setup
    from app.toolhub import planning_tools

    orch, group = setup(store, make_router, FakeLLM())
    ctx = await orch.toolhub.context(store.get_group(group["id"]), store.list_agents()[0], read_only=True)
    assert "list_workspace_files" in planning_tools(ctx.tools)[0]
    root = store.workspace_dir(group["id"])
    folder = root / "tasks" / "actual-id"
    folder.mkdir(parents=True)
    for name in ("state-1.md", "state-2.md"):
        (folder / name).write_text("Synthetic private body; only metadata should be listed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state-secret.md").write_text("not in group")
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    (folder / "state-link.md").symlink_to(outside / "state-secret.md")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "state-hidden.md").write_text("hidden")
    result = await orch.toolhub.call(ctx, "list_workspace_files", {"query": "state", "limit": 1})
    first = json.loads(result.text)
    assert result.ok and first["matched"] == 2 and first["next_offset"] == 1
    second = json.loads((await orch.toolhub.call(ctx, "list_workspace_files",
                        {"query": "state", "limit": 1, "offset": 1})).text)
    assert second["next_offset"] is None
    assert {f["path"] for f in first["files"] + second["files"]} == {
        "tasks/actual-id/state-1.md", "tasks/actual-id/state-2.md"}
    assert "Synthetic private body" not in result.text and "state-secret" not in result.text


def test_library_read_a_deep_path_that_is_not_there_does_not_crash(store, make_router):
    """The call three groups failed on, with the arguments they actually sent (2026-09-25/26).

    `library_read` with a full task path whose file did not exist answered
    `工具执行出错:AttributeError: 'NoneType' object has no attribute 'relative_to'` — in 介入术式科,
    前交通动脉 and 冗长扩张动, from four different members. A crash is not an answer: the member
    cannot tell "write the file first" from "the tool is broken", and the acceptance step of every
    plan runs on this call.
    """
    import asyncio

    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    for doc in ("tasks/bc09bbd5be8c/文献核验清单落盘-05abf364416a/文献核验清单.md",
                "tasks/f4feec93da49/示意图-F-G-H-制作-c61d85a54f1b/fig_f_flow_diverter.png",
                "tasks/35f7f9bc9e0e/V7-V9-A2-变异三图核验与补齐-1c9e9c1349e4/记录.md"):
        out = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": doc}))
        assert "relative_to" not in out.text, (doc, out.text)
        assert not out.ok, (doc, out.text)


def test_write_document_append_continues_a_long_one_across_calls(store, make_router):
    """The fix for the failure that took a whole group's task down: a 3000-character review cannot
    come out of one reply.

    Measured 2026-09-26 (group 冗长扩张动): the member was asked for a long document, put the whole
    body into one `write_document` call, and the reply ran past the model's output limit. The router
    throws such a call away whole (`模型回复被输出上限截断,未执行不完整的工具参数`) — so nothing was
    written, four times in a row, from two different members, and the task ended with no deliverable
    at all. Sections are the way out: `replace` for the first, `append` for every one after it.
    """
    import asyncio

    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    first = asyncio.run(orch.toolhub.call(ctx, "write_document", {
        "path": "交付/综述.md", "title": "冗长扩张动脉瘤综述", "body": "# 一、定义\n\n第一节正文。"}))
    assert first.ok, first.text
    second = asyncio.run(orch.toolhub.call(ctx, "write_document", {
        "path": "交付/综述.md", "mode": "append", "body": "# 二、影像\n\n第二节正文。"}))
    assert second.ok, second.text
    # The member has to be told it was an append, and how much was already there — without that it
    # cannot tell a finished file from one that still stops in the middle.
    assert "接在" in second.text or "appended" in second.text, second.text
    got = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "交付/综述.md"}))
    assert got.ok, got.text
    assert "第一节正文" in got.text and "第二节正文" in got.text, got.text
    assert got.text.count("冗长扩张动脉瘤综述") == 1, got.text      # the title is not printed twice
    # …and replace still replaces: a revision is written the same way it always was
    again = asyncio.run(orch.toolhub.call(ctx, "write_document", {
        "path": "交付/综述.md", "mode": "replace", "body": "只有这段。"}))
    assert again.ok, again.text
    after = asyncio.run(orch.toolhub.call(ctx, "library_read", {"doc": "交付/综述.md"}))
    assert "第一节正文" not in after.text and "只有这段" in after.text, after.text
