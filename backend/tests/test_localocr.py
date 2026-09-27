"""Reading the text off a picture, on this machine, with nothing configured.

The capability exists because of a specific dead end: on a machine with no vision model and cloud
vision off, an attached screenshot produced one sentence — configure a model — and a member that
only wanted the words in an error dialog got nothing. macOS ships a text recogniser, so that case
costs nothing and needs no account.

Two things this file is here to hold, because they are the ways this could quietly go wrong:

  * **the program is judged by its answer, not by its name.** `bindirs.tool("ocr")` finds any
    executable called `ocr`; a different one would be handed `--json`, print something else, and be
    read as "this picture has no text" — a wrong answer that looks exactly like a right one;
  * **text never passes for looking.** A member told "here is what the picture says" when all that
    happened was a recognition pass will describe a chart from its axis labels and believe it has
    understood the chart. The wording is not decoration here; it is the safety property.
"""

from __future__ import annotations

import asyncio
import pathlib
import stat
import sys

import pytest

from app import figure, localocr
from tests.conftest import FakeLLM
from tests.conftest import has as says
from tests.test_collab import setup

# The wording lives in two languages and the tests must not pin one of them — the same rule the
# rest of this suite follows (see `conftest.has`). Each tuple is (English, Chinese), and the
# assertions below check for the *claim*, never for the sentence.
NOT_A_DESCRIPTION = ("NOT a description", "不是对画面的描述")
NO_TEXT_FOUND = ("no text found", "没有读出文字")
LEFT_OUT = ("left out", "已略去")
NOT_OURS = ("not the one this app expects", "不是本程序要的那个")
BUILD_RECIPE = ("macos-ocr",)          # a path, so it is the same in both languages

STUB = '''#!{py}
import json, os, sys, time

args = sys.argv[1:]
if args and args[0] == "--languages":
    print({languages!r})
    sys.exit(0)
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
for path in [a for a in args if a != "--json"]:
    if path.endswith("refuse.png"):
        continue                      # the recogniser refused it: no line, which is how it shows
    print(json.dumps({{"file": path, "width": 10, "height": 10, "lines": [
        {{"text": "读到的一行", "conf": 0.99, "box": [0, 0, 1, 1]}},
        {{"text": "太糊的一行", "conf": 0.10, "box": [0, 0, 1, 1]}},
    ]}}))
'''


@pytest.fixture(autouse=True)
def _forget_probes():
    """`_PROBED` is keyed by binary path and lives as long as the process. A stub written into one
    test's directory must not answer for the real one, or the other way round."""
    localocr._PROBED.clear()
    yield
    localocr._PROBED.clear()


def stub_ocr(tmp_path: pathlib.Path, *,
             languages: str = "accurate (30 languages): en-US zh-Hans",
             name: str = "ocr") -> pathlib.Path:
    """A stand-in recogniser, answering exactly the two questions the module asks.

    Public because `test_images` needs one too: the attachment path falls back to this module, and
    that behaviour is tested where every other attachment decision is tested.
    """
    exe = tmp_path / name
    exe.write_text(STUB.format(py=sys.executable, languages=languages), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def _orch(store, make_router, **cfg):
    fake = FakeLLM(default="ok")
    orch, group = setup(store, make_router, fake, **cfg)
    return orch, group, fake


def _call(orch, ctx, name, args):
    return asyncio.run(orch.toolhub.call(ctx, name, args, None))


def _tools(orch, store, group):
    ctx = asyncio.run(orch.toolhub.context(store.get_group(group["id"]),
                                           store.get_agent(group["host_agent_id"]),
                                           connect=False))
    return ctx


# ------------------------------------------------------------------ which program is this
def test_a_binary_that_answers_the_probe_is_the_one_used(tmp_path, monkeypatch):
    exe = stub_ocr(tmp_path)
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(exe))
    assert localocr.available() == (str(exe), "")


def test_a_different_binary_called_ocr_is_refused_and_named(tmp_path, monkeypatch):
    """⚠️ The name is not evidence. Without this check, any `ocr` on the machine answers `--json`
    with something else, and the module reports "no text in this picture" — a wrong answer wearing
    the face of a right one."""
    exe = stub_ocr(tmp_path, languages="TEXT DETECTOR 1.0 — usage: ocr <file>")
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(exe))
    ok, why = localocr.available()
    assert ok == ""
    assert str(exe) in why, "拒绝的原因要点名是哪个程序"
    assert localocr.OVERRIDE_ENV in why, "并告诉读者怎么改指"
    assert says(why, NOT_OURS)


def test_no_program_at_all_says_how_to_get_one(monkeypatch):
    """`bindirs.tool` is stubbed rather than the environment: this machine really does have one, so
    "there is none" has to be arranged deliberately."""
    monkeypatch.setattr(localocr, "_tool", lambda name: None)
    monkeypatch.delenv(localocr.OVERRIDE_ENV, raising=False)
    ok, why = localocr.available()
    assert ok == "" and why
    assert says(why, BUILD_RECIPE), "只说『没有』对没编过它的人是死路"


# ------------------------------------------------------------------ what comes back
def test_the_text_read_is_returned_and_faint_lines_are_counted(tmp_path, monkeypatch):
    exe = stub_ocr(tmp_path)
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(exe))
    blocks = localocr.read([tmp_path / "a.png"])
    assert [b["text"] for b in blocks] == ["读到的一行"]
    assert blocks[0]["dropped"] == 1, "低置信的那行要被数出来,不是假装不存在"
    assert "太糊的一行" not in blocks[0]["text"]


def test_the_count_of_what_was_left_out_reaches_the_reader(tmp_path, monkeypatch):
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    out = localocr.render(localocr.read([tmp_path / "a.png"]))
    assert "1" in out and says(out, LEFT_OUT), out


def test_a_picture_the_recogniser_refused_is_in_the_answer(tmp_path, monkeypatch):
    """An answer about seven of eight pictures that does not say which one is missing is worse than
    an answer that mentions all eight."""
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    blocks = localocr.read([tmp_path / "a.png", tmp_path / "refuse.png"])
    assert len(blocks) == 2, "拒绝的那张也要占一个位置"
    assert blocks[0]["text"] and blocks[1]["text"] == "" and blocks[1]["error"]
    out = localocr.render(blocks)
    assert "a.png" in out and "refuse.png" in out


def test_a_picture_with_no_text_says_so_rather_than_reading_as_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(localocr, "MIN_CONF", 2.0)   # nothing clears it: everything is "too faint"
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    out = localocr.render(localocr.read([tmp_path / "a.png"]))
    assert says(out, NO_TEXT_FOUND), out


def test_text_is_never_presented_as_having_looked_at_the_picture(tmp_path, monkeypatch):
    """The safety property of this module, and the reason `render` is the only way out."""
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    out = localocr.render(localocr.read([tmp_path / "a.png"]))
    assert says(out, NOT_A_DESCRIPTION), out
    assert "读到的一行" in out, "说清楚它不是什么,不等于不给文字"


def test_a_slow_recogniser_is_a_sentence_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    monkeypatch.setenv("STUB_SLEEP", "5")
    with pytest.raises(localocr.OcrError) as err:
        localocr.read([tmp_path / "a.png"], timeout=0.4)
    assert str(err.value), "总得有话说"


# ------------------------------------------------------------------ handing it to a member
def test_the_tool_is_offered_when_this_machine_can_read_text(store, make_router, tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    orch, group, _ = _orch(store, make_router)
    assert "read_image_text" in _tools(orch, store, group).tools


def test_the_tool_is_withheld_when_it_cannot_run(store, make_router, monkeypatch):
    """The rule every other tool follows: never hand a member a tool that is certain to fail. And it
    is withheld **without** a line in `problems`, which is posted into the chat once per turn — a
    machine that simply has not built this program must not get a sentence about it every turn."""
    monkeypatch.setattr(localocr, "available", lambda: ("", "no `ocr` program here"))
    orch, group, _ = _orch(store, make_router)
    ctx = _tools(orch, store, group)
    assert "read_image_text" not in ctx.tools
    assert not any("ocr" in p for p in ctx.problems), ctx.problems


def test_a_member_can_read_the_text_off_a_picture_in_its_workspace(store, make_router, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    orch, group, _ = _orch(store, make_router)
    ctx = _tools(orch, store, group)
    (store.workspace_dir(group["id"]) / "shot.png").write_bytes(b"not really a png")

    out = _call(orch, ctx, "read_image_text", {"path": "shot.png"})
    assert out.ok, out.text
    assert "读到的一行" in out.text
    assert says(out.text, NOT_A_DESCRIPTION)


def test_reading_the_text_did_not_touch_the_workspace(store, make_router, tmp_path, monkeypatch):
    """`review_picture`'s rule, and it applies here for the same reason: anything left in the
    workspace can be picked up later by `assemble_video` as if it were a shot."""
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    orch, group, _ = _orch(store, make_router)
    ctx = _tools(orch, store, group)
    workspace = store.workspace_dir(group["id"])
    (workspace / "shot.png").write_bytes(b"not really a png")
    before = sorted(p.name for p in workspace.iterdir())

    _call(orch, ctx, "read_image_text", {"path": "shot.png"})
    assert sorted(p.name for p in workspace.iterdir()) == before


def test_too_many_paths_asks_for_a_split_rather_than_truncating(store, make_router, tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    orch, group, _ = _orch(store, make_router)
    ctx = _tools(orch, store, group)
    many = [f"f{i}.png" for i in range(localocr.MAX_IMAGES + 1)]

    out = _call(orch, ctx, "read_image_text", {"paths": many})
    assert not out.ok
    assert str(localocr.MAX_IMAGES) in out.text, "要说清上限是多少"


def test_a_missing_file_names_what_is_missing(store, make_router, tmp_path, monkeypatch):
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    orch, group, _ = _orch(store, make_router)
    ctx = _tools(orch, store, group)
    out = _call(orch, ctx, "read_image_text", {"path": "not-here.png"})
    assert not out.ok and "not-here.png" in out.text


def test_a_video_is_read_at_the_moment_asked_for(store, make_router, tmp_path, monkeypatch):
    """A frame goes through the same route `review_picture` uses, so "is this a picture or a video"
    is not decided twice — and the frame that lands in a temporary file is removed again."""
    monkeypatch.setenv(localocr.OVERRIDE_ENV, str(stub_ocr(tmp_path)))
    orch, group, _ = _orch(store, make_router)
    ctx = _tools(orch, store, group)
    seen: list[pathlib.Path] = []

    def fake_frame(target, at, cleanup):
        # Stands in for `_frame_of`: the point of this test is the route and the cleanup, not ffmpeg.
        tmp = tmp_path / "frame.jpg"
        tmp.write_bytes(b"frame")
        cleanup.append(tmp)
        seen.append(target)
        return "image/jpeg", b"frame"

    monkeypatch.setattr(orch.toolhub, "_frame_of", fake_frame)
    monkeypatch.setattr(orch.toolhub, "_moment_of", lambda target, args: 3.0)
    (store.workspace_dir(group["id"]) / "clip.mp4").write_bytes(b"not really a video")

    out = _call(orch, ctx, "read_image_text", {"path": "clip.mp4", "at_seconds": 3})
    assert out.ok, out.text
    assert "3" in out.text, "回答里要说是读的哪一帧"
    assert seen, "_frame_of 才是取帧的那条路"


# ------------------------------------------------------------------ the real recogniser
@pytest.mark.skipif(not localocr.available()[0], reason="no `ocr` program on this machine")
def test_the_real_program_reads_chinese_off_a_picture_here(tmp_path):
    """The stub proves the plumbing; this proves the thing itself. Skipped rather than mocked when
    the program is absent, so the suite still runs everywhere else."""
    from PIL import Image, ImageDraw

    want = "文字识别"
    png = tmp_path / "cn.png"
    im = Image.new("RGB", (720, 240), "white")
    ImageDraw.Draw(im).text((40, 70), want, font=figure.font(96), fill="black")
    im.save(png)

    blocks = localocr.read([png])
    assert want in blocks[0]["text"], blocks[0]
    assert says(localocr.render(blocks), NOT_A_DESCRIPTION)


@pytest.mark.skipif(not localocr.available()[0], reason="no `ocr` program on this machine")
def test_the_real_program_says_nothing_rather_than_inventing_it(tmp_path):
    from PIL import Image

    blank = tmp_path / "blank.png"
    Image.new("RGB", (400, 400), (30, 40, 50)).save(blank)
    blocks = localocr.read([blank])
    assert blocks[0]["text"] == ""
    assert blocks[0]["error"] == ""
    assert says(localocr.render(blocks), NO_TEXT_FOUND)
