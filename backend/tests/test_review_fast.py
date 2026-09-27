"""Looking and listening must not cost the group its whole turn.

The complaint was concrete: `review_picture` and `review_audio` are too slow, and everyone should be
able to see the result quickly. It turned out to be four separate things, each measured on this
machine before it was touched, and each pinned here:

  * the app reached for the **heaviest** model it could see — 46.3 s for one picture against 7.1 s
    for a `speed`-flagged one on the same picture with the same question, and a single model call is
    capped at `request_timeout` (60 s), so "slow" was one bad moment away from being "failed";
  * a 1080×1920 PNG is under the byte limit and was therefore sent whole, although the endpoint cuts
    it to 1568 px anyway — 646 KB became 145 KB and the same look came back 20% sooner;
  * the answer was never written down, so the second member to look at the same picture paid again,
    and eight frames were eight calls;
  * transcription takes about 12 s before it reads a word here, and it was run again for every
    question, over the whole file.

What must NOT be given up for speed is here too: a picture that changed is looked at again, `fresh`
still means fresh, a cache miss is never dressed up as a fresh look, and the file that was reviewed
travels back with the answer so a person can look at it themselves.
"""

from __future__ import annotations

import asyncio
import pathlib
import time

from app import attachments, ffmpeg, vision
from tests.conftest import FakeLLM
from tests.test_collab import setup


def _png(path, colour=(200, 60, 60), size=(120, 90)):
    from PIL import Image
    Image.new("RGB", size, colour).save(path)
    return path


def _calls_to(fake: FakeLLM, needle: str = "") -> list:
    """The vision calls this fake actually served — the count is the whole point of the file."""
    out = []
    for model, messages in fake.calls:
        blob = repr(messages)
        if "image_url" in blob and (not needle or needle in blob):
            out.append(model)
    return out


def _look(orch, ctx, args):
    return asyncio.run(orch.toolhub.call(ctx, "review_picture", args, None))


# --------------------------------------------------------------- which model looks
def _gateway(store, heavy_first: bool = True):
    """A cloud gateway whose pool leads with a heavyweight, exactly as this machine's did."""
    prov = store.add_provider("MetaChat", "openai", "https://api.metachat.example/v1", "sk-x")
    names = ("claude-fable-5", "gemini-3.1-flash-lite") if heavy_first else \
            ("gemini-3.1-flash-lite", "claude-fable-5")
    made = {}
    for n in names:
        made[n] = store.add_model(prov["id"], n)
    for m in store.list_models():
        store.update_model(m["id"], {"enabled": True, "strengths": ["multimodal"]})
    store.update_model(made["gemini-3.1-flash-lite"]["id"],
                       {"strengths": ["multimodal", "speed", "low-cost"]})
    store.update_settings({"vision_cloud": True})
    return made


def test_automatic_looking_reaches_for_the_quick_model_not_the_pool_order(store, make_router):
    """Looking is reading, not reasoning: a small model answers "is the tube round" as well as a
    large one, seven times sooner, and that is the group's whole turn it is spending."""
    st = store
    made = _gateway(st)
    pool = [m for m in st.list_models() if m["id"] in (made["claude-fable-5"]["id"],
                                                       made["gemini-3.1-flash-lite"]["id"])]
    assert [m["model_name"] for m in pool][0] == "claude-fable-5", "the heavy one really is first"

    cfg = st.get_settings()
    chose = vision.automatic(pool, cfg)
    assert chose and chose["model_name"] == "gemini-3.1-flash-lite", chose

    # The settings page recommends what `automatic` would choose — one definition of "which model",
    # read twice — rather than a second opinion that could disagree with what actually runs.
    router = make_router(FakeLLM())
    st_page = vision.status(st, router)
    assert st_page["recommended_id"] == \
        vision.automatic(router.usable_models(), cfg, allow_cloud=True)["id"]
    # Quickness did not become a reason to leave the machine: a local model still wins over any cloud
    # one, which is the rule this ordering sits behind rather than replaces.
    assert st_page["recommended_local"] is True

    # And being quick does not loosen the outbound switch: cloud vision off means no cloud model,
    # however brisk it is.
    assert vision.automatic(pool, {**cfg, "vision_cloud": False}) is None


def test_a_user_who_names_the_heavy_model_still_gets_it(store, make_router):
    """Speed is the *automatic* choice. A user who picked a model for this job outranks it, the same
    way they outrank the automatic choice today."""
    st = store
    made = _gateway(st)
    heavy = made["claude-fable-5"]
    st.update_settings({"vision_model_id": heavy["id"]})

    assert vision.pick(st, st.list_models())["model_name"] == "claude-fable-5"
    assert made["gemini-3.1-flash-lite"]["id"]   # the quick one exists, and was not what was asked for


# --------------------------------------------------------------- what gets sent
def test_a_picture_is_cut_down_by_pixels_and_not_only_by_bytes():
    """Only one of the two ways a picture is too big used to be checked. The bytes are the upload;
    the pixels are the wait, and the endpoint cuts them to its own ceiling anyway."""
    from PIL import Image
    import io

    big = io.BytesIO()
    Image.new("RGB", (1080, 1920), (10, 20, 30)).save(big, format="PNG")
    raw = big.getvalue()
    assert len(raw) < 8 * 1024 * 1024, "the case being pinned: small enough to be sent whole"

    mime, cut = attachments.shrink_image(raw, "image/png", 8 * 1024 * 1024)
    assert mime == "image/jpeg" and len(cut) < len(raw)
    assert attachments.longest_side(cut) <= attachments.LONGEST_SIDE

    # Already small in both senses: handed on untouched, so nothing is re-encoded for no reason.
    small = io.BytesIO()
    Image.new("RGB", (320, 200), (10, 20, 30)).save(small, format="JPEG", quality=90)
    tiny = small.getvalue()
    assert attachments.shrink_image(tiny, "image/jpeg", 8 * 1024 * 1024) == ("image/jpeg", tiny)

    # …and not-an-image is left alone rather than chewed on by Pillow.
    assert attachments.shrink_image(b"\x00\x01", "application/pdf", 8) == ("application/pdf", b"\x00\x01")


# --------------------------------------------------------------- one look, everyone reads it
def test_the_second_member_to_look_at_the_same_picture_pays_nothing(store, make_router):
    """The group works by several members checking the same thing. Each of them used to buy its own
    model call for the identical question about the identical bytes."""
    st = store
    st.add_model("ollama", "qwen3.5:9b")
    fake = FakeLLM(script={"ollama": "一根淡蓝色的直管,管壁均匀。"}, default="好")
    orch, g = setup(st, make_router, fake)
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    _png(pathlib.Path(st.workspace_dir(g["id"])) / "shot.png")

    first = _look(orch, ctx, {"path": "shot.png", "question": "管子是圆的吗?"})
    second = _look(orch, ctx, {"path": "shot.png", "question": "管子是圆的吗?"})

    assert first.ok and second.ok, first.text + second.text
    assert len(_calls_to(fake)) == 1, [m for m, _ in fake.calls]
    assert "淡蓝色的直管" in second.text, "the remembered answer is what comes back"
    # The reader has to be told it is a memory, and how old, or it reads as a fresh look.
    assert "already looked at" in second.text and "no model call was made" in second.text


def test_a_picture_that_changed_is_looked_at_again(store, make_router):
    """The one risk a memory creates: a member fixing what the review complained about and then
    reading the old verdict. The key is the file's own bytes, so a changed picture is a new look."""
    st = store
    st.add_model("ollama", "qwen3.5:9b")
    fake = FakeLLM(script={"ollama": "还是那根管子。"}, default="好")
    orch, g = setup(st, make_router, fake)
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    ws = pathlib.Path(st.workspace_dir(g["id"]))
    shot = _png(ws / "shot.png")
    args = {"path": "shot.png", "question": "管子是圆的吗?"}

    assert _look(orch, ctx, args).ok
    time.sleep(1.1)                                   # mtime has one-second resolution
    _png(shot, colour=(30, 30, 200))
    out = _look(orch, ctx, args)

    assert out.ok and len(_calls_to(fake)) == 2, "a changed picture gets a real look"
    assert "already looked at" not in out.text


def test_fresh_means_fresh(store, make_router):
    """After a fix a member needs to know what the picture looks like *now*, even at the same bytes."""
    st = store
    st.add_model("ollama", "qwen3.5:9b")
    fake = FakeLLM(script={"ollama": "看过了。"}, default="好")
    orch, g = setup(st, make_router, fake)
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    _png(pathlib.Path(st.workspace_dir(g["id"])) / "shot.png")

    _look(orch, ctx, {"path": "shot.png"})
    out = _look(orch, ctx, {"path": "shot.png", "fresh": True})

    assert out.ok and len(_calls_to(fake)) == 2
    assert "already looked at" not in out.text


# --------------------------------------------------------------- a set of frames, one pass
def test_a_set_of_frames_is_one_model_call_and_not_one_each(store, make_router):
    """Eight frames were eight calls: eight waits, and eight answers a reader has to stitch together.
    The pictures go in together, numbered, so the notes can be placed back on the files they are
    about."""
    st = store
    st.add_model("ollama", "qwen3.5:9b")
    seen_prompts: list[str] = []

    def spy(messages):
        seen_prompts.append(repr(messages))
        return "#1 = \"f1.png\": 一根管子。#2 = \"f2.png\": 一根管子。#3 = \"f3.png\": 一根管子。"

    fake = FakeLLM(script={"ollama": spy}, default="好")
    orch, g = setup(st, make_router, fake)
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    ws = pathlib.Path(st.workspace_dir(g["id"]))
    for name in ("f1.png", "f2.png", "f3.png"):
        _png(ws / name)

    out = asyncio.run(orch.toolhub.call(
        ctx, "review_picture", {"paths": ["f1.png", "f2.png", "f3.png"], "question": "都是圆的吗?"}, None))

    assert out.ok, out.text
    assert len(_calls_to(fake)) == 1, "three pictures, one call"
    assert seen_prompts and seen_prompts[0].count("'type': 'image_url'") == 3, "all three were attached"
    # Each picture is named in the prompt, in the order it was sent, or the answer is unplaceable.
    assert "#1 = " in seen_prompts[0] and "#3 = " in seen_prompts[0]
    # …and mixing remembered and new keeps both, labelled, without re-looking at the remembered one.
    again = asyncio.run(orch.toolhub.call(
        ctx, "review_picture", {"paths": ["f1.png", "f2.png"], "question": "都是圆的吗?"}, None))
    assert again.ok and "already looked at" in again.text


# --------------------------------------------------------------- hearing
def test_a_recording_is_transcribed_once(store, make_router, monkeypatch):
    """Transcription is a local model that takes ~12 s to start before it reads a word, and the same
    recording gets asked about repeatedly — "does the narration match the script" is asked per shot."""
    st = store
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    ws = pathlib.Path(st.workspace_dir(g["id"]))
    (ws / "narration.wav").write_bytes(b"\x00" * 64)
    runs: list[str] = []

    def counted(path, settings=None, **kw):
        runs.append(str(path))
        return ("这是旁白的第一句。", "")

    monkeypatch.setattr(attachments, "transcriber", lambda settings=None: ("whisper", []))
    monkeypatch.setattr(type(orch.toolhub), "_measure_sound", lambda self, t: (12.0, True))
    monkeypatch.setattr(attachments, "transcribe_with_reason", counted)

    first = asyncio.run(orch.toolhub.call(ctx, "review_audio", {"path": "narration.wav"}, None))
    second = asyncio.run(orch.toolhub.call(ctx, "review_audio", {"path": "narration.wav"}, None))

    assert first.ok and second.ok, first.text + second.text
    assert len(runs) == 1, runs
    assert "这是旁白的第一句。" in second.text
    assert "already read it" in second.text and "was not run again" in second.text


def test_a_long_recording_can_be_heard_one_window_at_a_time(store, make_router, monkeypatch):
    """Checking one line of a ten-minute narration should not mean reading all ten minutes again —
    and the window is a different fact about the file, so it is remembered on its own."""
    st = store
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    ws = pathlib.Path(st.workspace_dir(g["id"]))
    if not ffmpeg.available()[0]:
        import pytest
        pytest.skip("no ffmpeg here to cut a window out with")
    made = ws / "long.wav"
    code, detail = ffmpeg.run([ffmpeg.path(), "-hide_banner", "-loglevel", "error", "-y",
                               "-f", "lavfi", "-i", "sine=frequency=440:duration=6", str(made)], 60)
    assert code == 0 and made.is_file(), detail
    heard: list[tuple[str, float]] = []

    def counted(path, settings=None, **kw):
        heard.append((pathlib.Path(path).name, pathlib.Path(path).stat().st_size))
        return ("第 2 秒开始的这一句。", "")

    monkeypatch.setattr(attachments, "transcriber", lambda settings=None: ("whisper", []))
    monkeypatch.setattr(attachments, "transcribe_with_reason", counted)

    out = asyncio.run(orch.toolhub.call(
        ctx, "review_audio", {"path": "long.wav", "start_seconds": 2, "seconds": 1}, None))

    assert out.ok, out.text
    assert len(heard) == 1 and heard[0][0].startswith("ta-hear-"), heard
    assert heard[0][1] < made.stat().st_size, "only the window was handed to the transcriber"
    assert "Only 2" in out.text, "the reader has to know only part of it was heard"

    # The same window again is remembered; a *different* window is a different reading.
    asyncio.run(orch.toolhub.call(ctx, "review_audio",
                                 {"path": "long.wav", "start_seconds": 2, "seconds": 1}, None))
    assert len(heard) == 1, heard
    asyncio.run(orch.toolhub.call(ctx, "review_audio",
                                 {"path": "long.wav", "start_seconds": 4, "seconds": 1}, None))
    assert len(heard) == 2, heard
    # …and a window of zero is a member who mixed two numbers up, not a reason to read the whole file.
    bad = asyncio.run(orch.toolhub.call(
        ctx, "review_audio", {"path": "long.wav", "seconds": 0}, None))
    assert not bad.ok and "seconds" in bad.text


# --------------------------------------------------------------- and the person can look too
def test_the_file_that_was_reviewed_travels_back_with_the_answer(store, make_router, monkeypatch):
    """A review used to be a paragraph. The user then had to take the model's word for a picture
    sitting in their own workspace, or go and find it. The file comes back with the answer."""
    st = store
    st.add_model("ollama", "qwen3.5:9b")
    orch, g = setup(st, make_router, FakeLLM(script={"ollama": "一根管子。"}, default="好"))
    ctx = asyncio.run(orch.toolhub.context(st.get_group(g["id"]), st.list_agents()[0], connect=False))
    ws = pathlib.Path(st.workspace_dir(g["id"]))
    _png(ws / "figures" / "x.png", size=(60, 40)) if (ws / "figures").is_dir() else None
    (ws / "figures").mkdir(exist_ok=True)
    _png(ws / "figures" / "x.png", size=(60, 40))

    shot = _look(orch, ctx, {"path": "figures/x.png"})
    assert shot.ok and shot.files, shot.text
    assert shot.files[0]["kind"] == "image" and shot.files[0]["name"] == "figures/x.png"
    # `where` is what tells the chat to fetch it as a workspace path rather than out of the
    # generator's own folder — the two go through different routes.
    assert shot.files[0]["where"] == "workspace"

    (ws / "take.wav").write_bytes(b"\x00" * 64)
    monkeypatch.setattr(attachments, "transcriber", lambda settings=None: ("whisper", []))
    monkeypatch.setattr(type(orch.toolhub), "_measure_sound", lambda self, t: (3.0, True))
    monkeypatch.setattr(attachments, "transcribe_with_reason", lambda p, s=None, **kw: ("你好。", ""))
    heard = asyncio.run(orch.toolhub.call(ctx, "review_audio", {"path": "take.wav"}, None))
    assert heard.ok and heard.files and heard.files[0]["kind"] == "audio", heard.files
    assert heard.files[0]["where"] == "workspace"
