"""Reading someone else's video: the numbers first, then the pictures.

The claim this file defends is that a reference is *measured*, not admired. Everything the brief
later says about a film's format — how long it is, how big the frame is, how often it cuts, how loud
it is, whether anybody is speaking — comes from ffmpeg reading the actual file, and the tests build
real files with ffmpeg to check that. A mock would prove nothing here: the whole feature is "these
numbers are true of this file".

The second claim is quieter and matters just as much: what is written down is the *form*. The brief
carries a line saying the reference's own footage and music are not to be reused, and that line is
asserted below, because "imitate this video" is a sentence that can be misread.
"""

from __future__ import annotations

import pathlib

import pytest

from app import assemble, ffmpeg, study
from app.main import create_app
from app.toolhub import timeout_budget
from tests.conftest import FakeLLM
from tests.test_collab import setup

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available()[0], reason="needs ffmpeg")


# ------------------------------------------------------------------ real files, built here
def clip(path: pathlib.Path, *, seconds: float = 3.0, size: str = "320x180", rate: int = 25,
         plate: str = "testsrc", audio: bool = True) -> pathlib.Path:
    """A real video file. `plate` is any lavfi source — `testsrc` moves, `color=c=black` does not."""
    # lavfi option syntax: the first option joins the source name with `=`, the rest with `:`.
    # `color` already carries its first option (`c=black`), `testsrc` does not — so the two need
    # different joins, and getting this wrong makes ffmpeg answer "Invalid argument".
    graph = (f"{plate}:size={size}:rate={rate}:duration={seconds}" if plate.startswith("color=")
             else f"{plate}=size={size}:rate={rate}:duration={seconds}")
    cmd = [ffmpeg.path(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", graph]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-t", str(seconds), *ffmpeg.encoder(), "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    code, detail = assemble._run(cmd, 120)
    assert code == 0 and path.is_file(), detail
    return path


def cut_clip(path: pathlib.Path) -> pathlib.Path:
    """Three seconds, three plates, hard cuts at 1s and 2s — something to *count*."""
    parts = []
    for i, colour in enumerate(("black", "white", "black")):
        p = path.parent / f"part{i}.mp4"
        clip(p, seconds=1.0, plate=f"color=c={colour}", audio=False)
        parts.append(p)
    listing = path.parent / "parts.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    code, detail = assemble._run([ffmpeg.path(), "-hide_banner", "-loglevel", "error", "-y",
                                  "-f", "concat", "-safe", "0", "-i", str(listing),
                                  "-c", "copy", str(path)], 120)
    assert code == 0, detail
    return path


# ------------------------------------------------------------------ what the file is
@needs_ffmpeg
def test_the_numbers_the_brief_will_quote_are_read_off_the_file(tmp_path):
    """"42 seconds, 9:16, 25 fps, has sound" is only worth saying if it came from the file."""
    src = clip(tmp_path / "ref.mp4", seconds=3.0, size="320x180", rate=25)

    f = study.measure(src)

    assert 2.5 <= f["seconds"] <= 3.6, f
    assert (f["width"], f["height"]) == (320, 180)
    assert f["aspect"] == "16:9"
    assert 24 <= f["fps"] <= 26, f
    assert f["has_audio"] is True
    # Loudness, not just "there is a track": it is what tells a narration from a music bed from silence.
    assert isinstance(f["mean_db"], float) and f["mean_db"] < 0, f


@needs_ffmpeg
def test_a_silent_piece_is_reported_as_having_no_sound(tmp_path):
    """The other half: "no transcriber heard speech" and "there is no speech" are different answers."""
    src = clip(tmp_path / "quiet.mp4", seconds=1.0, audio=False)
    f = study.measure(src)
    assert f["has_audio"] is False and f["mean_db"] is None


@needs_ffmpeg
def test_cuts_are_counted_where_the_picture_actually_changes(tmp_path):
    """The rhythm in the brief is a count, and a count has to be checked against a known film.

    Three plates joined by hard cuts: the detector must find the two joins and not invent more.
    """
    src = cut_clip(tmp_path / "cuts.mp4")

    marks = study.cuts(src)

    assert len(marks) == 2, marks
    assert abs(marks[0] - 1.0) < 0.35 and abs(marks[1] - 2.0) < 0.35, marks


@needs_ffmpeg
def test_frames_are_evenly_spaced_real_pictures_of_the_moving_thing(tmp_path):
    """Not "eight files appeared": the frames are pictures, they are in order, and they differ —
    which is the only way to tell sampling from eight copies of the same instant."""
    src = clip(tmp_path / "moving.mp4", seconds=4.0, plate="testsrc")

    got = study.frames(src, 6, seconds=4.0)

    assert len(got) == 6, len(got)
    assert [t for t, _ in got] == sorted(t for t, _ in got), [t for t, _ in got]
    for _t, data in got:
        assert data[:2] == b"\xff\xd8" and len(data) > 800, "not a JPEG"
    # `testsrc` is a moving pattern: consecutive samples must not be byte-identical.
    assert got[0][1] != got[-1][1], "all frames are the same picture"
    # …and nothing was left behind for a later `assemble_video` to trip over.
    assert not any(p.name.startswith("ta-study-") for p in pathlib.Path("/tmp").glob("ta-study-*"))


def test_a_link_without_yt_dlp_names_the_command_that_installs_it(monkeypatch, tmp_path):
    """A missing tool is a sentence to the user, not a traceback — and the sentence has to be
    actionable, because "cannot fetch that link" alone leaves them nowhere to go."""
    monkeypatch.setattr(study, "downloader", lambda: None)

    with pytest.raises(study.StudyError) as e:
        study.download("https://example.org/v", tmp_path)

    assert "yt-dlp" in str(e.value) and ("brew" in str(e.value) or "pipx" in str(e.value))


# ------------------------------------------------------------------ what is written down
def test_the_brief_says_what_may_be_reused_and_what_may_not(tmp_path):
    """The one line in the document that is about rights rather than style."""
    facts = {"seconds": 42.0, "width": 1080, "height": 1920, "aspect": "9:16", "fps": 30.0,
             "has_audio": True, "mean_db": -20.0, "max_db": -1.0, "bytes": 10}
    out = study.write_spec(tmp_path / "ws", "参考风格-x.md", title="A reference",
                           url="https://example.org/v", facts=facts, body="## Format\n9:16")

    text = out.read_text(encoding="utf-8")
    assert out.parent.name == "ws" and out.name.endswith(".md")
    assert "https://example.org/v" in text, "the source is named"
    assert "42.0s" in text and "9:16" in text, "the measurements are quoted, not paraphrased"
    assert "1080x1920" in text
    assert study.RIGHTS_NOTE_ZH in text or study.RIGHTS_NOTE_EN in text


@needs_ffmpeg
def test_the_brief_to_a_model_carries_the_measured_numbers_and_a_real_transcript(tmp_path):
    """What the model is handed is the numbers plus the words — the style claims rest on both."""
    src = clip(tmp_path / "ref.mp4", seconds=2.0)
    facts = study.measure(src)
    text = study.facts_text(facts, {"shots": [0.8, 1.6], "read_seconds": 2.0,
                                    "transcript": "这是旁白的第一句。", "speech_why": ""})

    assert "2.0" in text and "16:9" in text
    assert "2 次切换" in text or "2 cuts" in text
    assert "这是旁白的第一句。" in text
    # And when the speech could not be read, that is said rather than left out.
    quiet = study.facts_text(facts, {"shots": [], "read_seconds": 2.0,
                                     "transcript": None, "speech_why": "no transcriber is installed"})
    assert "no transcriber is installed" in quiet


def test_two_references_with_the_same_title_do_not_pile_up(tmp_path):
    """The name is derived from the title, so re-studying the same film replaces its brief."""
    facts = {"seconds": 1.0, "width": 1, "height": 1, "aspect": "1:1", "fps": 1.0,
             "has_audio": False, "mean_db": None, "max_db": None, "bytes": 1}
    name = study.doc_name("Same  Title!")
    a = study.write_spec(tmp_path, name, title="Same", url="", facts=facts, body="first")
    b = study.write_spec(tmp_path, name, title="Same", url="", facts=facts, body="second")
    assert a == b and "second" in b.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("参考风格-*.md"))) == 1


# ------------------------------------------------------------------ through the tool layer
@needs_ffmpeg
def test_the_tool_is_offered_and_its_own_timeout_budget_comes_with_it(store, make_router):
    """The trap the plumbing guide warns about: a tool that names a `timeout_key` still gets the
    generic 60s if the key is not copied into the per-call spec — and a reference read takes minutes."""
    st = store
    st.add_model("ollama", "qwen3.5:9b")          # something that can look
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    ctx = __import__("asyncio").run(
        orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))

    assert "study_video" in ctx.tools, ctx.tools.keys()
    cfg = st.get_settings()
    assert timeout_budget(cfg, ctx.tools["study_video"]) == float(cfg["assemble_timeout"])
    # It runs an external program against a URL the user chose, so it asks first.
    assert ctx.tools["study_video"]["risk"] == "exec"


@needs_ffmpeg
def test_studying_a_local_reference_leaves_a_brief_the_whole_group_can_search(store, make_router,
                                                                             tmp_path, monkeypatch):
    """End to end on a real file with a stubbed pair of eyes: the brief lands in the workspace, the
    group's knowledge base picks it up, and the answer says where both went."""
    import asyncio

    from app import vision
    st = store
    st.add_model("ollama", "qwen3.5:9b")
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    group = st.get_group(g["id"])
    ws = st.workspace_dir(group["id"])
    src = clip(ws / "ref.mp4", seconds=2.0, plate="testsrc")

    seen: list[str] = []
    mimes: list[list[str]] = []

    async def fake_look(store_, router_, pics, prompt):  # noqa: ANN001
        seen.append(prompt)
        mimes.append([m for m, _ in pics])
        return "## 版式\n9:16, 一句话一屏。" if len(seen) == 1 else "## Format\n1080x1920, 30fps."

    monkeypatch.setattr(vision, "describe", fake_look)

    ctx = asyncio.run(orch.toolhub.context(group, st.list_agents()[0], connect=False))
    out = asyncio.run(orch.toolhub.call(ctx, "study_video", {"path": "ref.mp4"}, None))
    text, files = out.text, out.files

    assert out.ok, text
    assert len(seen) == 2, "frames are looked at, then the same frames are turned into a brief"
    # The first slot of each picture tuple is the MIME type. Putting a label there ("t=3s") gets the
    # whole call rejected by the provider with "Input should be 'image/jpeg'…" — and only there, which
    # is why the mime is asserted rather than assumed.
    assert mimes and all(m.startswith("image/") for m in mimes[0]), mimes
    # The times have to reach the model some other way, so they are in the prompt.
    assert "t=" in seen[0], seen[0][:200]
    assert "参考风格-ref.md" in text and files and files[0]["kind"] == "file"
    doc = ws / "参考风格-ref.md"
    assert doc.is_file()
    body = doc.read_text(encoding="utf-8")
    assert "1080x1920" in body and "9:16" in body, body
    # The line is asserted in whichever language this run is in: what matters is that it *travels*
    # with the brief, not that the test pinned one translation.
    assert study.RIGHTS_NOTE_ZH in body or study.RIGHTS_NOTE_EN in body, \
        "the reuse line travels with the brief"
    # ⚠️ **这里刻意不再断言「brief 自动进了群知识库」。** 曾经有一条自动同步:每轮发言前把工作目录里的
    # 文档收进本群知识库(`Library.sync_group_material`,由 `watch_workspace` 开关控制),它在
    # 2026-09-25 被**有意删掉**了 —— 项目材料不再自动进库(显式上传/attach 仍然进)。这条测试当时没跟着
    # 改,于是长期是红的,而**一条长期红的测试比没有测试更糟**:它让人以为每一次新改动都弄坏了它
    # (这一次它就差点被算到当天的改动头上,是靠逐个模块回退才摘清的)。
    # 现在断言的是**实际行为**:brief 实实在在落在工作目录里,并且**没有**被自动收进知识库。
    assert (ws / "参考风格-ref.md").is_file(), "brief 落在工作目录里"
    assert not [d for d in st.list_docs()
                if "参考风格" in str(d.get("title") or d.get("filename") or "")], \
        "项目材料不自动进知识库(2026-09-25 有意删掉的行为),所以这里应该是空的"


def test_a_reference_that_is_not_there_is_refused_by_name(store, make_router):
    import asyncio

    st = store
    st.add_model("ollama", "qwen3.5:9b")
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))

    gone = asyncio.run(orch.toolhub.call(ctx, "study_video", {"path": "nope.mp4"}, None))
    assert not gone.ok and "nope.mp4" in gone.text
    # And a call with neither a link nor a file says what to give it.
    empty = asyncio.run(orch.toolhub.call(ctx, "study_video", {}, None))
    assert not empty.ok and ("url" in empty.text and "path" in empty.text)


# ------------------------------------------------------------------ fetching a link
@needs_ffmpeg
def test_download_keeps_the_media_and_reads_the_title_from_the_info_file(monkeypatch, tmp_path):
    """The link path, verified without a network.

    A stub `yt-dlp` stands in for the platform: it copies a real file to the `-o` pattern and writes
    the info JSON the real one writes. What this checks is *our* side of the contract — that the
    arguments say what they must, that the info file is read for the title rather than treated as
    media, and that repeated runs land on one canonical name instead of piling up. The network path
    itself is left to a real run; the point here is that no amount of platform traffic can be blamed
    for our own bookkeeping being wrong.
    """
    import os
    sample = clip(tmp_path / "sample.mp4", seconds=1.0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "yt-dlp"
    stub.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in -o) out=\"$2\"; shift 2;; *) shift;; esac\n"
        "done\n"
        f"cp '{sample}' \"$(dirname \"$out\")/$(basename \"$out\" | sed 's/%(ext)s/mp4/')\"\n"
        f"printf '%s' '{{\"title\": \"Stub Reference\", \"uploader\": \"someone\", "
        f"\"duration\": 3.0, \"webpage_url\": \"https://example.org/v\"}}' "
        "> \"$(dirname \"$out\")/source.info.json\"\n",
        encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    dest = tmp_path / "ref"
    media, meta = study.download("https://example.org/v", dest)

    assert media.name == "source.mp4" and media.is_file(), media
    assert media.stat().st_size == sample.stat().st_size
    assert meta["title"] == "Stub Reference" and meta["uploader"] == "someone"
    # The info JSON is metadata, not a second copy of the film.
    assert media.suffix != ".json"

    # A second run with a different container must not leave two media files behind.
    second = bin_dir / "yt-dlp"
    second.write_text(stub.read_text(encoding="utf-8").replace("s/%(ext)s/mp4/", "s/%(ext)s/mkv/"),
                      encoding="utf-8")
    os.chmod(second, 0o755)
    media2, _ = study.download("https://example.org/v", dest)
    others = [p.name for p in dest.iterdir()
              if p.is_file() and p.suffix in (".mp4", ".mkv", ".webm")]
    assert len(others) == 1, others
    assert media2.suffix in (".mp4", ".mkv")
