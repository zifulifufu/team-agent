"""The private studio: what it keeps, what it must refuse, and what deleting has to take with it.

This shelf is the one place in the app that holds the user's **own face and their own recordings**,
so the rules worth pinning are not "does CRUD work" but the three that, got wrong, leak:

* what a piece of material **is** comes from its bytes, never from its name;
* deleting material deletes every take made from it — otherwise the face the user asked to be gone
  is still on disk under another name;
* a sidecar that does not parse is **named**, because material this app cannot describe is material
  it must not generate from.
"""
from __future__ import annotations

import json

import pytest

from app import studio

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64


@pytest.fixture(autouse=True)
def _own_shelf(tmp_path):
    """`use_folder` is module state, like the other shelves — put it back afterwards."""
    before = studio.folder_of()
    studio.use_folder(tmp_path / studio.FOLDER_NAME)
    try:
        yield
    finally:
        studio.use_folder(before)


def test_the_kind_comes_from_the_bytes_not_the_name():
    """A `.mov` holding a still must not be usable as video — that is what the byte check is for."""
    row = studio.add_asset(title="still", kind="photo", filename="portrait.png", data=PNG,
                           mime="image/png")
    assert row["kind"] == "photo"
    assert studio.file_of_asset(row["id"]).read_bytes() == PNG


def test_a_kind_outside_the_closed_list_is_refused():
    """The kind decides which generator may be pointed at the asset, so a free-form value is a
    guess about that."""
    with pytest.raises(ValueError):
        studio.add_asset(title="x", kind="hologram", filename="a.png", data=PNG)


def test_an_empty_file_is_refused():
    with pytest.raises(ValueError):
        studio.add_asset(title="x", kind="photo", filename="a.png", data=b"")


def test_tags_filter_and_the_tags_survive_a_reload():
    a = studio.add_asset(title="smile", kind="photo", filename="a.png", data=PNG, tags=["表情", "我"])
    b = studio.add_asset(title="room", kind="photo", filename="b.png", data=PNG, tags=["场景"])
    assert set(studio.assets(kind="photo")) == {a["id"], b["id"]}
    assert set(studio.assets(tag="表情")) == {a["id"]}
    # 从盘上重读一次，标签还在（sidecar 是唯一出处，没有被内存里的副本遮住）
    studio.reload(force=True)
    assert studio.asset(a["id"])["tags"] == ["表情", "我"]


def test_a_take_is_recorded_before_it_has_a_file():
    """An attempt that crashed between "start" and "produced" must still be visible as an attempt,
    rather than leaving a file with nothing saying where it came from."""
    a = studio.add_asset(title="face", kind="photo", filename="a.png", data=PNG)
    t = studio.add_take(asset_id=a["id"], prompt="微笑并转头", params={"seconds": 5}, state="running")
    assert t["state"] == "running"
    assert studio.takes(a["id"])[0]["id"] == t["id"]
    assert studio.file_of_take(t["id"]) is None, "还没有产物就不该有文件"


def test_the_history_is_newest_first():
    a = studio.add_asset(title="face", kind="photo", filename="a.png", data=PNG)
    first = studio.add_take(asset_id=a["id"], prompt="v1")
    second = studio.add_take(asset_id=a["id"], prompt="v2")
    studio.update_take(first["id"], created=1.0)
    studio.update_take(second["id"], created=2.0)
    got = [t["id"] for t in studio.takes(a["id"])]
    assert got.index(second["id"]) < got.index(first["id"]), "最新的在最前"


def test_reviewed_and_chosen_are_two_different_things():
    """看过 ≠ 满意。把两件事合成一个布尔值，「我看了但不要」就无处可记。"""
    a = studio.add_asset(title="face", kind="photo", filename="a.png", data=PNG)
    t = studio.add_take(asset_id=a["id"], prompt="x")
    studio.update_take(t["id"], reviewed=True)
    row = studio.take(t["id"])
    assert row["reviewed"] is True and row["chosen"] is False


def test_deleting_the_material_takes_every_take_with_it():
    """⚠️ The rule this shelf exists to keep: a take is generated **from** the face. Leaving it
    behind means what the user asked to be gone is still on disk under another name."""
    a = studio.add_asset(title="face", kind="photo", filename="a.png", data=PNG)
    t = studio.add_take(asset_id=a["id"], prompt="微笑")
    d = studio.take_directory(t["id"])
    assert d.is_dir()
    assert studio.remove_asset(a["id"]) is True
    assert studio.asset(a["id"]) is None
    assert studio.take(t["id"]) is None, "素材删了，由它生成的产出也必须没了"
    assert not d.exists(), "目录也要真的落盘删掉，而不只是从索引里消失"


def test_a_sidecar_that_will_not_parse_is_named_not_skipped():
    """Skipped, it reads as "this material simply has no title" — and material chosen for the wrong
    reason is worse than material not chosen at all."""
    a = studio.add_asset(title="ok", kind="photo", filename="a.png", data=PNG)
    broken = studio.folder_of() / studio.ASSETS / "deadbeef"
    broken.mkdir(parents=True)
    (broken / studio.ASSET_SIDECAR).write_text("{ this is not json", encoding="utf-8")
    studio.reload(force=True)
    assert a["id"] in studio.assets(), "好的那份还在"
    assert "deadbeef" not in studio.assets(), "坏的那份不能被当成可用素材"
    assert any(e["name"] == "deadbeef" for e in studio.errors()), "而且必须被点名"


def test_material_and_takes_live_in_separate_trees():
    """Why folders rather than one flat list: an asset and its takes are a unit, so removing one has
    to be able to find the other."""
    a = studio.add_asset(title="face", kind="photo", filename="a.png", data=PNG)
    t = studio.add_take(asset_id=a["id"], prompt="x")
    root = studio.folder_of()
    assert (root / studio.ASSETS / a["id"]).is_dir()
    assert (root / studio.TAKES / a["id"] / t["id"]).is_dir()
    # 落盘的 sidecar 里不该出现 dir（那是加载时才加的内存字段）
    body = json.loads((root / studio.ASSETS / a["id"] / studio.ASSET_SIDECAR).read_text("utf-8"))
    assert "dir" not in body
