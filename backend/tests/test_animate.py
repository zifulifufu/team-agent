"""Motion: drawings that move, and films that are not slide shows.

The thing worth testing here is not that ffmpeg runs — it is that the pictures **change over time in
the way the narration says they do**, and that a still and a moving frame are the same drawing. Those
two properties are what make an assembled film a film; everything else about it is timing, which the
assembler's own tests cover.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from app import animate, assemble, figure
from tests.conftest import FakeLLM
from tests.test_collab import setup

needs_ffmpeg = pytest.mark.skipif(not assemble.available()[0],
                                  reason="drawing a clip needs ffmpeg and ffprobe on this machine")

SIZE = (270, 480)


def frame(kind: str, t: float, params: dict | None = None, labels: list | None = None,
          size: tuple[int, int] = SIZE):
    """One drawn frame, as an image. Uses the same path the animation does."""
    return figure.plate(size, draw=figure.drawer(kind, size, params or {}, labels)(t))


def changed(a, b) -> int:
    """How many pixels differ by more than a rounding error — the measure of "something moved".

    Through `ImageChops` rather than by walking both images in Python: this gets called on every
    frame of a parametric test, and the per-pixel loop was both slower and deprecated.
    """
    from PIL import ImageChops
    hist = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L").histogram()
    return sum(n for level, n in enumerate(hist) if level > 12)


# ------------------------------------------------------------------ the motion itself
@pytest.mark.parametrize("kind", figure.ANIMATIONS)
def test_every_animation_moves_and_moves_the_whole_way_through(kind):
    """A drawing that only changes between two frames is a cut, not an animation. So: the start and
    the end differ, and so does every quarter in between — and the same input twice gives the same
    frame, because a film that cannot be reproduced cannot be reviewed."""
    start, end = frame(kind, 0.02), frame(kind, 0.98)
    moved = changed(start, end)
    assert moved > 0.004 * SIZE[0] * SIZE[1], f"{kind} barely moves ({moved} pixels)"

    steps = [frame(kind, t) for t in (0.02, 0.35, 0.65, 0.98)]
    for a, b in zip(steps, steps[1:]):
        assert changed(a, b) > 200, f"{kind} jumps instead of moving between two of its parts"
    assert changed(frame(kind, 0.5), frame(kind, 0.5)) == 0


def test_a_still_is_the_end_of_the_animation():
    """One vocabulary, not two: "the coil is in place" and "the coil being wound in" are the same
    drawing at two times, so the still a reviewer approves is a frame of the film that follows."""
    for kind in figure.ANIMATIONS:
        still = figure.plate(SIZE, draw=figure.schematic(kind, SIZE, {}))
        last = frame(kind, 1.0, {"camera": "none"})
        assert changed(still, last) == 0, f"the still of {kind} is not the animation's last frame"


def test_a_still_is_never_pushed_in_even_when_the_storyboard_asks_for_it():
    """A camera move belongs to the film. If a still took the same push, the frame a reviewer saw
    would be cropped differently from the frame the film uses."""
    asked = figure.plate(SIZE, draw=figure.schematic("coil_fill", SIZE, {"camera": "push"}))
    plain = figure.plate(SIZE, draw=figure.schematic("coil_fill", SIZE, {}))
    assert changed(asked, plain) == 0


def test_the_old_names_still_draw_the_same_thing():
    """`aneurysm_coiling` was what a still was called before these could move. A storyboard written
    then should not have to be rewritten."""
    for old, new in figure.SCHEMATIC_ALIASES.items():
        assert changed(figure.plate(SIZE, draw=figure.schematic(old, SIZE, {})),
                       figure.plate(SIZE, draw=figure.schematic(new, SIZE, {}))) == 0


def test_a_label_appears_when_the_part_it_names_does():
    """`from`/`to` are what let a drawing be narrated: a label for the coil makes no sense while the
    sac is still filling, and a label that is there from the first frame tells the viewer nothing."""
    labels = [{"text": "弹簧圈", "at_part": "coil", "from": 0.6, "to": 1.0}]
    with_label, without = frame("coil_fill", 0.1, labels=labels), frame("coil_fill", 0.1)
    assert changed(with_label, without) == 0, "the label showed up before its part"
    late_with, late_without = frame("coil_fill", 0.9, labels=labels), frame("coil_fill", 0.9)
    assert changed(late_with, late_without) > 200, "the label never showed up at all"


def test_an_animation_that_does_not_exist_comes_back_with_the_list():
    with pytest.raises(figure.FigureError) as e:
        figure.drawer("heart_surgery", SIZE, {}, [])
    assert "coil_fill" in str(e.value) and "aneurysm_coiling" in str(e.value)


def test_a_camera_that_does_not_exist_is_refused_before_anything_is_drawn():
    with pytest.raises(figure.FigureError) as e:
        animate.spec_of({"kind": "coil_fill", "camera": "zoom"})
    assert "zoom" in str(e.value)


def test_a_part_that_does_not_exist_is_refused_rather_than_ignored():
    """Silently doing nothing about `show: ["artery"]` is how a caller ends up believing it asked for
    something."""
    with pytest.raises(figure.FigureError) as e:
        animate.spec_of({"kind": "coil_fill", "show": ["sac", "artery"]})
    assert "artery" in str(e.value)


# ------------------------------------------------------------------ drawing a whole clip
@needs_ffmpeg
def test_a_clip_is_drawn_to_the_length_it_was_asked_for(tmp_path):
    """The motion is spread over exactly the time given, which is what lets a shot's animation follow
    its narration instead of the other way round."""
    out = tmp_path / "a.mp4"
    still = tmp_path / "a.png"
    got = asyncio.run(animate.render(out, animate.spec_of("blood_flow"), size=SIZE, fps=12,
                                     seconds=1.5, heading="血流", credit="本程序自绘", still=still,
                                     timeout=120))
    assert got["frames"] == 18 and got["seconds"] == pytest.approx(1.5)
    assert out.is_file() and still.is_file() and still.stat().st_size > 1000
    info = assemble.probe(out)
    assert info["kind"] == "video" and info["seconds"] == pytest.approx(1.5, abs=0.2)


@needs_ffmpeg
def test_the_clip_really_moves_once_it_has_been_encoded(tmp_path):
    """Frame-difference after the round trip, not before: the question is whether what a viewer gets
    is motion, and an encoder that was handed moving frames could still produce a frozen clip."""
    out = tmp_path / "b.mp4"
    asyncio.run(animate.render(out, animate.spec_of("coil_fill"), size=SIZE, fps=12, seconds=1.0,
                               timeout=120))
    frames = []
    for t in (0.1, 0.9):
        png = tmp_path / f"f{t}.png"
        code, detail = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                                      "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                                      str(png)], 60)
        assert code == 0, detail
        frames.append(png)
    from PIL import Image
    a = Image.open(frames[0]).convert("RGB")
    b = Image.open(frames[1]).convert("RGB")
    assert changed(a, b) > 0.004 * SIZE[0] * SIZE[1]


# ------------------------------------------------------------------ through the tools
def test_the_tool_draws_a_clip_and_points_at_the_review_frame(store, make_router):
    """The poster frame is part of the contract, not a nicety: it is what gets looked at, or handed to
    somebody who can see, before the film is assembled."""
    import asyncio as aio
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = aio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                       connect=False))
    assert "make_animation" in ctx.tools

    out = aio.run(orch.toolhub.call(ctx, "make_animation",
                                    {"kind": "coil_fill", "seconds": 2, "size": "270x480",
                                     "heading": "填塞过程", "credit": "本程序自绘示意图"}, None))
    if not out.ok:
        pytest.skip(f"ffmpeg refused: {out.text[:80]}")
    assert out.files and out.files[0]["kind"] == "video"
    assert ".mp4" in out.files[0]["name"]
    assert "关键帧" in out.text or "review" in out.text.lower()
    # And it tells the caller the anchors, because a label placed by guessing points somewhere else.
    assert "sac=" in out.text


def test_a_shot_can_be_an_animation_instead_of_a_picture(store, make_router, tmp_path):
    """The storyboard is where this has to be usable: one `anim` per shot, and the film draws it at
    whatever length the narration needs."""
    ws = tmp_path / "ws"
    ws.mkdir()
    plan = assemble.make_plan(
        [{"anim": "aneurysm_grow", "say": "管壁薄弱处逐渐鼓出。", "credit": "本程序自绘"},
         {"anim": {"kind": "coil_fill", "camera": "none", "coil_turns": 4}, "seconds": 4}],
        workspace=ws)
    assert [e["kind"] for e in plan] == ["anim", "anim"]
    assert plan[0]["anim"]["kind"] == "aneurysm_grow"
    assert plan[0]["own"] == plan[0]["seconds"], "an animation is drawn to its length, not held"
    assert plan[1]["anim"]["params"]["coil_turns"] == 4


@needs_ffmpeg
def test_an_animated_film_comes_out_with_the_motion_in_it(store, make_router, tmp_path):
    """End to end, because the failure this whole change is about — a film that plays as a slide show
    — only ever shows up in the finished file."""
    import asyncio as aio
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ws = pathlib.Path(store.workspace_dir(g["id"]))
    ctx = aio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                       connect=False))

    async def yes(spec, args):
        return True

    out = aio.run(orch.toolhub.call(ctx, "assemble_video",
                                    {"shots": [{"anim": {"kind": "coil_fill", "camera": "none"},
                                                "seconds": 2, "say": ""},
                                               {"anim": {"kind": "blood_flow", "camera": "none"},
                                                "seconds": 2, "say": ""}],
                                     "size": "270x480", "name": "anim-test", "subtitles": "off"},
                                    yes))
    assert out.ok, out.text
    film = pathlib.Path(aio.run(_find_film(ws)))
    assert film.is_file()
    frames = []
    for t in (0.4, 1.6):
        png = tmp_path / f"m{t}.png"
        code, detail = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                                      "-y", "-ss", str(t), "-i", str(film), "-frames:v", "1",
                                      str(png)], 60)
        assert code == 0, detail
        frames.append(png)
    from PIL import Image
    a, b = (Image.open(p).convert("RGB") for p in frames)
    assert changed(a, b) > 0.004 * SIZE[0] * SIZE[1], "the assembled film has no motion in it"

    # And the sheet says these shots are drawings rather than pictures.
    sheet = film.with_suffix(".md").read_text(encoding="utf-8")
    assert "anim" in sheet and ("程序动画" in sheet or "animated" in sheet)


async def _find_film(ws: pathlib.Path) -> pathlib.Path:
    return next((ws / "video").glob("anim-test.mp4"))


def test_a_film_of_stills_says_it_is_a_slide_show(store, make_router):
    """Every shot a still is the state this whole module exists to end, and the group has to be told
    rather than left to notice after rendering a slide show with narration over it."""
    import asyncio as aio
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ws = pathlib.Path(store.workspace_dir(g["id"]))
    ctx = aio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                       connect=False))

    async def yes(spec, args):
        return True

    out = aio.run(orch.toolhub.call(ctx, "assemble_video",
                                    {"shots": [{"title": "一", "seconds": 1},
                                               {"title": "二", "seconds": 1},
                                               {"title": "三", "seconds": 1}],
                                     "size": "270x480", "name": "stills-only", "subtitles": "off"},
                                    yes))
    assert out.ok, out.text
    assert "幻灯片" in out.text or "slide show" in out.text
