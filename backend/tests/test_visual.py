"""What may go into the frame, and what happens to a picture that may not go in as it is.

The complaint these tests are the answer to was 「图不专业」, and it was measured before it was
believed: 30 pictures in one finished film, **nine different backgrounds**, a 14x spread in colour
count, and 12 of the 30 not the film's shape — while the 15 pictures the app drew itself were
identical to each other. So what is defended here is not a taste: it is that a mixed pile of material
comes out of `_unify` looking like one film, that a real picture is never invented, and that a
decision somebody made on purpose is not overruled.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app import assemble, figure, visual

FRAME = (1080, 1920)


RED = (200, 60, 60)


def _flat(path: Path, size: tuple[int, int], *, canvas=(250, 250, 250), ink=(40, 60, 120)) -> Path:
    """A drawing: three flat colours in blocks. Nearest thing to a chart.

    Every shape stays off the border on purpose — a mark that touches a corner changes the picture's
    own background measurement, which is the number half these tests are about.
    """
    w, h = size
    im = Image.new("RGB", size, canvas)
    d = ImageDraw.Draw(im)
    d.rectangle([w * 0.2, h * 0.3, w * 0.8, h * 0.7], fill=ink)
    d.ellipse([w * 0.3, h * 0.35, w * 0.7, h * 0.55], fill=RED)
    im.save(path)
    return path


def _photo(path: Path, size: tuple[int, int]) -> Path:
    """A photograph: continuous tone, so no small set of colours describes it.

    Built from a smooth gradient with per-pixel noise rather than from a real file, so the test says
    what it means — "a picture whose colours do not concentrate" — and not "this JPEG".
    """
    w, h = size
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(h):
        for x in range(w):
            v = (x * 7 + y * 13) % 256
            px[x, y] = (v, (v * 3) % 256, (v * 5 + x) % 256)
    im.save(path)
    return path


def _on_a_page(path: Path, size: tuple[int, int], *, page=(255, 255, 255),
               ink=(40, 60, 120)) -> Path:
    """A colour diagram photographed on a page: a flat white surround, content in the middle.

    The margin is wide enough that the page is more than a quarter of the picture, which is what
    `FIELD_SHARE` is: a page has to be a substantial part of the frame before the picture is "on" one.
    """
    w, h = size
    im = Image.new("RGB", size, page)
    d = ImageDraw.Draw(im)
    d.rectangle([w * 0.28, h * 0.28, w * 0.72, h * 0.72], fill=ink)
    d.ellipse([w * 0.36, h * 0.36, w * 0.64, h * 0.64], fill=RED)
    im.save(path)
    return path


def _projection(path: Path, size: tuple[int, int]) -> Path:
    """A projection the way a light box or a print gives it: anatomy dark on a light field.

    Greyscale, because that is what `NEUTRAL_CHROMA` is about — and because flipping a neutral picture
    is lossless, while flipping a coloured one is not.
    """
    w, h = size
    im = Image.new("RGB", size, (250, 250, 250))
    d = ImageDraw.Draw(im)
    d.ellipse([w * 0.3, h * 0.35, w * 0.7, h * 0.65], fill=(20, 20, 20))
    d.line([0, h * 0.5, w, h * 0.5], fill=(70, 70, 70), width=max(2, w // 40))
    im.save(path)
    return path


def _lifted(path: Path, size: tuple[int, int]) -> Path:
    """A picture with real range but no true black: the shadows start at 90. A grade is the right
    answer here, and this is the fixture that says so — as against `_bright_photograph`, whose whole
    range is bright and which must not be graded at all."""
    w, h = size
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(h):
        for x in range(w):
            v = 90 + (x * 110 // max(1, w - 1))
            px[x, y] = (v, v - 5 + (y % 9), v + 3)
    im.save(path)
    return path


def _bright_photograph(path: Path, size: tuple[int, int]) -> Path:
    """A photograph that is bright **all over**: light everywhere, not light round the edge.

    This is the picture the page rule must not touch. Its field colour matches a quarter of it (so the
    share alone would say "page") but the middle matches it just as much, which is what a bright scene
    looks like and a page never does.
    """
    w, h = size
    im = Image.new("RGB", size, (238, 238, 236))
    px = im.load()
    for y in range(h):
        for x in range(w):
            n = ((x * 11 + y * 17) % 14) - 7
            px[x, y] = (238 + n, 236 + n, 232 + n)
    im.save(path)
    return path


# --------------------------------------------------------------------- measuring
def test_a_drawing_and_a_photograph_are_told_apart_by_measurement(tmp_path):
    flat = visual.audit(_flat(tmp_path / "flat.png", (600, 600)))
    photo = visual.audit(_photo(tmp_path / "photo.png", (200, 200)))
    assert flat["flat"] is True
    assert photo["flat"] is False
    # The thresholds came off real material; the two groups must not touch. See FLAT_COVERAGE.
    assert flat["coverage"] > visual.FLAT_COVERAGE
    assert photo["coverage"] < visual.FLAT_COVERAGE


def test_the_background_is_read_at_full_resolution_not_off_a_thumbnail(tmp_path):
    """A thumbnail averages neighbouring pixels, so every still *looks* like it has its own
    background. That number is the headline finding, so it is read where it is true."""
    p = _flat(tmp_path / "flat.png", (1080, 1920), canvas=(11, 20, 34))
    assert visual.audit(p)["background"] == "#0B1422"
    assert visual.audit(p)["corner_delta"] == 0


# --------------------------------------------------------------------- the gate
def test_a_drawing_of_the_wrong_shape_is_drawn_again(tmp_path):
    p = _flat(tmp_path / "wide.png", (1600, 900))
    v = visual.admit(p, FRAME)
    assert v["fit"] == "redraw"


def test_a_photograph_of_the_wrong_shape_is_framed_not_re_drawn(tmp_path):
    """The rule that matters most: a re-drawn angiogram is an invented angiogram."""
    p = _photo(tmp_path / "wide.png", (600, 300))
    assert visual.admit(p, FRAME)["fit"] == "plate"


def test_a_photograph_already_the_frame_s_shape_fills_it(tmp_path):
    p = _photo(tmp_path / "tall.png", (540, 960))
    assert visual.admit(p, FRAME)["fit"] == "cover"


def test_a_drawing_is_drawn_again_even_when_it_already_fits_the_frame(tmp_path):
    """Shape is not the test here — unification is. A set of drawings each carrying its own
    background is the "two films stapled together" problem at 9:16 as much as at 16:9."""
    p = _flat(tmp_path / "tall.png", (1080, 1920), canvas=(250, 250, 250))
    assert visual.admit(p, FRAME)["fit"] == "redraw"


def test_a_fit_the_caller_named_is_honoured_and_the_cost_is_stated(tmp_path):
    p = _flat(tmp_path / "wide.png", (1600, 900))
    v = visual.admit(p, FRAME, fit="cover")
    assert v["fit"] == "cover"                      # a decision, not a suggestion
    assert any("stretch" in w.lower() or "crop" in w.lower() for w in v["warnings"])


def test_asking_to_re_draw_a_photograph_warns_that_it_would_invent_it(tmp_path):
    p = _photo(tmp_path / "photo.png", (400, 300))
    v = visual.admit(p, FRAME, fit="redraw")
    assert v["fit"] == "redraw"
    assert any("claim" in w.lower() or "real" in w.lower() for w in v["warnings"])


def test_a_picture_with_no_source_line_says_so(tmp_path):
    p = _flat(tmp_path / "wide.png", (1600, 900))
    assert any("from" in w.lower() or "来源" in w for w in visual.admit(p, FRAME)["warnings"])
    assert not [w for w in visual.admit(p, FRAME, credit="某图谱")["warnings"] if "来源" in w]


# --------------------------------------------------------------------- reading the style
def test_the_profile_reads_the_shared_style_and_names_who_disagrees(tmp_path):
    """「找出共同点」 has two halves, and the second one is the useful half: which pictures are the
    ones that do not belong."""
    good = (_flat(tmp_path / "a.png", (800, 600), canvas=(11, 20, 34), ink=(200, 60, 60)),
            _flat(tmp_path / "b.png", (400, 300), canvas=(11, 20, 34), ink=(60, 200, 120)),
            _flat(tmp_path / "c.png", (800, 600), canvas=(11, 20, 34), ink=(240, 240, 250)))
    odd = _flat(tmp_path / "d.png", (800, 600), canvas=(255, 255, 255), ink=(0, 0, 0))
    prof = visual.profile([*good, odd])
    assert prof["read"] == 4 and prof["unreadable"] == []
    assert prof["canvas"] == "#0B1422"                    # three of four agree
    assert prof["agreement"] == 0.75
    assert [Path(r["path"]).name for r in prof["split"]] == ["d.png"]


def test_the_palette_holds_the_material_s_own_colours_not_an_average_of_them(tmp_path):
    """The bug the user's complaint was about. The palette used to be a k-means over every picture —
    including the photographs and screenshots that are never re-drawn — and it turned the material's
    blue and cyan into greys: measured on the real film, the catheter's blue landed 112/255 away from
    anything in the palette and the flow cyan 106/255, while three greys from a chat screenshot took
    those slots. A colour the drawings use has to come through as itself.
    """
    inks = [(200, 60, 60), (60, 200, 120), (240, 240, 250)]
    drawings = [_flat(tmp_path / f"{i}.png", (300, 200), canvas=(11, 20, 34), ink=ink)
                for i, ink in enumerate(inks)]
    # A photograph in the same set: it is never re-drawn, so it must not get a say.
    photo = _photo(tmp_path / "photo.png", (200, 200))
    prof = visual.profile([*drawings, photo])
    for ink in inks:
        nearest = min(prof["ramp_rgb"],
                      key=lambda c: sum((a - b) ** 2 for a, b in zip(c, ink)))
        distance = sum((a - b) ** 2 for a, b in zip(nearest, ink)) ** 0.5
        assert distance <= 20, f"{ink} came out {nearest} ({distance:.0f} away)"


def test_an_unreadable_file_is_counted_rather_than_skipped(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not a picture")
    prof = visual.profile([_flat(tmp_path / "a.png", (200, 200)), bad])
    assert prof["count"] == 2 and prof["read"] == 1
    assert prof["unreadable"] and "broken.png" in prof["unreadable"][0]


# --------------------------------------------------------------------- drawing it again
def test_re_drawing_collapses_the_palette_and_keeps_the_frame(tmp_path):
    """The measured effect, not a description of it: 496-716 colours in the real material, and the
    output built from the film's palette alone."""
    src = _flat(tmp_path / "wide.png", (1600, 900))
    src_colours = len(set(Image.open(src).convert("RGB").getdata()))
    out = visual.redraw(src, tmp_path / "out.png", FRAME, visual.profile([src]))
    with Image.open(out) as im:
        im = im.convert("RGB")
        assert im.size == FRAME
        assert im.getpixel((0, 0)) == visual.FILM_CANVAS     # the film's own background
        assert len(set(im.getdata())) <= visual.PALETTE_MAX + 4
    assert src_colours >= 3


def test_a_picture_already_on_the_film_canvas_is_placed_and_left_alone(tmp_path):
    """The rule that stops the engine damaging work that is already right.

    Measured before it existed: a re-draw of a schematic this app had drawn itself came out 78.7%
    identical, the other 21% being its heading band (`figure.BAND`) and its darker field, both judged
    "background" and both repainted. Re-drawing something the user approved is damage, not style.
    """
    src = tmp_path / "already.png"
    im = Image.new("RGB", (1080, 1920), figure.DRAWING_BG)      # the film's own canvas
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1080, 160], fill=figure.BAND)           # a heading band, 12/255 off the canvas
    d.rectangle([300, 800, 780, 1100], fill=figure.COIL)
    d.ellipse([420, 880, 660, 1020], fill=figure.CATHETER)
    im.save(src)

    v = visual.admit(src, FRAME)
    assert v["fit"] == "cover" and v["repaint"] is False       # frame-shaped and already at home

    out = visual.redraw(src, tmp_path / "out.png", FRAME, visual.profile([src]), repaint=False)
    before = list(Image.open(src).convert("RGB").getdata())
    after = list(Image.open(out).convert("RGB").getdata())
    same = sum(1 for a, b in zip(before, after) if a == b) / len(before)
    assert same > 0.999, f"only {same:.1%} of the pixels survived untouched"


def test_re_drawing_a_light_drawing_onto_a_dark_film_keeps_its_hues(tmp_path):
    """Ink and paper trade places, in HSV — inverting RGB would turn the artery cyan, and a
    schematic whose artery changes colour between two shots is worse than a dark one."""
    src = tmp_path / "light.png"
    im = Image.new("RGB", (800, 600), (250, 250, 250))
    ImageDraw.Draw(im).rectangle([200, 150, 600, 450], fill=RED)
    im.save(src)
    out = visual.redraw(src, tmp_path / "out.png", FRAME, visual.profile([src]))
    with Image.open(out) as placed:
        placed = placed.convert("RGB")
        body = placed.crop((placed.width // 4, placed.height // 3,
                            placed.width * 3 // 4, placed.height * 2 // 3))
    pixels = list(body.getdata())
    assert [px for px in pixels if px[0] > px[1] + 25 and px[0] > px[2] + 25], \
        "the red must survive the tone swap as red"
    assert not [px for px in pixels if px[2] > px[0] + 40], \
        "and must not come out cyan, which is what inverting RGB would do"


def test_a_dark_drawing_is_left_alone_rather_than_inverted(tmp_path):
    """The bug an OCR re-read of the output caught: inverting a picture that is already dark turned
    its white labels black, and the film's dark background swallowed them — the labels were still in
    the file and no longer on the screen."""
    src = tmp_path / "dark.png"
    im = Image.new("RGB", FRAME, (11, 20, 34))
    ImageDraw.Draw(im).rectangle([240, 760, 840, 1160], fill=(240, 244, 250))
    im.save(src)
    out = visual.redraw(src, tmp_path / "out.png", FRAME, visual.profile([src]))
    with Image.open(out) as placed:
        pixels = list(placed.convert("RGB").getdata())
    assert len([px for px in pixels if min(px) > 180]) > len(pixels) * 0.02, \
        "the light fill must come through light, not inverted into the dark"


# --------------------------------------------------------------------- framing it
def test_framing_puts_it_on_the_film_s_own_background(tmp_path):
    src = _photo(tmp_path / "wide.png", (800, 400))
    out = visual.plate_write(src, tmp_path / "plate.png", FRAME, heading="造影", credit="某医院")
    with Image.open(out) as im:
        im = im.convert("RGB")
        assert im.size == FRAME
        assert im.getpixel((5, FRAME[1] - 5)) == visual.FILM_CANVAS


# --------------------------------------------------------------------- the film uses it
def _plan_with(tmp_path, *pictures):
    shots = [{"clip": p.name, "seconds": 3, "say": ""} for p in pictures]
    return shots


def test_assemble_draws_its_stills_again_and_records_the_style(tmp_path):
    import asyncio

    wide = _flat(tmp_path / "wide.png", (1600, 900))
    tall = _flat(tmp_path / "tall.jpg", (1080, 1920), canvas=(250, 250, 250))
    plan = assemble.make_plan(_plan_with(tmp_path, wide, tall), workspace=tmp_path, fit="blur")
    out_dir = tmp_path / "video"
    plan_dir = tmp_path / ".assemble" / "x"
    plan_dir.mkdir(parents=True)
    notes = asyncio.run(assemble._unify(plan, FRAME, plan_dir, out_dir))
    assert [e["unified"] for e in plan] == ["redraw", "redraw"]
    assert all(e["fit"] == "cover" for e in plan)
    assert all(Path(e["source"]).exists() for e in plan)
    assert (out_dir / "style.json").exists()
    assert any("重画" in n or "drawn again" in n for n in notes)


def test_a_shot_that_named_its_own_fit_is_left_alone(tmp_path):
    import asyncio

    wide = _photo(tmp_path / "wide.png", (600, 300))
    shots = _plan_with(tmp_path, wide)
    shots[0]["fit"] = "blur"
    plan = assemble.make_plan(shots, workspace=tmp_path, fit="blur")
    plan_dir = tmp_path / ".assemble" / "y"
    plan_dir.mkdir(parents=True)
    asyncio.run(assemble._unify(plan, FRAME, plan_dir, tmp_path / "video"))
    assert plan[0]["fit"] == "blur" and "unified" not in plan[0]
    assert Path(plan[0]["source"]).name == "wide.png"


def test_the_film_s_style_is_offered_to_the_models(monkeypatch):
    """A rule only in prose is a rule a model forgets on shot 9. It has to be in the text too."""
    monkeypatch.setattr(visual.i18n, "pick_now", lambda en, zh=None: en)
    text = visual.rules()
    assert str(visual.SOFT_SHORT_EDGE) in text and "credit" in text


def test_the_guidance_quotes_the_numbers_the_code_actually_uses():
    """Drift guard, and the reason this test exists: a prompt that quotes a number the code does not
    use is worse than no prompt — the model follows the sentence and the film does the opposite.
    """
    from app import tools

    guide = tools.EXAMPLE_SKILLS["long-form-video"]
    for key in ("body", "body_zh"):
        text = guide[key]
        for n in (visual.SOFT_SHORT_EDGE, visual.MIN_SHORT_EDGE):
            assert str(n) in text, f"{key} does not state {n}"
        # and the tone rule, with the number the code actually uses: the clause exists so a member
        # hands pictures over whole instead of cropping them to "fix" the tone themselves.
        assert f"{visual.FIELD_SHARE:.0%}" in text, f"{key} does not state the page threshold"
        # and both languages have to say the parts that matter most: do not repaint what is already
        # right, do not invent what you have not seen, and say where a picture came from
        assert "style.json" in text and "credit" in text
        if key == "body":
            assert "already on the film's own background" in text
            assert "no web or image search" in text
        else:
            assert "本来就在本片底色上" in text
            assert "没有联网搜图" in text

# --------------------------------------------------------------------- the tone
def test_a_diagram_on_a_page_loses_the_page_and_keeps_its_content(tmp_path):
    """「同等色调」 at the level it actually failed: a white page hung in a dark film.

    Measured on the real film: nine of its thirty stills carried a page, 42-67% of the frame each, and
    every one of them had been *placed* correctly — which is why placement was not the thing to fix.
    """
    src = _on_a_page(tmp_path / "page.png", (900, 1600))
    info = visual.audit(src)
    assert visual.tone_of(info) == "page"
    assert info["field_share"] > visual.FIELD_SHARE

    out = visual.tone_write(src, tmp_path / "out.png", FRAME)
    assert "page removed" in out["applied"]
    with Image.open(out["out"]) as im:
        im = im.convert("RGB")
        assert im.size == FRAME
        assert im.getpixel((4, 4)) == figure.DRAWING_BG          # the page is gone
        content = im.crop((im.width // 2 - 60, im.height // 2 - 60,
                           im.width // 2 + 60, im.height // 2 + 60))
    kinds = set(content.getdata())
    assert any(max(c) - min(c) > 60 for c in kinds), "the content's colour has to survive"


def test_a_projection_on_a_light_field_is_reversed_the_way_a_viewer_shows_it(tmp_path):
    """DICOM's `MONOCHROME1` is exactly this picture, and every viewer reverses it so anatomy reads
    bright on a dark field. It is lossless, and it is the one operation here that touches the content.
    """
    src = _projection(tmp_path / "print.png", (900, 1600))
    info = visual.audit(src)
    assert info["chroma"] < visual.NEUTRAL_CHROMA, "a projection is neutral, which is why it may flip"
    assert visual.tone_of(info) == "flip"

    out = visual.tone_write(src, tmp_path / "out.png", FRAME)
    # Both happen, and the second is not decoration: a flip alone leaves a field of 5,5,5, which is a
    # second dark in a film whose own dark is 11,20,34.
    assert out["applied"] == ["flip", "page removed"]
    with Image.open(out["out"]) as im:
        im = im.convert("RGB")
        assert im.getpixel((4, 4)) == figure.DRAWING_BG          # the field is the film's own
        centre = im.crop((im.width // 2 - 20, im.height // 2 - 20,
                          im.width // 2 + 20, im.height // 2 + 20))
        assert min(centre.convert("L").getdata()) > 120, "the dark anatomy has to come back bright"


def test_a_photograph_that_is_bright_all_over_keeps_every_pixel(tmp_path):
    """The one call here that can destroy something, tested with the picture that would be destroyed.

    Its field colour covers a quarter of it — the same as a page — so the share alone is not enough to
    answer "is there a page under this", and the fix is not to matte it: its brightness is what the
    picture **is**. Only its black end moves.
    """
    src = _bright_photograph(tmp_path / "bright.png", (900, 1600))
    info = visual.audit(src)
    assert info["field_share"] >= visual.FIELD_SHARE, "this is the picture the naive rule would matte"
    assert info["field_inner"] >= 0.5, "and its middle is the same as its edge: a scene, not a page"

    with Image.open(src) as im:
        before = list(im.convert("L").getdata())

    out = visual.tone_write(src, tmp_path / "out.png", FRAME)
    assert out["applied"] == [], "a bright surface is not a lifted shadow: nothing may be graded"
    with Image.open(out["out"]) as im:
        after = sorted(im.convert("L").getdata())
    # The shape already matched, so it fills the frame; and not one value was moved. This is the
    # "deliberate exception" stated as a number rather than as a promise.
    assert after[int(len(after) * 0.01)] > 180, "a bright surface stays bright"
    assert sum(1 for v in after if v > 200) / len(after) > 0.9
    assert max(before) - min(before) < visual.TONE_MIN_RANGE, "…because its whole range is tight"


def test_a_picture_already_on_the_film_canvas_is_not_toned_at_all(tmp_path):
    """The rule that has to survive every other rule: already right means untouched."""
    src = _flat(tmp_path / "home.png", (600, 1000), canvas=figure.DRAWING_BG)
    info = visual.audit(info_path := src)
    assert visual.tone_of(info) == ""
    out = visual.tone_write(src, tmp_path / "out.png", FRAME)
    assert out["applied"] == [] and out["untouched"] is True


def test_the_verdict_says_what_happens_to_the_tone(tmp_path):
    """A decision nobody can see is the failure this project keeps paying for, so the verdict carries
    it and `_unify` can put it in the film's notes."""
    on_page = visual.admit(_on_a_page(tmp_path / "p.png", (900, 1600)), FRAME, credit="x")
    assert on_page["tone"] == "page"
    assert any("tone" in w.lower() or "色调" in w for w in on_page["warnings"])

    home = visual.admit(_flat(tmp_path / "h.png", (1080, 1920), canvas=figure.DRAWING_BG), FRAME,
                        credit="x")
    assert home["tone"] == ""


def test_the_end_matching_only_moves_a_black_that_is_lifted(tmp_path):
    """A grade nobody asked for is its own kind of damage, so two kinds of picture are left alone: one
    that already has a black, and one that is bright all over (which is a surface, not a shadow)."""
    lifted = _lifted(tmp_path / "lifted.png", (600, 900))
    assert visual._ends_lut(Image.open(lifted), film_lum=19.0, paper_lum=243.0)[0] is not None

    dark = _flat(tmp_path / "dark.png", (600, 900), canvas=(10, 14, 20), ink=(200, 200, 200))
    assert visual._ends_lut(Image.open(dark), film_lum=19.0, paper_lum=243.0)[0] is None

    bright = _bright_photograph(tmp_path / "bright.png", (600, 900))
    assert visual._ends_lut(Image.open(bright), film_lum=19.0, paper_lum=243.0)[0] is None
