"""Where a narration segment lands: the caller's path is honoured, an escape is refused.

`synthesize_speech` used to write every segment to `speech/narration-<random>.wav` with no way to
ask for anything else. So a task whose deliverable said `音频/旁白-镜1.wav` could never be verified:
the member recorded all seven segments, reported them honestly, and was stamped "did not deliver"
anyway — measured 2026-09-26 in group 视频制作, and the round's turns were spent redoing narration
that already existed. The path is the caller's choice now; these tests pin the four shapes.
"""

from __future__ import annotations

import pytest

from app.toolhub import narration_dest


def test_without_a_name_the_file_goes_under_speech(tmp_path):
    got = narration_dest(tmp_path, "", ".wav", "narration-abc.wav")

    assert got == (tmp_path / "speech" / "narration-abc.wav").resolve()


def test_the_path_the_plan_promised_is_honoured(tmp_path):
    got = narration_dest(tmp_path, "音频/旁白-镜1.wav", ".wav", "narration-abc.wav")

    assert got == (tmp_path / "音频" / "旁白-镜1.wav").resolve()


def test_a_name_without_a_suffix_gets_the_engines(tmp_path):
    got = narration_dest(tmp_path, "音频/旁白-镜1", ".wav", "x")

    assert got.name == "旁白-镜1.wav"


def test_the_container_is_the_engines_not_a_wish(tmp_path):
    """`.mp3` holding WAVE bytes is a file whose name lies, and ffmpeg reads it happily."""
    got = narration_dest(tmp_path, "音频/旁白-镜1.mp3", ".wav", "x")

    assert got.suffix == ".wav"


def test_a_folder_and_a_way_out_are_both_refused(tmp_path):
    with pytest.raises(ValueError):
        narration_dest(tmp_path, "音频/", ".wav", "x")
    # ⚠️ Not rewritten into `<workspace>/a.wav`: refusing to escape and quietly writing somewhere
    # else are different answers, and only the first one tells the caller what happened.
    for escape in ("../a.wav", "音频/../../a.wav", "/tmp/a.wav"):
        with pytest.raises(ValueError):
            narration_dest(tmp_path, escape, ".wav", "x")
