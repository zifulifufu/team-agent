"""The vocabulary's rules, held to the official ACE-Step guidance.

What is worth a test here is not "the function returns a string" but the five things a free text box
cannot guarantee: genre first, concrete words, BPM inside the genre's own range, no contradictory
pair, and not so many keywords that they dilute each other. The last test in this file is the one
that matters most day to day — every preset the page offers must itself be clean, because a preset
is the thing a user is most likely to press without reading.
"""
from __future__ import annotations

from app import musicprompt


def test_genre_leads_the_tag_string():
    """Genre anchors everything else, so it has to be first — not "somewhere in the list"."""
    tags, warn = musicprompt.compose_tags(genre="minimal-piano", mood="calm",
                                          instruments=["felt-piano"], bpm=72)
    assert tags.split(",")[0].strip() == "minimal piano"
    assert warn == []


def test_instruments_are_concrete_names_not_adjectives():
    """`felt piano` renders; `sophisticated` has no sound to render."""
    tags, _ = musicprompt.compose_tags(genre="ambient", instruments=["felt-piano", "drone"])
    assert "felt piano" in tags and "evolving drone" in tags


def test_bpm_travels_both_in_the_tags_and_as_a_parameter():
    """Official guidance: write it in the tags *and* pass it as `bpm`, so the two agree."""
    tags, _ = musicprompt.compose_tags(genre="tech-house", bpm=122)
    assert "122 bpm" in tags
    assert musicprompt.bpm_span("tech-house") == (118, 126)


def test_a_tempo_outside_the_genres_range_is_warned_about():
    """Techno at 70 makes the model swing between two poles instead of committing."""
    _, warn = musicprompt.compose_tags(genre="tech-house", bpm=70)
    assert warn, "an out-of-range BPM has to be said out loud, not silently sent"


def test_a_tempo_inside_the_range_is_not_warned_about():
    _, warn = musicprompt.compose_tags(genre="tech-house", bpm=122)
    assert warn == []


def test_contradictory_moods_are_flagged():
    """`epic` is in the mood vocabulary, so it arrives through free text rather than the selector —
    and calm-next-to-epic is exactly the oscillation the guidance warns about."""
    _, warn = musicprompt.compose_tags(genre="cinematic-orchestral", mood="calm",
                                       extra="epic, aggressive", bpm=80)
    assert any("oscillate" in w or "摇摆" in w for w in warn)


def test_a_calm_trailer_is_not_called_contradictory():
    """⚠️ The other half of that rule: a quiet trailer cue is a real thing, and flagging it would
    teach the user to ignore warnings."""
    _, warn = musicprompt.compose_tags(genre="trailer", mood="calm", bpm=120)
    assert warn == []


def test_lofi_and_hifi_together_are_flagged():
    _, warn = musicprompt.compose_tags(genre="lo-fi-hiphop", production=["dusty", "clean"], bpm=80)
    assert any("lo-fi" in w.lower() or "Lo-fi" in w for w in warn)


def test_too_many_keywords_are_flagged():
    many = [g["id"] for g in musicprompt.GENRES] + [i["id"] for i in musicprompt.INSTRUMENTS]
    tags, warn = musicprompt.compose_tags(genre="ambient", instruments=many)
    assert warn, "past ~12 keywords they dilute each other and the user cannot tell"
    assert len(tags.split(",")) > musicprompt.MAX_TAGS


def test_an_unlisted_genre_is_kept_but_said_out_loud():
    """Free text is allowed — the model may still know it. Passing it silently would be the bug."""
    tags, warn = musicprompt.compose_tags(genre="gagaku court music", bpm=60)
    assert "gagaku court music" in tags
    assert warn


def test_no_genre_and_no_free_text_is_warned_about():
    _, warn = musicprompt.compose_tags(mood="neutral", bpm=120)
    assert any("genre" in w.lower() or "流派" in w for w in warn)


def test_every_preset_is_itself_clean():
    """⚠️ The preset is what gets pressed without being read.

    A preset that trips its own warnings is a preset that ships the exact problem this module was
    written to prevent — so each one is composed here and has to come back with nothing to say.
    """
    for p in musicprompt.PRESETS:
        tags, warn = musicprompt.compose_tags(
            genre=p["genre"], mood=p["mood"], instruments=p["instruments"],
            production=p["production"], vocals=p["vocals"], bpm=p["bpm"])
        assert warn == [], f"preset {p['id']} trips its own warnings: {warn}"
        assert len(tags.split(",")) <= musicprompt.MAX_TAGS, f"preset {p['id']} is too long"
        assert tags.split(",")[0].strip(), f"preset {p['id']} has no genre"


def test_the_vocabulary_is_complete_enough_to_draw_the_page():
    v = musicprompt.vocabulary()
    for key in ("genres", "instruments", "production", "vocals", "presets"):
        assert v[key], f"{key} is empty, so that column of the page would be blank"
    for g in v["genres"]:
        assert g["id"] and g["label"] and "bpm" in g, "every genre needs a label and a BPM range"
    for p in v["presets"]:
        assert {"id", "label", "genre", "mood", "bpm"} <= set(p), f"preset {p.get('id')} is incomplete"
