"""The music shelf and the ducking windows, pinned.

What these protect is the part that fails without saying anything:

* **A sidecar that does not parse must be named.** Skipped, it reads as "this track simply has no
  metadata" — and a track chosen for the wrong reason is worse than one chosen for none.
* **A name that is not on the shelf must raise and list what is.** There is a fallback in the obvious
  alternative, and the fallback is a film scored with music nobody asked for.
* **The ducking windows come from the plan, not from a guess.** A window half a second out either
  clips the first word of the narration or leaves the music up through it; both are audible and
  neither raises.

The files are written as plain bytes: the shelf identifies tracks by extension and never opens them,
so a test that decoded real audio would be testing ffmpeg, not this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import assemble, comfyui, music


@pytest.fixture(autouse=True)
def _restore_shelf():
    """`use_folder` is module state; put it back so one test cannot leak into the next."""
    before = music.folder_of()
    try:
        yield
    finally:
        music.use_folder(before)


def shelf(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    folder = tmp_path / "music"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (folder / name).write_text(body, encoding="utf-8") if isinstance(body, str) \
            else (folder / name).write_bytes(body)
    music.use_folder(folder)
    return folder


def track(name: str, **meta) -> dict[str, str | bytes]:
    """A track: the audio file, plus its sidecar when there is anything to say."""
    out: dict[str, str | bytes] = {f"{name}.mp3": b"\x00" * 16}
    if meta:
        out[f"{name}.json"] = json.dumps(meta, ensure_ascii=False)
    return out


# ------------------------------------------------------------------ the shelf itself
def test_audio_files_are_tracks_and_other_files_are_ignored(tmp_path):
    shelf(tmp_path, {"calm-piano.mp3": b"\x00" * 32, "notes.txt": "not music", ".DS_Store": b"x"})
    assert music.names() == ["calm-piano"]


def test_a_sidecar_supplies_the_things_you_choose_on(tmp_path):
    shelf(tmp_path, track("storm", title="Storm", mood="tense", tags=["strings"],
                          seconds=95, source="purchased", licence="royalty-free"))
    row = music.row("storm")
    assert row["title"] == "Storm" and row["mood"] == "tense"
    assert row["tags"] == ["strings"] and row["seconds"] == 95.0
    assert "royalty-free" in music.describe(row)


def test_a_track_with_no_sidecar_still_plays_and_says_so(tmp_path):
    shelf(tmp_path, {"bare.mp3": b"\x00" * 16})
    row = music.row("bare")
    assert row["name"] == "bare" and row["mood"] == "neutral" and row["seconds"] == 0.0


def test_a_broken_sidecar_takes_the_track_out_and_names_the_file(tmp_path):
    """Not a silent "this one happens to have no metadata": a track whose sidecar cannot be read is
    off the shelf, and the file that broke it is named."""
    shelf(tmp_path, {"half.json": "{ not json", "half.mp3": b"\x00" * 16})
    assert music.names() == []
    errors = music.errors()
    assert errors and "half.json" in errors[0]["why"]


def test_a_mood_outside_the_vocabulary_is_an_error_not_a_silent_no_match(tmp_path):
    shelf(tmp_path, {"odd.json": json.dumps({"mood": "moody"}), "odd.mp3": b"\x00" * 16})
    assert music.names() == []
    assert music.errors() and "moody" in music.errors()[0]["why"]


def test_a_sidecar_pointing_at_nothing_does_not_create_a_track(tmp_path):
    shelf(tmp_path, {"ghost.json": json.dumps({"title": "ghost"})})
    assert music.names() == []


# ------------------------------------------------------------------ resolving a name
def test_an_unknown_name_raises_and_lists_what_exists(tmp_path):
    shelf(tmp_path, {"a.mp3": b"\x00" * 8, "b.mp3": b"\x00" * 8})
    with pytest.raises(KeyError) as e:
        music.resolve("c")
    message = str(e.value)
    assert "a" in message and "b" in message          # never a fallback


def test_an_empty_shelf_says_so_rather_than_picking_something(tmp_path):
    shelf(tmp_path, {})
    with pytest.raises(KeyError):
        music.pick()


def test_a_path_to_an_audio_file_is_used_as_a_path(tmp_path):
    shelf(tmp_path, {})
    loose = tmp_path / "loose.wav"
    loose.write_bytes(b"\x00" * 16)
    name, row = music.resolve(str(loose))
    assert name == "loose" and row["file"] == str(loose)


def test_a_shelf_name_wins_over_the_same_name_on_disk(tmp_path):
    folder = shelf(tmp_path, {"theme.mp3": b"\x00" * 8})
    music.use_folder(folder)
    name, row = music.resolve("theme")
    assert row["file"] == str(folder / "theme.mp3")


# ------------------------------------------------------------------ choosing
def _two_tracks(tmp_path):
    return shelf(tmp_path, {
        "hurry.mp3": b"\x00" * 8,
        "hurry.json": json.dumps({"mood": "tense", "seconds": 30}),
        "long-calm.mp3": b"\x00" * 8,
        "long-calm.json": json.dumps({"mood": "calm", "seconds": 300}),
    })


def test_the_asked_for_mood_beats_a_neutral_track(tmp_path):
    shelf(tmp_path, {"n.mp3": b"\x00" * 8, "c.mp3": b"\x00" * 8,
                     "c.json": json.dumps({"mood": "calm"})})
    name, _, why = music.pick(mood="calm")
    assert name == "c" and "calm" in why


def test_a_track_that_does_not_have_to_loop_wins(tmp_path):
    _two_tracks(tmp_path)
    name, _, why = music.pick(mood="calm", seconds=200)
    assert name == "long-calm"
    assert "loop" not in why.split("；")[0] or "long enough" in why


def test_a_shorter_track_says_it_will_loop(tmp_path):
    _two_tracks(tmp_path)
    _, _, why = music.pick(mood="tense", seconds=200)
    assert "loop" in why


def test_a_vocal_track_loses_to_an_instrumental_one_under_a_narrator(tmp_path):
    shelf(tmp_path, {
        "song.mp3": b"\x00" * 8,
        "song.json": json.dumps({"mood": "calm", "tags": ["vocal"], "seconds": 300}),
        "bed.mp3": b"\x00" * 8,
        "bed.json": json.dumps({"mood": "calm", "tags": ["instrumental"], "seconds": 300}),
    })
    name, _, why = music.pick(mood="calm", seconds=200, spoken=True)
    assert name == "bed"
    assert "speaking" in why or "vocals" in why


def test_tags_the_caller_asked_for_beat_tags_they_did_not(tmp_path):
    shelf(tmp_path, {
        "a.mp3": b"\x00" * 8, "a.json": json.dumps({"tags": ["piano"]}),
        "b.mp3": b"\x00" * 8, "b.json": json.dumps({"tags": ["drums"]}),
    })
    name, _, why = music.pick(tags=["piano"])
    assert name == "a" and "piano" in why


# ------------------------------------------------------------------ the ducking windows
def test_windows_cover_only_the_shots_that_actually_speak():
    plan = [{"seconds": 5.0, "voice_seconds": 3.5}, {"seconds": 5.0}, {"seconds": 4.0,
                                                                      "voice_seconds": 2.0}]
    assert assemble._speech_windows(plan, 0.35) == [(0.0, 3.85), (10.0, 12.35)]


def test_a_window_never_runs_past_its_own_shot():
    """A narration longer than its slot was lengthened for it — but if it ever was not, the window
    must not eat into the next shot's music."""
    plan = [{"seconds": 4.0, "voice_seconds": 9.0}]
    assert assemble._speech_windows(plan, 0.35) == [(0.0, 4.0)]


def test_a_film_with_no_narration_has_no_windows():
    assert assemble._speech_windows([{"seconds": 5.0}, {"seconds": 5.0}], 0.35) == []


def test_the_tail_is_what_makes_the_music_come_back_smoothly():
    plan = [{"seconds": 5.0, "voice_seconds": 2.0}]
    assert assemble._speech_windows(plan, 0.0)[0][1] == 2.0
    assert assemble._speech_windows(plan, 1.0)[0][1] == 3.0


# ------------------------------------------------------------------ composing (ACE-Step, via ComfyUI)
def test_the_music_graph_carries_what_was_asked_for():
    g = comfyui.music_graph(tags="calm piano", lyrics="", seconds=45, bpm=90, seed=7)
    assert g["94"]["inputs"]["tags"] == "calm piano"
    assert g["94"]["inputs"]["bpm"] == 90 and g["94"]["inputs"]["seed"] == 7
    # The length has to reach **both** the latent and the encoder: the encoder writes the duration
    # into the conditioning, so changing only one of them produces a track of the wrong length.
    assert g["98"]["inputs"]["seconds"] == 45.0
    assert g["94"]["inputs"]["duration"] == 45.0
    assert g["9"]["class_type"] == "SaveAudioMP3"      # the blueprint leaves saving to its caller


def test_the_graph_loads_both_encoders_because_the_blueprint_loads_both():
    """7.8 GB of the 13.7 GB is a second text encoder through one `DualCLIPLoader` — the obvious
    assumption is that one of them is optional, and it is not."""
    loader = comfyui.music_graph(tags="x")["105"]["inputs"]
    assert loader["clip_name1"].startswith("qwen_0.6b")
    assert loader["clip_name2"].startswith("qwen_4b")
    assert loader["type"] == "ace"


def test_the_graph_wires_the_nodes_the_way_the_blueprint_wires_them():
    g = comfyui.music_graph(tags="x")
    assert g["78"]["inputs"]["model"] == ["104", 0]         # UNET → AuraFlow shift → sampler
    assert g["3"]["inputs"]["positive"] == ["94", 0]
    assert g["3"]["inputs"]["negative"] == ["47", 0]        # zeroed-out, as the blueprint does
    assert g["18"]["inputs"]["vae"] == ["106", 0]
    assert g["9"]["inputs"]["audio"] == ["18", 0]


def test_the_default_tempo_is_an_ordinary_number_not_zero():
    """The node takes an int and the blueprint ships 190; 0 was never shown to mean "let it decide"."""
    assert comfyui.music_graph(tags="x")["94"]["inputs"]["bpm"] == 120


def test_missing_weights_are_named_one_by_one_with_an_address_each():
    schemas = {"TextEncodeAceStepAudio1.5": {},
               "UNETLoader": {"input": {"required": {"unet_name": [["something_else.safetensors"]]}}},
               "DualCLIPLoader": {"input": {"required": {"clip_name1": [[]], "clip_name2": [[]]}}},
               "VAELoader": {"input": {"required": {"vae_name": [[]]}}}}
    miss = comfyui.music_missing(schemas)
    assert len(miss) == 4 and "acestep_v1.5_turbo.safetensors" in miss
    text = comfyui.music_diagnosis(schemas)
    for name in miss:
        assert name in text
    assert "hf-mirror.com" in text            # a file name alone is not an instruction


def test_an_old_comfyui_is_told_that_it_is_old_not_that_a_file_is_missing():
    text = comfyui.music_diagnosis({})
    assert "ACE-Step" in text and "safetensors" not in text


def test_publishing_writes_the_audio_and_says_what_it_is(tmp_path):
    shelf(tmp_path, {})
    dest = music.publish("Night Shift", b"\x00" * 32, seconds=42, mood="calm",
                         tags=["instrumental"], source="test", licence="MIT")
    assert dest.is_file() and dest.suffix == ".mp3"
    row = music.row("Night-Shift")
    assert row is not None and row["seconds"] == 42.0 and row["tags"] == ["instrumental"]
    assert "MIT" in music.describe(row)


def test_publishing_twice_under_one_name_never_overwrites(tmp_path):
    shelf(tmp_path, {})
    a = music.publish("Take", b"first")
    b = music.publish("Take", b"second")
    assert a != b and a.is_file() and b.is_file()
    assert a.read_bytes() == b"first"        # a film already scored with the old take still gets it
    assert music.row("Take") and music.row("Take-2")


def test_publishing_without_a_shelf_says_so(tmp_path):
    music.use_folder(None)
    with pytest.raises(KeyError):
        music.publish("x", b"y")
