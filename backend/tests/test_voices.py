"""Cloned voices: a reference recording becomes an asset a film can be narrated in.

What is worth pinning here is the **boundary between the two namespaces**. `Tingting` is a macOS
system voice and `voice:clinic` is one of these; they go to different programs, and every way of
confusing them ends with a film narrated in a voice nobody chose — which is only noticed after it
has been watched, if at all.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app import assemble, localcmd, voices


@pytest.fixture
def voice_folder(tmp_path):
    """A folder of the user's own voices, undone afterwards.

    Module-level state on purpose (the assembler reads it without a store to ask), so a test that
    points it at a `tmp_path` has to point it back.
    """
    voices.use_folder(tmp_path / "voices")
    try:
        yield tmp_path / "voices"
    finally:
        voices.use_folder(None)


def put(folder, name: str, body, *, audio: bytes = b"RIFF....WAVE",
        write_ref: bool = True) -> None:
    """One voice folder. `write_ref=False` is for the case where the manifest is right and the
    recording is not there — writing it anyway would make the test pass for the wrong reason."""
    d = folder / name
    d.mkdir(parents=True, exist_ok=True)
    (d / voices.MANIFEST).write_text(
        body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    ref = str((body.get("ref_audio") if isinstance(body, dict) else "") or "ref.wav")
    if write_ref and "/" not in ref and ".." not in ref:
        (d / ref).write_bytes(audio)


def test_a_voice_is_a_folder_with_a_recording_and_its_transcript(voice_folder):
    """A voice is a *file*, not a run of the model: the shipped engine clones zero-shot, so the
    reference recording IS the voice and there is nothing to train."""
    put(voice_folder, "clinic-zh", {"ref_audio": "ref.wav", "ref_text": "这是十秒钟的参考录音。",
                                    "note": "门诊讲解用"})
    got = voices.reload(force=True)
    assert got["loaded"] == ["clinic-zh"] and got["errors"] == []
    row = voices.row("clinic-zh")
    assert row["engine"] == "omnivoice" and row["ref_text"].startswith("这是十秒")
    assert pathlib.Path(row["path"]).is_file()
    assert row["timeout"] == 900, "a clone loads a model before it says anything"


def test_what_makes_a_voice_unusable_is_named_and_not_skipped(voice_folder):
    """A voice that is quietly not loaded narrates the film in *something else* while everybody
    believes the group's own voice was used."""
    put(voice_folder, "no-ref", {"ref_text": "只有转写"})
    put(voice_folder, "no-file", {"ref_audio": "missing.wav"}, write_ref=False)
    put(voice_folder, "escapes", {"ref_audio": "../../../etc/hosts"})
    put(voice_folder, "bad-engine", {"ref_audio": "ref.wav", "engine": "whisper-tts"})
    put(voice_folder, "bad-json", "{ not json")
    put(voice_folder, "fine", {"ref_audio": "ref.wav", "ref_text": "ok"})
    got = voices.reload(force=True)
    assert got["loaded"] == ["fine"]
    why = {e["file"]: e["why"] for e in got["errors"]}
    assert set(why) == {"no-ref", "no-file", "escapes", "bad-engine", "bad-json"}
    for name, said in why.items():
        assert said.strip(), f"{name} was rejected with no reason"
    assert "outside" in why["escapes"] or "之外" in why["escapes"]


def test_a_system_voice_and_a_cloned_one_are_different_things(voice_folder):
    """`Tingting` goes to `say`; `voice:clinic-zh` goes to the cloning engine. One function decides
    which, because every other place that had to guess got it wrong in a different way."""
    put(voice_folder, "clinic-zh", {"ref_audio": "ref.wav"})
    voices.reload(force=True)
    assert voices.parse("Tingting") == ("system", None)
    assert voices.parse("") == ("system", None)
    kind, row = voices.parse("voice:clinic-zh")
    assert kind == "clone" and row["name"] == "clinic-zh"
    assert voices.audio_suffix("Tingting") == ".aiff"
    assert voices.audio_suffix("voice:clinic-zh") == ".wav"
    assert voices.is_clone("VOICE:clinic-zh"), "the prefix is not case-sensitive"


def test_a_cloned_voice_that_does_not_exist_fails_instead_of_falling_through(voice_folder):
    """The failure this prevents has two halves. Handing `voice:clinic` to `say` fails inside a
    program the user never asked about and reads as "the narration broke"; falling through to the
    engine with a *system* voice name reads as success. So a name that is not in the registry is an
    error, and the error lists the ones that are."""
    put(voice_folder, "clinic-zh", {"ref_audio": "ref.wav"})
    voices.reload(force=True)
    with pytest.raises(ValueError) as e:
        voices.parse("voice:clinnic")
    said = str(e.value)
    assert "clinnic" in said and "clinic-zh" in said
    assert "macOS" in said or "系统嗓音" in said, "it says what the unprefixed form would have meant"


def test_the_assembler_refuses_an_unknown_cloned_voice_before_it_records_anything(voice_folder):
    assert voices.names() == []
    with pytest.raises(assemble.AssembleError) as e:
        assemble.pick_voice("一句话", "voice:nope")
    assert "nope" in str(e.value)


def test_the_narration_budget_follows_the_narrator(voice_folder):
    """`say` answers in a second, so the caller caps it at two minutes. A cloning engine loads a
    model first, and that same cap fails the *first* line of a correctly configured film — with a
    message about the narration rather than about the cap."""
    put(voice_folder, "slow", {"ref_audio": "ref.wav", "timeout": 1200})
    voices.reload(force=True)
    assert assemble.speak_timeout("Tingting", 1800) == 120.0
    assert assemble.speak_timeout("voice:slow", 60) == 1200.0, "the voice's own budget, not the cap"
    put(voice_folder, "default-budget", {"ref_audio": "ref.wav"})
    voices.reload(force=True)
    assert assemble.speak_timeout("voice:default-budget", 60) == 900.0


def test_the_engine_flags_are_the_ones_the_engine_declares(voice_folder):
    """`--ref_audio`, with underscores. argparse does not accept `--ref-audio`, and a renamed flag
    fails at synthesis time — after the film has been timed. Pinned here so a "tidy-up" cannot
    quietly change it."""
    put(voice_folder, "clinic-zh", {"ref_audio": "ref.wav", "ref_text": "参考", "language": "zh"})
    voices.reload(force=True)
    row = voices.row("clinic-zh")
    argv = voices.argv_for(row, "念这句", pathlib.Path("/tmp/out.wav"))
    assert argv[0] == "omnivoice-infer"
    assert "--ref_audio" in argv and row["path"] in argv
    assert "--ref-audio" not in argv and "--refAudio" not in argv
    assert argv[argv.index("--text") + 1] == "念这句", "the line goes in as one argument"
    assert argv[argv.index("--output") + 1] == "/tmp/out.wav"
    assert "--ref_text" in argv and "--language" in argv
    minimal = voices.argv_for({**row, "ref_text": "", "language": ""}, "x", pathlib.Path("/tmp/o.wav"))
    assert "--ref_text" not in minimal, "an empty transcript is left off rather than passed empty"


def test_the_local_tool_delegates_the_voice_command_rather_than_copying_it(voice_folder):
    """`localcmd` runs the same binary for the same job, so it must not keep a second spelling of
    the flags — one of the two would end up out of date and only one of them is tested."""
    put(voice_folder, "clinic-zh", {"ref_audio": "ref.wav", "ref_text": "参考"})
    voices.reload(force=True)
    ref = voices.row("clinic-zh")["path"]
    delegated = localcmd.argv_for("voicestudio", out="/tmp/o.wav", instruction="念这句",
                                  ref_audio=ref, ref_text="参考")
    assert delegated == voices.argv_for({**voices.row("clinic-zh"), "path": ref}, "念这句",
                                        pathlib.Path("/tmp/o.wav"))
    plain = localcmd.argv_for("voicestudio", out="/tmp/o.wav", instruction="念这句")
    assert "--ref_audio" not in plain, "with no voice chosen there is nothing to reference"


def test_a_voice_can_point_at_its_engine_because_the_engine_is_not_on_path(voice_folder,
                                                                          tmp_path):
    """The gap that only shows up on a real machine: a cloning engine normally lives in the project's
    own virtualenv (`<clone>/.venv/bin/omnivoice-infer`), which is on nobody's PATH. Without a way
    to name it, the voice layer is complete and the engine is unfindable — which reads as "the
    narration broke" rather than "point me at the program"."""
    real = tmp_path / "engine"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    real.chmod(0o755)
    put(voice_folder, "pointed", {"ref_audio": "ref.wav", "bin": str(real)})
    voices.reload(force=True)
    assert voices.row("pointed")["bin"] == str(real)
    assert voices.binary("omnivoice", str(real)) == str(real)

    not_executable = tmp_path / "notes.txt"
    not_executable.write_text("x", encoding="utf-8")
    assert voices.binary("omnivoice", str(not_executable)) == "", \
        "a path that is not an executable is treated as absent, not trusted"
    assert voices.binary("omnivoice", str(tmp_path / "nope")) == ""


def test_missing_engine_is_named_with_the_command_that_installs_it(monkeypatch):
    monkeypatch.setattr(voices, "binary", lambda *_a, **_k: "")
    said = voices.reason_missing("omnivoice")
    assert "omnivoice-infer" in said and "uv sync" in said


def test_qwen3tts_speaks_the_same_flags_the_tool_side_uses():
    """旁白侧给 Qwen3-TTS 拼出来的命令行,必须和它自己那个入口对得上。

    Qwen's installed CLI accepts hyphens; OmniVoice accepts underscores.
    """
    row = {"engine": "qwen3tts", "path": "/tmp/ref.wav", "ref_text": "参考原话", "language": "Chinese"}
    argv = voices.argv_for(row, "念这句", pathlib.Path("/tmp/out.wav"))
    assert argv[0] == "qwen-tts-say", argv
    assert argv[1:3] == ["--text", "念这句"], argv
    assert "--output" in argv and "/tmp/out.wav" in argv, argv
    assert "--ref-audio" in argv and "/tmp/ref.wav" in argv, argv
    assert "--ref-text" in argv and "参考原话" in argv, argv
    assert "--language" in argv and "Chinese" in argv, argv
