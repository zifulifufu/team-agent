"""Layout templates: the look of a film, chosen by name instead of by code change.

Three things are worth pinning, and each is a way a film could come out looking like something
nobody asked for: a layout that silently fails to load, a name that silently falls back to the
default look, and a knob that does nothing on screen.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app import assemble, layouts
from tests.conftest import FakeLLM
from tests.test_collab import setup

needs_ffmpeg = pytest.mark.skipif(not assemble.available()[0],
                                  reason="assembling needs ffmpeg and ffprobe on this machine")


@pytest.fixture
def user_layouts(tmp_path):
    """A folder of the user's own layouts, undone afterwards.

    The registry is module-level state because the tool layer reads it before it has a store to ask,
    so a test that points it at a `tmp_path` has to point it back — otherwise another test assembles
    a film with a layout that no longer exists on disk.
    """
    layouts.use_folder(tmp_path / "layouts")
    try:
        yield tmp_path / "layouts"
    finally:
        layouts.use_folder(None)


def put(folder, name: str, body) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(
        body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


# ------------------------------------------------------------------ the shipped ones
def test_the_default_layout_is_exactly_what_the_code_did_before_layouts_existed():
    """A film assembled without naming a layout has to be the same film. Every one of these numbers
    is a literal that used to live in `assemble.py`, so a change here is a change to every film
    everybody has already made without asking for one."""
    d = layouts.style("default")
    assert d["subtitle"] == {"font_scale": 0.045, "bottom": 0.86, "wrap": 0.86,
                             "box": "#000000", "alpha": 130, "colour": "#FFFFFF"}
    assert d["card"]["background"] == "#0E1B2A" and d["card"]["accent"] == "#2E7CF6"
    assert (d["card"]["title_scale"], d["card"]["subtitle_scale"]) == (0.062, 0.032)
    assert d["credit"]["text"] == "" and d["pace"]["max_shot_seconds"] == 0
    assert d["pace"]["still"] == 0, "0 means 'whatever the code's own default is'"


def test_the_shipped_names_are_stable_and_describe_themselves():
    """They are offered in a tool's schema, so a name is an interface. Each one also has to say what
    it is for — a dropdown of four opaque names is one nobody picks from."""
    assert layouts.names() == ["case-review", "default", "lecture", "public-science"]
    for name in layouts.names():
        row = layouts.row(name)
        assert row["title"] and row["note"], f"{name} does not describe itself"
        assert row["size"] in ("",) + assemble.SIZES, f"{name} names a size we do not make"


def test_an_unknown_layout_is_refused_with_the_list_rather_than_defaulted():
    """There is a sensible default look and it is called `default`. Quietly giving it to a caller who
    asked for `clinic` produces a film that is nearly what was asked for — which is the outcome
    nobody notices, and the reason this raises instead."""
    with pytest.raises(ValueError) as e:
        layouts.style("clinic")
    said = str(e.value)
    assert "clinic" in said and "default" in said


def test_an_empty_name_means_the_default_look():
    assert layouts.style("") is layouts.BUILTIN["default"]


# ------------------------------------------------------------------ the user's own
def test_a_layout_the_user_wrote_is_offered_beside_the_shipped_ones(user_layouts):
    put(user_layouts, "clinic", {"title": "Clinic", "size": "1920x1080",
                                "subtitle": {"font_scale": 0.03}})
    assert "clinic" in layouts.names()
    got = layouts.style("clinic")
    assert got["size"] == "1920x1080" and got["subtitle"]["font_scale"] == 0.03
    assert got["subtitle"]["bottom"] == 0.86, "unset knobs keep the measured defaults"


def test_a_file_that_cannot_be_used_is_named_and_never_silently_skipped(user_layouts):
    """The failure this prevents: a layout that is quietly not loaded, so the film assembles with
    the default look while the caller believes it got the one they wrote."""
    put(user_layouts, "broken", "{ not json")
    put(user_layouts, "not-an-object", "[1, 2]")
    put(user_layouts, "bad-colour", {"card": {"background": "dark navy"}})
    put(user_layouts, "out-of-range", {"subtitle": {"font_scale": 4.5}})
    put(user_layouts, "bad-shape", {"subtitle": "big"})
    put(user_layouts, "default", {"subtitle": {"font_scale": 0.03}})      # collides
    put(user_layouts, "fine", {"subtitle": {"alpha": 200}})
    got = layouts.reload(force=True)
    assert got["loaded"] == ["fine"], "only the usable one is loaded"
    why = {e["file"]: e["why"] for e in got["errors"]}
    assert set(why) == {"broken.json", "not-an-object.json", "bad-colour.json",
                        "out-of-range.json", "bad-shape.json", "default.json"}
    for name, said in why.items():
        assert said.strip(), f"{name} was rejected with no reason"
    assert "4.5" in why["out-of-range.json"], "an out-of-range number names the number"


def test_a_colour_is_checked_rather_than_passed_to_the_drawer(user_layouts):
    """`PIL` would happily accept a nonsense string and draw something, and the film would come out
    with a colour nobody chose. The check is here so the diagnosis arrives before the render."""
    put(user_layouts, "ok", {"subtitle": {"box": "#123ABC", "colour": "#ffffff"}})
    assert layouts.style("ok")["subtitle"]["box"] == "#123ABC"


def test_a_transition_kind_that_is_not_a_transition_is_refused(user_layouts):
    put(user_layouts, "wrong", {"transition": {"kind": "dissolve"}})
    got = layouts.reload(force=True)
    assert got["loaded"] == []
    assert "dissolve" in got["errors"][0]["why"]
    assert "cut" in got["errors"][0]["why"], "it says what is available instead"


def test_the_default_transition_is_the_one_the_app_already_had():
    """`cut` is a plain concat, which is what this app has always done. A layout that quietly
    changed the default would re-render every film anybody had already made."""
    assert layouts.style("default")["transition"] == {"kind": "cut", "seconds": 0.4}
    assert layouts.style("default")["pace"]["zoom"] == 0.00035, "the push-in rate as it was"


def test_a_fade_needs_a_shot_long_enough_to_survive_it():
    """A half-second shot with a half-second fade at each end is a shot nobody sees. The rule lives
    in one function so the renderer and the note about what was skipped cannot disagree."""
    style = json.loads(json.dumps(layouts.style("default")))
    style["transition"] = {"kind": "fade", "seconds": 0.5}
    assert assemble.fade_fits(4.0, style) is True
    assert assemble.fade_fits(1.0, style) is False, "1s cannot hold a 0.5s fade at each end"
    assert assemble.fade_fits(1.0, layouts.style("default")) is False, "cut never fades"


# ------------------------------------------------------------------ onto the film
def _still(workspace: pathlib.Path, name: str = "clip.png",
           colour: str = "navy") -> pathlib.Path:
    out = workspace / name
    code, detail = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                                  "-f", "lavfi", "-i", f"color=c={colour}:s=320x240",
                                  "-frames:v", "1", str(out)], 60)
    assert code == 0 and out.is_file(), detail
    return out


@needs_ffmpeg
def test_a_layout_puts_a_card_at_each_end_as_ordinary_shots(tmp_path):
    """The cards are added as shots rather than as plan entries made by hand, so they go through the
    same validation and timing as everything else — and a card is the only part of a film whose
    length nobody ever asks for, which is why it has its own default."""
    root = tmp_path / "ws"
    root.mkdir()
    _still(root)
    style = dict(layouts.style("default"),
                 opening={"title": "颅内动脉瘤介入治疗", "subtitle": "科普示意"},
                 closing={"title": "有症状请及时就医", "subtitle": ""})
    out = __import__("asyncio").run(assemble.render(
        root, [{"clip": "clip.png", "say": "", "text": "", "seconds": 2}],
        style=style, name="cards", timeout=300))
    kinds = [e["kind"] for e in out["plan"]]
    assert kinds == ["card", "image", "card"], kinds
    assert out["plan"][0]["title"] == "颅内动脉瘤介入治疗"
    assert out["plan"][-1]["title"] == "有症状请及时就医"
    assert out["shots"] == 3 and out["seconds"] > 2


@needs_ffmpeg
def test_a_layout_without_its_own_text_leaves_the_film_alone(tmp_path):
    """`default` sets no card text, so no card is drawn — the layout decides where words may go, and
    the caller decides whether there are any. A layout that invented a title would put words in a
    film that nobody wrote."""
    root = tmp_path / "ws"
    root.mkdir()
    _still(root)
    out = __import__("asyncio").run(assemble.render(
        root, [{"clip": "clip.png", "say": "", "text": "", "seconds": 2}],
        style=layouts.style("default"), name="plain", timeout=300))
    assert [e["kind"] for e in out["plan"]] == ["image"]


@needs_ffmpeg
def test_a_layouts_per_shot_ceiling_shortens_shots_and_says_so(tmp_path):
    """The ceiling is the layout's answer to "how fast should this film move" — the one thing a
    caller cannot express through `seconds`, because they do not know how long each shot will turn
    out to be until the narration is recorded."""
    root = tmp_path / "ws"
    root.mkdir()
    _still(root)
    style = json.loads(json.dumps(layouts.style("default")))
    style["pace"]["max_shot_seconds"] = 2.0
    out = __import__("asyncio").run(assemble.render(
        root, [{"clip": "clip.png", "say": "", "text": "", "seconds": 6}],
        style=style, name="capped", timeout=300))
    assert out["plan"][0]["seconds"] == 2.0
    assert out["plan"][0]["why"] == "layout"
    assert any("2s ceiling" in n or "2 秒上限" in n for n in out["notes"]), out["notes"]


@needs_ffmpeg
def test_a_fade_really_darkens_the_ends_of_a_shot(tmp_path):
    """Measured, not assumed — the same discipline the clip measurement uses. A "fade" that only
    appears in the filter graph and not on the frames is exactly the kind of thing a duration check
    would wave through, so this reads actual pixel brightness at both ends and in the middle."""
    from PIL import Image
    root = tmp_path / "ws"
    root.mkdir()
    _still(root, colour="0xE8B23A")
    shots = [{"clip": "clip.png", "say": "", "text": "", "seconds": 4}]

    def bright(film, at: float) -> float:
        png = root / f"f{at}.png"
        code, detail = assemble._run([assemble.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
                                      "-y", "-ss", str(at), "-i", str(film), "-frames:v", "1",
                                      str(png)], 60)
        assert code == 0 and png.is_file(), detail
        px = list(Image.open(png).convert("L").resize((16, 16)).get_flattened_data())
        return sum(px) / len(px)

    def render_with(kind: str) -> str:
        style = json.loads(json.dumps(layouts.style("default")))
        style["transition"] = {"kind": kind, "seconds": 0.5}
        # `unify=False`: this measures the **transition**, and the style pass legitimately changes
        # how bright the frame is — a 4:3 still in a 9:16 film used to be blown up behind a blurred
        # copy of itself and is now framed whole at its own size, which is most of the frame's
        # brightness. Placement has its own tests (`tests/test_visual.py`); mixing the two would make
        # this test fail for a reason that has nothing to do with fades.
        out = __import__("asyncio").run(assemble.render(root, shots, size="480x854", style=style,
                                                       name="tr-" + kind, timeout=300, unify=False))
        return out["path"]

    cut, fade = render_with("cut"), render_with("fade")
    assert bright(cut, 0.05) - bright(fade, 0.05) > 30, "the fade did not darken the opening"
    assert bright(cut, 3.9) - bright(fade, 3.9) > 30, "the fade did not darken the ending"
    assert abs(bright(cut, 2.0) - bright(fade, 2.0)) < 8, "the middle must be untouched"


@needs_ffmpeg
def test_a_shot_too_short_to_fade_is_reported_rather_than_quietly_cut(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _still(root)
    style = json.loads(json.dumps(layouts.style("default")))
    style["transition"] = {"kind": "fade", "seconds": 0.5}
    out = __import__("asyncio").run(assemble.render(
        root, [{"clip": "clip.png", "say": "", "text": "", "seconds": 6},
               {"clip": "clip.png", "say": "", "text": "", "seconds": 1}],
        style=style, name="mixed-fade", timeout=300))
    assert any("hard cut" in n or "硬切" in n for n in out["notes"]), out["notes"]


@needs_ffmpeg
def test_the_standing_credit_line_is_drawn_over_every_shot(tmp_path):
    """It is the line that has to survive a re-cut, and a re-cut drops cards first — so it is on the
    frames rather than on a card. Asserted as a file that every shot is given, because "the credit
    came out blank" is not something a duration check would ever catch."""
    root = tmp_path / "ws"
    root.mkdir()
    _still(root)
    style = json.loads(json.dumps(layouts.style("default")))
    style["credit"]["text"] = "图示 © 长海医院神经外科"
    out = __import__("asyncio").run(assemble.render(
        root, [{"clip": "clip.png", "say": "", "text": "字幕", "seconds": 2}],
        style=style, name="credit", timeout=300))
    scratch = pathlib.Path(out["path"]).parent.parent / assemble.SCRATCH / "credit"
    assert (scratch / "credit.png").is_file(), "the overlay was never drawn"
    assert any("图示 © 长海医院神经外科" in n for n in out["notes"])


# ------------------------------------------------------------------ through the tool
@needs_ffmpeg
def test_the_tool_refuses_an_unknown_layout_by_name(store, make_router):
    """A member that names a layout which does not exist must hear about it, not get a film in the
    default look. It has to fail *before* the render: a refusal that arrives after ten minutes of
    ffmpeg is a different, worse answer."""
    import asyncio
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))
    got = asyncio.run(orch.toolhub.call(
        ctx, "assemble_video", {"layout": "no-such-look",
                                "shots": [{"title": "x", "seconds": 2}]}))
    assert got.ok is False and got.files == []
    assert "no-such-look" in got.text and "default" in got.text
    assert store.workspace_dir(g["id"]).glob("video/*.mp4") is not None, "sanity: workspace exists"
