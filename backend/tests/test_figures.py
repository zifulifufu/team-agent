"""Teaching pictures: a real figure out of a note, and a schematic drawn here.

Two claims are being defended. First, that a group can reach the figures its documents came with —
which is the difference between "search 300 files" and "put the right picture on screen". Second,
that a drawing made here is controllable: it says what it is, its parts are where it says they are,
and a label points at the thing it names. Both were wrong at some point today and a reviewer looking
at a still caught it, which is what the still review exists for.
"""

from __future__ import annotations

import pathlib

import pytest

from app import figure
from app.library import note_figures
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_collab import setup


def note(tmp_path, body: str, name: str = "note.md") -> pathlib.Path:
    vault = tmp_path / "Vault"
    (vault / "shots").mkdir(parents=True, exist_ok=True)
    (vault / "shots" / "fig-one.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 40)
    (vault / name).write_text(body, encoding="utf-8")
    return vault / name


# ------------------------------------------------------------------ a document's own figures
def test_a_note_hands_over_the_pictures_it_came_with(tmp_path):
    """The bridge the whole flow depends on: a document is not only its words."""
    src = note(tmp_path, "---\nsource: https://example.org/x\ntitle: A\n---\n\n![[shots/fig-one.jpg]]\n")
    figs, meta = note_figures(str(src))

    assert [f["name"] for f in figs] == ["fig-one.jpg"]
    assert figs[0]["kind"] == "figure" and pathlib.Path(figs[0]["path"]).is_file()
    # The provenance comes back with them, because a credit line is built from it.
    assert meta["source"] == "https://example.org/x"


def test_a_width_hint_and_a_bare_name_both_resolve(tmp_path):
    """Obsidian writes both `![[path/name.jpg|240]]` (relative to the vault root) and `![[name.mp4]]`
    (next to the note), and the notes in this user's vault use both."""
    vault = tmp_path / "Vault"
    vault.mkdir(parents=True)
    (vault / "clip.mp4").write_bytes(b"\x00" * 16)
    (vault / "shots").mkdir()
    (vault / "shots" / "fig.jpg").write_bytes(b"\xff\xd8" + b"0" * 10)
    src = vault / "note.md"
    src.write_text("![[shots/fig.jpg|240]]\n\n![[clip.mp4]]\n", encoding="utf-8")

    names = [f["name"] for f in note_figures(str(src))[0]]

    assert names == ["fig.jpg", "clip.mp4"] or names == ["clip.mp4", "fig.jpg"]
    kinds = {f["name"]: f["kind"] for f in note_figures(str(src))[0]}
    assert kinds["clip.mp4"] == "clip" and kinds["fig.jpg"] == "figure"


@pytest.mark.parametrize("link", [
    "![[/etc/passwd]]",                       # absolute
    "![[../../../../etc/hosts]]",             # climbing out
    "![[https://example.org/x.jpg]]",         # remote
])
def test_a_note_cannot_turn_a_line_into_a_path_this_app_reads(tmp_path, link):
    """A note is data from somewhere else. The one thing this must never do is let a line in it name
    a file this app will read from anywhere on the machine."""
    src = note(tmp_path, f"{link}\n")
    assert note_figures(str(src))[0] == []


def test_a_document_with_no_pictures_says_so_and_names_one_that_has_them(store, make_router,
                                                                          tmp_path):
    """A dead end that names an exit. 'No pictures' is worth saying only alongside where pictures are."""
    import asyncio
    from app.library import Library
    lib = Library(store)
    kb = lib.shared_kb()
    lib.add_text("Only words", "no pictures in this one", kind="note", kb_id=kb["id"])
    # Imported from a folder, which is the path that records the note's real location on disk — and
    # the location is what the figures are read from.
    withpix = tmp_path / "Vault"
    (withpix / "shots").mkdir(parents=True)
    (withpix / "shots" / "a.jpg").write_bytes(b"\xff\xd8" + b"0" * 8)
    (withpix / "Rich.md").write_text("---\nsource: https://e.org\n---\n![[shots/a.jpg]]\n",
                                     encoding="utf-8")
    lib.add_dir(str(withpix), kb_id=kb["id"])
    orch, g = setup(store, make_router, FakeLLM(default="x"))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))

    out = asyncio.run(orch.toolhub.call(ctx, "list_figures", {"doc": "Only words"}, None))
    assert out.ok
    assert "nothing real to show" in out.text.lower() or "没有可放的真实画面" in out.text
    # The dead end names the documents that would not be one — that is the whole value of saying it.
    assert "Rich(1)" in out.text
    listed = asyncio.run(orch.toolhub.call(ctx, "list_figures", {"doc": "Rich"}, None))
    assert listed.ok and "a.jpg" in listed.text and "e.org" in listed.text


# ------------------------------------------------------------------ drawing a frame
def test_a_frame_is_drawn_with_its_heading_caption_and_credit(tmp_path):
    """The three lines that make a picture usable in a film: what we are looking at, what to notice,
    and where it came from. Without the third the film cannot be published."""
    src = tmp_path / "wide.png"
    from PIL import Image
    Image.new("RGB", (400, 300), (200, 60, 60)).save(src)

    out = figure.compose(src, tmp_path / "f.png", (400, 700), heading="标题",
                         caption="图注", credit="来源:测试")
    from PIL import Image as I
    im = I.open(out).convert("RGB")
    assert im.size == (400, 700)
    # Top band, picture in the middle, caption area at the bottom — checked on pixels, because
    # "compose ran without raising" says nothing about where anything landed.
    assert im.getpixel((200, 10))[2] > 30 and sum(im.getpixel((200, 10))) < 200
    assert im.getpixel((200, 350))[0] > 150
    assert sum(im.getpixel((200, 690))) < 120


def test_a_schematic_knows_where_its_own_parts_are():
    """The fix for a mistake worth not repeating: a label's position was guessed, and it pointed at
    empty space. A drawing made here knows its own geometry, so nobody has to guess."""
    g = figure.schematic_geometry((1080, 1920), {"sac_at_x": 0.62, "sac_r": 0.13})

    for name in ("sac", "coil", "catheter_tip", "vessel", "flow_in", "flow_out"):
        x, y = g[name]
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0, (name, g[name])
    # The sac sits above the vessel, not on it — that is what makes it read as a bulge.
    assert g["sac"][1] < g["vessel"][1]
    assert abs(g["sac"][0] - 0.62) < 0.01 and g["_rr"] == pytest.approx(1080 * 0.13)


def test_a_label_lands_on_its_anchor_and_not_on_top_of_another_label(tmp_path):
    """Three labels whose anchors are within a few percent of each other must not stack, and none of
    them may cover the point it names. A reviewer read the first attempt as three plates hiding the
    aneurysm they were naming."""
    draw = figure.schematic("aneurysm_coiling", (600, 1000), {
        "labels": [{"text": "动脉瘤瘤腔", "at_part": "sac"},
                   {"text": "微导管", "at_part": "catheter_tip"},
                   {"text": "弹簧圈", "at_part": "coil"}]})
    plate = __import__("PIL.Image", fromlist=["Image"]).new("RGB", (600, 1000), (11, 20, 34))
    from PIL import ImageDraw
    # Run the label placement on its own and look at the plates it chose.
    taken: list = []
    f = figure.font(20)
    d = ImageDraw.Draw(plate)
    g = figure.schematic_geometry((600, 1000), {})
    for part in ("sac", "catheter_tip", "coil"):
        figure._tag(d, f"标签{part}", figure._px(g[part], (600, 1000), "x"), f, (600, 1000), taken)

    assert len(taken) == 3
    for i in range(3):
        for j in range(i + 1, 3):
            assert figure._overlap(taken[i], taken[j]) == 0, (taken[i], taken[j])
        # …and no plate sits on the anchor it names. (It may sit near a *different* anchor — with
        # three of them within a couple of percent of each other, something has to give.)
        x, y = figure._px(g[("sac", "catheter_tip", "coil")[i]], (600, 1000), "x")
        assert figure._overlap(taken[i], (x - 4, y - 4, x + 4, y + 4)) == 0
    plate.save(tmp_path / "labels.png")


def test_a_schematic_says_it_is_a_drawing(tmp_path):
    """A drawing that does not say it is a drawing is the other way to mislead."""
    from app import i18n
    i18n.set_current("zh")
    try:
        assert "示意" in figure.drawing_note()
    finally:
        i18n.set_current("en")


def test_a_schematic_that_does_not_exist_is_refused_with_the_list():
    with pytest.raises(figure.FigureError) as e:
        figure.schematic("heart_surgery", (400, 700), {})
    assert "aneurysm_coiling" in str(e.value)


@pytest.mark.parametrize("at", [[2.0, 0.5], ["x", "y"], [0.5], None])
def test_a_mark_outside_the_frame_is_refused(tmp_path, at):
    """A mark that lands off-canvas is a caller error worth hearing about — the model cannot measure a
    picture, so it must be told when the number it invented is not a number."""
    with pytest.raises(figure.FigureError):
        figure._px(at, (400, 700), "mark")


def test_a_frame_never_overwrites_another_one(tmp_path):
    """Two frames from the same subject are two files, not one overwriting the other — and never a
    path that already exists, which is also what stops a planted symlink being the file that is
    opened."""
    first = figure.workspace_output(tmp_path, "figures", "shot")
    assert not first.exists()
    first.write_bytes(b"x")
    second = figure.workspace_output(tmp_path, "figures", "shot")
    assert second.name == "shot-2.png" and not second.exists()


def test_a_figures_folder_that_is_a_symlink_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "figures").symlink_to(outside)
    with pytest.raises(figure.FigureError):
        figure.workspace_output(tmp_path, "figures", "x")


# ------------------------------------------------------------------ through the tool
def test_the_tools_are_offered_and_a_schematic_comes_back_as_a_still(store, make_router):
    """End to end through the real handler: a group asks for the coiling schematic and gets a file it
    can hand to `assemble_video`, whose parts are reported as fractions so the next mark can be
    placed without guessing."""
    import asyncio
    orch, g = setup(store, make_router, FakeLLM(default="x"))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))
    assert "make_figure" in ctx.tools

    out = asyncio.run(orch.toolhub.call(ctx, "make_figure", {
        "schematic": "aneurysm_coiling", "size": "480x854", "name": "coil-shot",
        "heading": "弹簧圈栓塞", "caption": "导管经血管腔进入瘤腔,弹簧圈盘绕成团。",
        "params": {"labels": [{"text": "瘤腔", "at_part": "sac"}]}}, None))

    assert out.ok, out.text
    assert out.files and out.files[0]["kind"] == "image"
    assert "sac=" in out.text
    # The frame has to say it is a drawing, in whichever language the request is in — that line is
    # written into the credit, not left to the caller to remember.
    assert "schematic" in out.text.lower() or "示意图" in out.text
    shot = pathlib.Path(store.workspace_dir(g["id"])) / "figures" / "coil-shot.png"
    assert shot.is_file() and shot.stat().st_size > 4000
    info = figure.FigureError and __import__("PIL.Image", fromlist=["Image"]).open(shot)
    assert info.size == (480, 854)


def test_one_source_only_and_it_has_to_be_one_that_exists(store, make_router):
    """Three sources are three different things; asking for two is asking for nothing, and the answer
    says so rather than picking one."""
    import asyncio
    orch, g = setup(store, make_router, FakeLLM(default="x"))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))

    both = asyncio.run(orch.toolhub.call(ctx, "make_figure",
                                         {"schematic": "aneurysm", "image": "x.png"}, None))
    assert not both.ok and "exactly one" in both.text
    none = asyncio.run(orch.toolhub.call(ctx, "make_figure", {}, None))
    assert not none.ok
    missing = asyncio.run(orch.toolhub.call(ctx, "make_figure", {"image": "nope.png"}, None))
    assert not missing.ok


def test_a_recording_is_not_a_still_and_the_answer_says_what_to_do_instead(store, make_router,
                                                                          tmp_path):
    """A case collection's most valuable asset is its recordings. They cannot be a still, and the
    refusal has to point at `assemble_video` rather than leave the caller guessing."""
    import asyncio
    from app.library import Library
    lib = Library(store)
    vault = tmp_path / "V"
    vault.mkdir()
    (vault / "case.mp4").write_bytes(b"\x00" * 32)
    (vault / "Case.md").write_text("![[case.mp4]]\n", encoding="utf-8")
    lib.add_dir(str(vault), kb_id=lib.shared_kb()["id"])
    orch, g = setup(store, make_router, FakeLLM(default="x"))
    ctx = asyncio.run(orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                          connect=False))

    out = asyncio.run(orch.toolhub.call(ctx, "make_figure", {"doc": "Case", "figure": "1"}, None))

    assert not out.ok and "assemble_video" in out.text
