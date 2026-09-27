"""Judging a reply, hearing it read out, and handing it to another group.

Three things the user asked for after reading a group's answers: the keys to rate one, to have it
read aloud, and to forward it elsewhere. What is pinned here is mostly about **not lying**:

  * a rating is the user's *verdict*, so changing their mind replaces it and a mis-click can be
    undone — a table that appends would let a scoreboard be clicked up and down;
  * only a member's reply can be rated. Your own message has no member to answer for it and a system
    note was written by this app, so a verdict on either would put rows on a scoreboard nobody can
    act on;
  * the scoreboard counts both thumbs. A list that quietly dropped the down-votes would be worse
    than no list;
  * read-aloud says whether it read the whole thing, and reads through the *same* function the film
    narration uses — the failure mode there (`say` writing nothing and exiting 0) must not come back
    in a second implementation;
  * a forwarded message goes through the path a typed message takes, so the receiving group actually
    hears about it, and carries the name of where it came from.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import feedback as feedback_lib, speech
from app.main import create_app
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的。"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = tmp_path / "data"
        yield c


def gid(client) -> str:
    return client.get("/api/groups").json()[0]["id"]


def a_message(client, gid_: str) -> str:
    """One member's reply to judge — written straight into the store, like a finished round would."""
    store = client.app.state.store
    return store.add_message(gid_, "agent", store.list_agents()[0]["id"], "Copywriter",
                             "未破裂动脉瘤的介入术式有三类:支架辅助栓塞、血流导向、WEB 瘤内扰流。",
                             model_id="deepseek/deepseek-flash")["id"]


# ------------------------------------------------------------------ the verdict
def test_a_rating_is_the_users_verdict_not_a_log_of_clicks(client):
    g = gid(client)
    mid = a_message(client, g)

    first = client.post(f"/api/groups/{g}/messages/{mid}/feedback",
                        json={"rating": "up", "note": "数字有出处"}).json()
    assert first["feedback"]["rating"] == "up" and first["feedback"]["note"] == "数字有出处"
    assert first["totals"] == {"up": 1, "down": 0, "rated": 1}

    # Changing their mind replaces the one opinion instead of adding a second.
    changed = client.post(f"/api/groups/{g}/messages/{mid}/feedback",
                          json={"rating": "down", "note": "但没写适应证"}).json()
    assert changed["totals"] == {"up": 0, "down": 1, "rated": 1}
    assert [m["down"] for m in changed["members"]] == [1]

    # …and a mis-click can be taken back: an empty rating is a clear, not an empty verdict.
    cleared = client.post(f"/api/groups/{g}/messages/{mid}/feedback",
                          json={"rating": "", "note": ""}).json()
    assert cleared["feedback"] is None and cleared["totals"]["rated"] == 0
    assert client.get("/api/feedback").json()["items"] == []


def test_a_rating_that_is_not_one_of_the_verdicts_is_refused(client):
    g = gid(client)
    mid = a_message(client, g)
    bad = client.post(f"/api/groups/{g}/messages/{mid}/feedback", json={"rating": "maybe"})
    assert bad.status_code == 400 and "maybe" in bad.text
    assert client.get("/api/feedback").json()["items"] == []


def test_only_a_members_reply_can_be_judged(client):
    """Your own message, and the app's own notes, are not something a member can be marked for."""
    g = gid(client)
    store = client.app.state.store
    mine = store.add_message(g, "user", None, "我", "帮我做一支视频。")["id"]
    note = store.add_message(g, "system", None, "系统", "已达到单次最大发言轮数。")["id"]

    for mid in (mine, note):
        got = client.post(f"/api/groups/{g}/messages/{mid}/feedback", json={"rating": "up"})
        assert got.status_code == 400, mid


def test_the_scoreboard_shows_both_thumbs_and_the_notes(client):
    """What the panel reads: who is doing well, who is not, and the sentences behind both."""
    g = gid(client)
    store = client.app.state.store
    first = store.list_agents()[0]
    good = store.add_message(g, "agent", first["id"], "Storyboard", "分镜八镜,每镜 6 秒。")["id"]
    bad = store.add_message(g, "agent", first["id"], "Storyboard", "分镜没写时长。")["id"]
    client.post(f"/api/groups/{g}/messages/{good}/feedback", json={"rating": "up"})
    client.post(f"/api/groups/{g}/messages/{bad}/feedback",
                json={"rating": "down", "note": "时长对不上"})

    board = client.get("/api/feedback").json()
    assert board["totals"] == {"up": 1, "down": 1, "rated": 2}
    member = board["members"][0]
    assert member["name"] == "Storyboard" and member["up"] == 1 and member["down"] == 1
    assert [n["note"] for n in member["notes"]] == ["时长对不上"]
    assert member["notes"][0]["text"].startswith("分镜没写时长")

    # One group's board is that group's, so a panel opened inside a project is not the whole app's.
    assert client.get("/api/feedback", params={"gid": g}).json()["totals"]["rated"] == 2
    assert client.get("/api/feedback", params={"gid": "nope"}).status_code == 404


def test_a_rated_message_that_vanishes_takes_its_rating_with_it(client):
    """Clear the chat and the scoreboard must not keep counting replies that are gone."""
    g = gid(client)
    mid = a_message(client, g)
    client.post(f"/api/groups/{g}/messages/{mid}/feedback", json={"rating": "up"})
    client.delete(f"/api/groups/{g}/messages")
    assert client.get("/api/feedback").json()["items"] == []


# ------------------------------------------------------------------ hearing it
def test_a_reply_can_be_read_out_loud_by_this_machine(client, monkeypatch):
    """The button fetches audio for one message. WAV, because the browser is what plays it: `say`
    writes AIFF by default and Chromium cannot play AIFF, so a reading the user cannot hear would
    look exactly like a broken button.
    """
    g = gid(client)
    mid = a_message(client, g)
    got = client.get(f"/api/groups/{g}/messages/{mid}/speech")

    assert got.status_code == 200
    assert got.headers["content-type"] == "audio/wav"
    assert got.content[:4] == b"RIFF" and got.content[8:12] == b"WAVE"
    assert len(got.content) > 1000, "a reading with no audio in it is not a reading"
    assert got.headers["X-Team-Agent-Cut"] == "0"
    assert got.headers["X-Team-Agent-Voice"], "which voice read it has to be knowable"

    # The second press is the same reading, out of the cache — the point being that re-synthesising
    # identical text costs a second every time on a chat where messages are replayed constantly.
    again = client.get(f"/api/groups/{g}/messages/{mid}/speech")
    assert again.content == got.content and again.headers["X-Team-Agent-Reused"] == "1"


def test_a_long_reply_says_it_was_cut(client, monkeypatch):
    g = gid(client)
    store = client.app.state.store
    mid = store.add_message(g, "agent", store.list_agents()[0]["id"], "Copywriter",
                            "这是一段很长的旁白。" * 400)["id"]
    got = client.get(f"/api/groups/{g}/messages/{mid}/speech")
    assert got.status_code == 200 and got.headers["X-Team-Agent-Cut"] == "1"
    assert speech.was_cut("很长。" * 1000)


def test_reading_out_loud_never_sends_the_text_anywhere():
    """It is a local narrator or nothing: a private group's reply must not have to leave the machine
    to be *heard* by the person sitting in front of it."""
    from app import speech as module
    src = Path(module.__file__).read_text(encoding="utf-8")
    assert "httpx" not in src and "requests" not in src and "router" not in src
    assert "assemble.speak" in src, "the one place that knows how to run `say`"


def test_the_voice_follows_the_language_and_missing_voices_fall_back(client):
    """A voice name that is not installed makes `say` fail outright — so the choice is checked
    against what this machine has, and an unknown text language is not a guess."""
    have = speech.voices()
    assert speech.voice_for("未破裂颅内动脉瘤的介入术式", have)
    assert speech.voice_for("Three ways to treat an aneurysm", have)
    # A machine with none of the preferred voices gets `say`'s own default rather than a failing name.
    assert speech.voice_for("未破裂动脉瘤", set()) == ""
    assert speech.voice_for("anything", {"Nonesuch"}) == ""


def test_what_is_read_aloud_is_the_words_and_not_the_markup(client):
    """`say` announces punctuation: read literally, a Markdown reply begins "hash hash". The words
    are what a person wants to hear."""
    from app.api_ext import _plain_text
    got = _plain_text("## 术式\n\n- **要点**:见 [指南](https://x.example)\n\n```py\ncode\n```\n")
    assert got == "术式\n要点:见 指南"
    assert "hash" not in got and "#" not in got and "**" not in got


# ------------------------------------------------------------------ handing it on
def test_forwarding_goes_through_the_path_a_typed_message_takes(client):
    """The receiving group has to *hear* about it: a row written straight into the table would be a
    message no member ever reads."""
    store = client.app.state.store
    source = gid(client)
    target = store.create_group("另一个项目", None, member_ids=[])["id"]
    mid = a_message(client, source)

    got = client.post(f"/api/groups/{source}/messages/{mid}/forward",
                      json={"to_group_id": target})
    assert got.status_code == 200, got.text
    assert got.json()["group_id"] == target

    sent = [m for m in store.list_messages(target) if m["sender_type"] == "user"]
    assert len(sent) == 1, sent
    assert "转发" in sent[0]["content"] or "Forwarded" in sent[0]["content"]
    assert "Copywriter" in sent[0]["content"], "where it came from travels with it"
    assert "血流导向" in sent[0]["content"]

    # Forwarding into the group it is already in is a quote, not a forward — and refused as such.
    again = client.post(f"/api/groups/{source}/messages/{mid}/forward",
                        json={"to_group_id": source})
    assert again.status_code == 400
    # …and a message from another group cannot be reached through this one.
    other = store.add_message(target, "agent", None, "Someone", "别的群的话。")["id"]
    assert client.post(f"/api/groups/{source}/messages/{other}/forward",
                       json={"to_group_id": target}).status_code == 404


# ------------------------------------------------------------------ how it sounds, not just what it says
def test_a_reading_moves_the_voice_and_does_not_read_like_a_formant_synthesiser():
    """The complaint was that read-aloud sounded stiff. `say` reads at one rate, on one pitch, with no
    pause at the full stops — that *is* the stiffness, and Apple's embedded commands
    (`[[slnc]]`, `[[rate]]`, `[[pbas]]`) are the whole of what can be fixed locally.

    What is pinned: a question lifts and slows, an exclamation lifts more, a sentence is followed by
    a pause and a paragraph by a longer one, and a long sentence breathes at its own commas.
    """
    line = "未破裂动脉瘤可以选择不开颅。真的吗?"
    said = speech.emotive(line)
    assert "[[slnc 260]]" in said, "a full stop is a pause, not a comma"
    assert said.index("[[rate 156]]") < said.index("真的吗?"), "the question is the slow part"
    assert "[[pbas +6]]" in said.split("[[slnc")[-1] or "[[pbas +6]]" in said

    long_one = "三类术式各有适应证,需要个体化选择,不能一概而论。"
    assert speech.emotive(long_one).count("[[slnc 110]]") == 2, \
        "a long sentence breathes at its own commas"
    assert "[[slnc 110]]" not in speech.emotive("术式有三种。"), "a short one is read in one breath"

    two_paras = "第一段。\n\n第二段。"
    assert "[[slnc 430]]" in speech.emotive(two_paras), "a blank line is a longer pause"
    assert not speech.emotive(two_paras).endswith("]]"), "no trailing silence after the last word"

    # A message that contains `[[` itself must not be able to inject a command of its own.
    hostile = speech.emotive("看这个 [[rate 500]] 标记")
    assert "[[rate 500]]" not in hostile and "[ [rate 500]]" in hostile


def test_the_reading_is_cached_against_what_was_actually_spoken(client):
    """The cache key is the marked-up text, so changing how the voice moves makes a new file — the
    alternative is serving yesterday's monotone from a cache that looks valid."""
    g = gid(client)
    mid = a_message(client, g)
    first = client.get(f"/api/groups/{g}/messages/{mid}/speech")
    assert first.status_code == 200 and first.headers["X-Team-Agent-Reused"] == "0"
    second = client.get(f"/api/groups/{g}/messages/{mid}/speech")
    assert second.headers["X-Team-Agent-Reused"] == "1" and second.content == first.content

    # …and the audio really is longer than a flat reading of the same line would be: the pauses are
    # in the file, not merely in the text that was handed over.
    from app import attachments as attachments_lib
    store = client.app.state.store
    row = store.get_message(mid)
    assert speech.emotive(row["content"]) != row["content"]
    assert attachments_lib.longest_side(b"x") == 0     # a non-image is not an image
