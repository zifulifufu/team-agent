"""Assembling a long video out of what a group already has.

The part that matters is not that ffmpeg runs — it is that the *timing* comes out the way a film
has to be timed: the narration sets the pace, a target length is either met or reported honestly,
and the words land where they can be read rather than where the code happened to put them.
"""

from __future__ import annotations

import pathlib

import pytest

from app import assemble
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_collab import setup

needs_ffmpeg = pytest.mark.skipif(not assemble.available()[0],
                                  reason="assembling needs ffmpeg and ffprobe on this machine")


def ws(tmp_path) -> pathlib.Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    return root


def tiny_clip(workspace: pathlib.Path, name: str = "clip.mp4", seconds: int = 2) -> pathlib.Path:
    """A real clip, made here rather than committed: the assembler is supposed to work on whatever
    the group has, so the test brings something of the same kind rather than a fixture."""
    out = workspace / name
    ff = assemble.ffmpeg_path()
    code, detail = assemble._run([ff, "-hide_banner", "-loglevel", "error", "-y",
                                  "-f", "lavfi", "-i", f"color=c=navy:s=320x240:d={seconds}:r=24",
                                  "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                                  "-shortest", str(out)], 120)
    assert code == 0 and out.is_file(), detail
    return out


# ------------------------------------------------------------------ how long each shot runs
@needs_ffmpeg
def test_reviewed_audio_is_reused_without_tts_and_keeps_its_real_length(tmp_path, monkeypatch):
    import asyncio
    import math
    import struct
    import wave

    root = ws(tmp_path)
    with wave.open(str(root / "reviewed.wav"), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(i * 440 * 2 * math.pi / 24000)))
                                 for i in range(48000)))

    async def no_tts(*args, **kwargs):
        raise AssertionError("a reviewed recording must never be synthesized again")
    monkeypatch.setattr(assemble, "speak", no_tts)
    shot = {"title": "已核验声音", "audio_file": "reviewed.wav", "audio_start": 0.5,
            "audio_duration": 1.5, "seconds": 1, "say": "原稿仅作字幕"}
    out = asyncio.run(assemble.render(root, [shot], size="270x480", name="reuse", burn=False))
    assert out["narrated"] == 1
    assert out["plan"][0]["voice_seconds"] == 1.5
    assert out["seconds"] >= 1.5
    assert assemble.probe(pathlib.Path(out["path"]))["audio"]
    code, volume = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-i", out["path"],
                                  "-af", "volumedetect", "-f", "null", "-"], 60)
    assert code == 0 and "mean_volume: -inf" not in volume and "mean_volume:" in volume
    for patch in ({"audio_start": -1}, {"audio_start": 2}, {"audio_duration": 3},
                  {"audio_duration": float("nan")}, {"audio": "silent"},
                  {"audio_file": "../outside.wav"}, {"audio_file": "/etc/hosts"}):
        with pytest.raises(assemble.AssembleError):
            assemble.make_plan([{**shot, **patch}], workspace=root)
    plan = assemble.make_plan([{"title": "不用再提供台词", "audio_file": "reviewed.wav"}], workspace=root)
    assert plan[0]["audio"] == "voice" and plan[0]["seconds"] == 2


def test_a_shot_runs_as_long_as_its_narration_needs(tmp_path):
    """Nobody writes a length for every shot, and a shot timed to nothing is a guess. The narration
    is the one thing whose length is knowable before anything is rendered, so it sets the pace —
    this estimate is then replaced by the real recording when it exists."""
    root = ws(tmp_path)
    plan = assemble.make_plan(
        [{"title": "片头", "seconds": 3},
         {"title": "第二镜", "say": "这是一段大约二十个字的旁白,用来决定这个镜头应该有多长。"},
         {"title": "第三镜", "seconds": 2, "say": "旁白很短。"}],
        workspace=root)
    assert plan[0]["seconds"] == 3 and plan[0]["why"] == "asked"
    assert plan[1]["why"] == "narration" and plan[1]["seconds"] > 5
    # An explicit length is the author's call at planning time; the recording overrules it later
    # only if the voice really does not fit (see the render test below).
    assert plan[2]["seconds"] == 2 and plan[2]["why"] == "asked"


def test_a_card_holds_the_length_it_is_given_or_a_default(tmp_path):
    root = ws(tmp_path)
    plan = assemble.make_plan([{"title": "片头", "subtitle": "副标题", "seconds": 3},
                               {"title": "只有静帧"}], workspace=root)
    assert plan[0]["seconds"] == 3 and plan[0]["kind"] == "card"
    assert plan[1]["seconds"] == assemble.STILL_SECONDS and plan[1]["why"] == "default"


def test_a_clip_keeps_its_own_length_when_nothing_is_asked(tmp_path):
    root = ws(tmp_path)
    tiny_clip(root, seconds=3)
    plan = assemble.make_plan([{"clip": "clip.mp4"}], workspace=root)
    assert plan[0]["kind"] == "video" and 2.5 < plan[0]["seconds"] < 3.5
    assert plan[0]["why"] == "source"


def test_the_words_on_screen_default_to_the_narration(tmp_path):
    """"Say this and show this" is the exception; most shots say one thing and show it."""
    plan = assemble.make_plan([{"title": "x", "say": "旁白即字幕"}], workspace=ws(tmp_path))
    assert plan[0]["text"] == "旁白即字幕"
    assert plan[0]["audio"] == "voice"


def test_a_target_length_is_absorbed_by_what_can_hold_longer(tmp_path):
    """A film has to be the length it has to be, and a still picture can hold. A clip cannot: making
    it longer means changing its speed, so the target is met by the stills or it is reported."""
    root = ws(tmp_path)
    tiny_clip(root, seconds=4)
    plan = assemble.make_plan([{"title": "片头", "seconds": 4}, {"clip": "clip.mp4", "seconds": 4}],
                              workspace=root, total=20)
    assert plan[0]["stretched"] == 12 and plan[0]["seconds"] == 16   # the card takes all the slack
    assert plan[1]["seconds"] == 4                                   # the clip is left alone


def test_a_target_that_cannot_be_met_is_reported_not_faked(tmp_path):
    root = ws(tmp_path)
    tiny_clip(root, seconds=4)
    plan = assemble.make_plan([{"clip": "clip.mp4", "seconds": 4}], workspace=root, total=60)
    assert plan[0]["short"] == 56 and plan[0]["seconds"] == 4


def test_a_film_longer_than_asked_says_so_instead_of_cutting(tmp_path):
    """Silently dropping a shot to hit a length would delete work the group agreed on."""
    root = ws(tmp_path)
    plan = assemble.make_plan([{"title": "a", "seconds": 30}], workspace=root, total=10)
    assert plan[0]["over"] == 20 and plan[0]["seconds"] == 30


@pytest.mark.parametrize("shots, want", [
    ([], "needs a shot list"),
    ("not a list", "needs a shot list"),
    ([{"seconds": 3}], "no picture at all"),
    ([{"anim": "heart_surgery"}], "is not a drawing this app can make"),
    ([{"anim": {"kind": "coil_fill", "camera": "zoom"}}], "Camera"),
    ([{"title": "x", "seconds": 0.2}], "between"),
    ([{"title": "x", "seconds": "long"}], "not a number"),
    ([{"clip": "/etc/hosts"}], "absolute"),
    ([{"clip": "nope.mp4"}], "no file at"),
    ([{"title": "x"}] * (assemble.MAX_SHOTS + 1), "at most"),
])
def test_a_shot_list_that_cannot_be_filmed_is_refused_with_the_reason(tmp_path, shots, want):
    with pytest.raises(assemble.AssembleError) as e:
        assemble.make_plan(shots, workspace=ws(tmp_path))
    assert want in str(e.value)


def test_a_way_of_fitting_that_is_not_one_is_refused(tmp_path):
    with pytest.raises(assemble.AssembleError) as e:
        assemble.make_plan([{"clip": "x.png", "fit": "stretch"}], workspace=ws(tmp_path))
    assert "stretch" in str(e.value)


def test_blur_is_the_default_and_it_is_a_measured_choice(tmp_path):
    """The groups this is used by film 16:9 material for 9:16 platforms, and `cover` throws two
    thirds of every frame away — on an anatomical animation, the part with the anatomy in it. The
    default only matters because it is the one nobody overrides."""
    assert assemble.DEFAULT_FIT == "blur"
    plan = assemble.make_plan([{"title": "x"}], workspace=ws(tmp_path))
    assert plan[0]["fit"] == "blur"
    assert assemble.make_plan([{"title": "x"}], workspace=ws(tmp_path),
                              fit="cover")[0]["fit"] == "cover"
    assert assemble.make_plan([{"title": "x", "fit": "contain"}], workspace=ws(tmp_path),
                              fit="cover")[0]["fit"] == "contain"     # a shot can override the film


def _wide_red(path: pathlib.Path) -> pathlib.Path:
    """A wide bright source, so what the three fitting modes put in the matte is unmistakable."""
    from PIL import Image
    img = Image.new("RGB", (320, 180), (220, 30, 30))
    for x in range(140, 180):
        for y in range(70, 110):
            img.putpixel((x, y), (255, 255, 255))
    img.save(path)
    return path


@needs_ffmpeg
def test_the_three_ways_of_fitting_leave_different_things_in_the_matte(tmp_path):
    """Checked on the pixels, because "the picture fits" is not something a log line can show:
    `contain` leaves black, `blur` leaves a blurred copy of the same picture, and `cover` fills the
    frame and loses what was at the sides."""
    import asyncio
    root = ws(tmp_path)
    _wide_red(root / "wide.png")
    outs = {}
    for fit in ("contain", "blur", "cover"):
        out = asyncio.run(assemble.render(
            root, [{"clip": "wide.png", "seconds": 1, "motion": False}],
            size="270x480", name=f"fit-{fit}", timeout=180, fit=fit))
        outs[fit] = pathlib.Path(out["path"])

    from PIL import Image

    def top(clip: pathlib.Path):        # a pixel well above where the contained picture starts
        frame = tmp_path / (clip.stem + ".png")
        code, detail = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                                      "-y", "-ss", "0.5", "-i", str(clip), "-frames:v", "1",
                                      str(frame)], 60)
        assert code == 0, detail
        return Image.open(frame).convert("RGB").getpixel((135, 8))

    assert max(top(outs["contain"])) < 30, top(outs["contain"])        # black bars
    assert top(outs["blur"])[0] > 40, top(outs["blur"])                # a copy of the picture
    assert top(outs["cover"])[0] > 150, top(outs["cover"])             # the picture itself, cropped
    assert top(outs["blur"]) != top(outs["cover"]), "blur and cover must not look the same"

    assert assemble.parse_size("1080x1920")[2] == "1080x1920"
    assert assemble.parse_size("")[2] == "1080x1920"          # the vertical default
    assert assemble.parse_size("1920x1080")[2] == "1920x1080"
    assert assemble.parse_size("1081x1921")[2] == "1080x1920"  # h264 refuses an odd dimension
    with pytest.raises(assemble.AssembleError):
        assemble.parse_size("big")


# ------------------------------------------------------------------ the words, and where they land
def _pixels(path: pathlib.Path):
    from PIL import Image
    return Image.open(path).convert("RGBA").load(), Image.open(path)


def test_the_subtitle_is_drawn_where_it_can_be_read(tmp_path):
    """The reason this is drawn here at all: this machine's ffmpeg has no `drawtext`, so no filter
    graph could put them anywhere. The test therefore checks the geometry, because "there is a PNG"
    is not the same as "the words are on screen where a viewer can read them"."""
    w, h = 720, 1280
    out = assemble.subtitle_png("颅内动脉瘤介入治疗,弹簧圈填塞瘤腔,载瘤动脉保持通畅。", (w, h),
                                tmp_path / "sub.png")
    from PIL import Image
    img = Image.open(out).convert("RGBA")
    assert img.size == (w, h)
    px = img.load()
    drawn = [(x, y) for y in range(0, h, 4) for x in range(0, w, 4) if px[x, y][3] > 0]
    assert drawn, "nothing was drawn at all"
    ys = [y for _, y in drawn]
    # In the lower part of the frame (readable, and out of the way of the picture), and not touching
    # the very bottom — a generated clip often carries its own caption there, and so do the platforms.
    assert min(ys) > h * 0.5, min(ys)
    assert max(ys) < h * 0.95, max(ys)
    white = sum(1 for y in range(0, h, 2) for x in range(0, w, 2)
                if (p := px[x, y])[3] > 200 and p[0] > 230 and p[1] > 230 and p[2] > 230)
    assert white > 100, f"the text itself is not visible ({white} bright pixels)"


def test_a_title_card_has_a_title_and_no_picture(tmp_path):
    out = assemble.card_png("颅内动脉瘤介入治疗", "弹簧圈栓塞 · 科普示意", (720, 1280),
                            tmp_path / "card.png")
    from PIL import Image
    img = Image.open(out).convert("RGB")
    colours = img.getcolors(maxcolors=1 << 20) or []
    white = sum(n for n, c in colours if min(c) > 230)
    assert white > 200, colours[:4]
    assert len(colours) > 2, "a card is not one flat colour"      # the accent line and the text


# ------------------------------------------------------------------ the subtitle file
def test_the_srt_is_built_from_the_same_plan_that_timed_the_film():
    plan = [{"seconds": 3.0, "text": ""}, {"seconds": 2.5, "text": "第一句"},
            {"seconds": 4.0, "text": "第二句"}]
    srt = assemble.srt_of(plan)
    assert "1\n00:00:03,000 --> 00:00:05,500\n第一句" in srt
    assert "2\n00:00:05,500 --> 00:00:09,500\n第二句" in srt
    assert srt.count("-->") == 2                     # a silent shot gets no line


def test_a_timecode_is_the_shape_a_player_expects():
    assert assemble._timecode(0) == "00:00:00,000"
    assert assemble._timecode(3661.25) == "01:01:01,250"
    assert assemble._timecode(-5) == "00:00:00,000"


# ------------------------------------------------------------------ the voice
def test_the_voice_follows_the_language_on_screen():
    if not assemble.voices():
        pytest.skip("this machine has no speech voices configured")
    zh = assemble.pick_voice("颅内动脉瘤介入治疗")
    assert zh in ("Tingting", "Meijia", "Sinji") or zh
    assert assemble.pick_voice("anything", "Alex") == "Alex"     # an explicit choice always wins


def test_a_rough_length_is_available_before_anything_is_recorded():
    assert assemble.narration_seconds("颅内动脉瘤介入治疗,弹簧圈填塞瘤腔。") > 2
    assert assemble.narration_seconds("") == 1.0


def test_a_narration_estimate_and_the_real_reading_are_the_same_order_of_magnitude():
    """The plan is timed from the estimate and then corrected by the recording, so an estimate that
    is wildly wrong would move every later shot. This pins the estimate to the machine's own voice."""
    if not assemble.voices():
        pytest.skip("this machine has no speech voices configured")
    import asyncio
    text = "介入治疗从股动脉穿刺,把微导管送到动脉瘤腔内,用弹簧圈填塞瘤腔。"
    got = asyncio.run(assemble.speak(text, pathlib.Path("/tmp/ta-est.aiff"),
                                     assemble.pick_voice(text), 60))
    assert 0.5 < got / assemble.narration_seconds(text) < 2.0, (got, assemble.narration_seconds(text))


# ------------------------------------------------------------------ what a member is offered
def test_the_tool_is_offered_exactly_when_it_can_work(store, make_router, monkeypatch):
    """Same rule as every other tool: nobody is handed one that cannot run, and the user is told why
    it is missing — a member told to assemble without ffmpeg would retry and blame itself."""
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group, agent = store.get_group(g["id"]), store.list_agents()[0]

    monkeypatch.setattr(assemble, "available", lambda: ("", "需要 ffmpeg"))
    import asyncio
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    assert "assemble_video" not in ctx.tools
    assert "需要 ffmpeg" in ctx.problems

    monkeypatch.setattr(assemble, "available", lambda: ("/usr/bin/false-ffmpeg", ""))
    ctx = asyncio.run(orch.toolhub.context(group, agent, connect=False))
    assert "assemble_video" in ctx.tools
    assert not any("ffmpeg" in p for p in ctx.problems), ctx.problems
    # It runs programs on this machine, so it asks before it does — like run_code and the generators.
    # (The `store` fixture runs everything on `allow_all`; this puts the shipped default back, because
    # "does a new built-in ask?" is exactly what the default decides.)
    store.update_settings({"perm_mode": "ask_risky"})
    assert orch.toolhub.policy(ctx.tools["assemble_video"]) == "ask"
    # The budget a tool declared for itself has to reach the caller that enforces it. This is how a
    # three-minute render gets three minutes instead of the generic 60 seconds.
    cfg = store.get_settings()
    assert ctx.tools["assemble_video"]["timeout_key"] == "assemble_timeout"
    from app.toolhub import timeout_budget
    assert timeout_budget(cfg, ctx.tools["assemble_video"]) == cfg["assemble_timeout"]


# ------------------------------------------------------------------ for real
@needs_ffmpeg
def test_a_film_is_made_and_the_answer_says_what_it_is(tmp_path, store, make_router):
    """One real run: a title card, a clip with narration, and a card at the end. What is checked is
    the answer a group would read — the length, the files, the honest line about the voice — and
    that the artefacts really exist."""
    import asyncio
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    workspace = pathlib.Path(store.workspace_dir(g["id"]))
    tiny_clip(workspace, seconds=2)
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))

    out = asyncio.run(orch.toolhub.call(ctx, "assemble_video", {
        "name": "assembly-test",
        "size": "480x854",
        "shots": [{"title": "测试片头", "subtitle": "示例", "seconds": 2},
                  {"clip": "clip.mp4", "say": "这段旁白由本机语音合成,用来验证时长与字幕。",
                   "seconds": 5},
                  {"title": "结束", "seconds": 2}]}))

    assert out.ok, out.text
    assert "assembly-test.mp4" in out.text and "0:09" in out.text.replace(" ", "")
    assert "Tingting" in out.text or "旁白" in out.text
    assert [f["kind"] for f in out.files][0] == "video"
    film = workspace / "video" / "assembly-test.mp4"
    assert film.is_file() and film.stat().st_size > 5000
    info = assemble.probe(film)
    assert info["kind"] == "video" and info["audio"] and 8.0 < info["seconds"] < 10.5
    assert (workspace / "video" / "assembly-test.srt").is_file()
    assert (workspace / "video" / "assembly-test.md").is_file()
    # The intermediates are kept out of the way, in a dotted folder nothing scans.
    assert (workspace / assemble.SCRATCH / "assembly-test").is_dir()


@needs_ffmpeg
def test_a_narration_longer_than_its_slot_lengthens_the_shot(tmp_path, store, make_router):
    """The one rule this module exists to get right: an asked-for length is overruled by the voices
    that have to fit inside it, because a sentence cut in half is the most obvious way an assembled
    film looks broken — and the change is written down rather than done quietly."""
    import asyncio
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    workspace = pathlib.Path(store.workspace_dir(g["id"]))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))

    out = asyncio.run(orch.toolhub.call(ctx, "assemble_video", {
        "name": "narration-test", "size": "480x854",
        "shots": [{"title": "一句话", "seconds": 1,
                   "say": "这句话明显比一秒钟长得多,所以这个镜头必须被延长,否则话会被截断。"}]}))

    assert out.ok, out.text
    assert "lengthened" in out.text or "延长" in out.text
    assert (workspace / "video" / "narration-test.mp4").is_file()


def test_the_tool_is_not_offered_to_a_generating_member(store, make_router):
    """A generating member's whole turn is one call to its own model; handing it an assembly tool
    would turn "make me a clip" into a member writing a shot list nobody asked for."""
    import asyncio
    from app import media
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    group = store.get_group(g["id"])
    maker = {**store.list_agents()[0], "origin": media.MEDIA_ORIGIN}
    ctx = asyncio.run(orch.toolhub.context(group, maker, connect=False))
    assert "assemble_video" not in ctx.tools


@needs_ffmpeg
def test_a_still_moves_unless_it_is_asked_not_to(tmp_path):
    """A still drifts by default — 2026-09-26, after a whole film came out frozen.

    The 300s science film was 14 stills. The assembler itself reported 「会动的镜头: 0 个是本程序画的」,
    and measuring it here confirmed the picture: adjacent frames 0.5s apart differed by **0.00** at seven
    of eight sampled moments. The user's words were 「图片不是动态的」.

    The cause was this default: `motion` was true only for a *title card*, so every illustration sat
    perfectly still unless the caller passed a flag nobody passes. Now it is true for stills, with two
    exceptions that stay still on purpose: `fit: contain` (its whole point is to keep the whole picture
    inside the frame — pushing in crops exactly what it was asked to preserve) and an explicit
    `motion: false`.
    """
    import asyncio
    import pathlib

    from PIL import Image, ImageChops, ImageDraw

    root = ws(tmp_path)
    # ⚠️ A **patterned** still, not a flat colour: a push-in on a single-colour frame changes nothing,
    # and the test would pass while the film stayed frozen. (`ImageDraw` clips, so the checkerboard
    # does not need the arithmetic to land exactly on the edges.)
    still = Image.new("RGB", (270, 480), (20, 20, 20))
    pen = ImageDraw.Draw(still)
    for y in range(0, 480, 24):
        for x in range(0, 270, 24):
            if (x // 24 + y // 24) % 2:
                pen.rectangle([x, y, x + 11, y + 11], fill=(235, 235, 235))
    still.save(root / "still.png")

    def grabbed(film: pathlib.Path, seconds: float) -> Image.Image:
        png = root / f"g{seconds}.png"
        code, detail = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                                      "-ss", str(seconds), "-i", str(film),
                                      "-frames:v", "1", str(png)], 60)
        assert code == 0, detail
        return Image.open(png).convert("L")

    def drift(shot: dict, name: str) -> float:
        info = asyncio.run(assemble.render(root, [shot], size="270x480", name=name, burn=False))
        film = pathlib.Path(info["path"])
        assert film.is_file(), info
        a, b = grabbed(film, 0.2), grabbed(film, 1.6)
        diff = ImageChops.difference(a, b)
        return sum(i * n for i, n in enumerate(diff.histogram())) / (a.width * a.height)

    moved = drift({"clip": "still.png", "seconds": 3, "fit": "cover"}, "still")
    assert moved > 1.0, f"静帧没有动（帧差 {moved:.2f}）—— 默认又关掉了"

    kept = drift({"clip": "still.png", "seconds": 3, "fit": "contain"}, "wide")
    assert kept < 0.5, f"fit: contain 的图被推镜裁掉了（帧差 {kept:.2f}）"

    frozen = drift({"clip": "still.png", "seconds": 3, "fit": "cover", "motion": False}, "frozen")
    assert frozen < 0.5, f"显式 motion: false 仍然在动（帧差 {frozen:.2f}）"
