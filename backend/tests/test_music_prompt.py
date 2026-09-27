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


# ------------------------------------------------- reading a sentence (the panel's only input)
#
# 2026-09-27：面板上的五台推子撤了，用户只写一句话。所以「读得对不对」成了这个模块的主考项 ——
# 而它之所以能考，是因为 `read()` 是确定的（一张线索表），不是一次模型调用。

def _tags_of(text: str, **kw) -> str:
    d = musicprompt.read(text, **kw)
    tags, _warn = musicprompt.compose_tags(
        genre=d["genre"], mood=d["mood"], instruments=d["instruments"],
        production=d["production"], vocals=d["vocals"], bpm=d["bpm"])
    return tags


def test_a_sentence_comes_back_as_a_well_formed_prompt():
    """一句话进去，出来的是那格子（流派第一、关键词不超上限、BPM 在流派区间里）。"""
    tags = _tags_of("安静一点的钢琴，不要鼓，慢慢铺开，垫在旁白下面")
    first = tags.split(",")[0].strip()
    assert first == "minimal piano", tags
    assert "no vocals" in tags
    assert len(tags.split(",")) <= musicprompt.MAX_TAGS


def test_asking_for_no_drums_really_drops_the_drums():
    """⚠️ 「不要鼓」是**否定**，不是线索 —— 鼓的线索里也有「鼓」字，不做这一步会反着读。"""
    d = musicprompt.read("有节奏一点，但是不要鼓")
    assert "brushed-drums" not in d["instruments"]
    assert "soft-percussion" not in d["instruments"]


def test_a_named_tempo_beats_the_range():
    """用户写了数字就按用户写的；没写才按流派区间推。"""
    assert musicprompt.read("安静的钢琴 92 bpm")["bpm"] == 92
    assert musicprompt.read("安静的钢琴")["bpm"] != 92


def test_slow_and_fast_words_move_the_tempo_inside_the_genres_own_range():
    """位置在区间内挪，而不是写死一个数 —— 写死会让「慢的浩室」变成 70 BPM，正是会糊的那种输入。"""
    lo, hi = musicprompt.bpm_span("tech-house")
    slow = musicprompt.read("浩室，慢一点")["bpm"]
    fast = musicprompt.read("浩室，节奏强烈一点")["bpm"]
    assert lo <= slow < fast <= hi, (slow, fast, lo, hi)


def test_the_description_can_say_who_sings():
    assert musicprompt.read("来一段有女声的")["vocals"] == "soft-female"
    assert musicprompt.read("纯器乐，没人唱")["vocals"] == "instrumental"


def test_the_panel_choice_overrides_the_reading():
    """人声是唯一一个「人的手压过算法」的地方，这一点要明着钉住。"""
    assert musicprompt.read("安静钢琴", vocals="choir")["vocals"] == "choir"


def test_lyrics_next_to_instrumental_is_resolved_out_loud():
    """填了歌词又要纯音乐是自相矛盾的输入 —— 按歌词走，并且**说出来**。"""
    d = musicprompt.read("安静钢琴", vocals="instrumental", lyrics="唱两句")
    assert d["vocals"] != "instrumental"
    assert d["notes"], "改了用户的输入却不说，等于悄悄替换"


def test_an_unreadable_description_says_so():
    """⚠️ 读不出来**不是**装作读懂了：它用默认底子，并把这件事写进 notes。"""
    d = musicprompt.read("随便来点音乐")
    assert d["genre"] == musicprompt.DEFAULT_GENRE
    assert d["notes"], "用了默认底子就必须说"


def test_a_readable_description_does_not_cry_wolf():
    assert musicprompt.read("紧张的暗氛围，讲并发症")["notes"] == []


def test_every_reading_is_a_clean_prompt():
    """⚠️ 这个模块全部的规矩在这里合起来验一遍：随便一句话，读出来的那套词自己必须是干净的
    （没有矛盾标签、不超上限、BPM 与流派对得上）。否则「自动」就成了平庸输出的来源。"""
    sentences = [
        "安静一点的钢琴，不要鼓，慢慢铺开，垫在旁白下面",
        "紧张、悬念，讲并发症的时候用，暗一点",
        "开场三秒要抓住人，有力量感，预告片那种",
        "轻快的科普，面向公众，有点节奏感 100 bpm",
        "温暖的木吉他，像在讲一个病例故事",
        "来一段能唱歌的，要有女声",
        "随便来点音乐",
        "后摇，从平静推到高潮",
        "科技感，器械与影像技术，合成器",
        "lo-fi，放松，像在咖啡馆里学习",
    ]
    for s in sentences:
        d = musicprompt.read(s)
        tags, warn = musicprompt.compose_tags(
            genre=d["genre"], mood=d["mood"], instruments=d["instruments"],
            production=d["production"], vocals=d["vocals"], bpm=d["bpm"])
        assert warn == [], f"「{s}」读出来的词自己就有话说：{warn}"
        assert len(tags.split(",")) <= musicprompt.MAX_TAGS, s
        lo, hi = musicprompt.bpm_span(d["genre"])
        assert lo <= d["bpm"] <= hi, f"「{s}」的 BPM 不在它自己选的那个流派的区间里"


def test_two_instruments_of_the_same_family_are_never_both_used():
    """⚠️ 实测第一版出过 `grand piano, felt piano` 两个钢琴 —— 那不是"更丰富"，
    是两条几乎一样的线索在互相稀释。"""
    d = musicprompt.read("安静一点的钢琴")
    fams = [musicprompt._FAMILY.get(i) for i in d["instruments"]]
    assert len([f for f in fams if f]) == len(set(f for f in fams if f)), d["instruments"]


def test_the_mood_words_match_the_shelfs_closed_vocabulary():
    """⚠️ 一处「同一件事写两份」的守卫。

    `_MOOD_CUES` 的键必须与 `music.MOODS` 一模一样：曲库按那份闭词表校验 `mood`，多一个词
    会让上架被拒，少一个词会让某个情绪永远读不出来 —— 而两处都在各自的文件里看着很对。
    """
    from app import music as shelf
    assert set(musicprompt._MOOD_CUES) == set(shelf.MOODS)


def test_one_reading_never_names_an_instrument_twice():
    for s in ("钢琴和合成器", "大提琴和弦乐，还有低音", "打击乐、鼓，再加一点打击"):
        d = musicprompt.read(s)
        assert len(d["instruments"]) == len(set(d["instruments"])), d["instruments"]
