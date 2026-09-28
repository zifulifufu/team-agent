"""Which intermediates the app may clear away, and which it must leave alone.

⚠️ Every rule here was measured, not imagined. On group `31dd6fef6e27` these two sat side by side:

    .assemble/颅内动脉瘤介入治疗科普/         7.9 MB   abandoned first attempt — no film exists
    .assemble/颅内动脉瘤介入治疗科普-300s/  135.2 MB   finished — `video/颅内动脉瘤介入治疗科普-300s.mp4`

Two names differing by one word, and the finished one's name *begins with* the abandoned one's. A
prefix test destroys the abandoned attempt's only copy; "delete the failed intermediates too" does the
same thing on purpose. Both are pinned below.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app import cleanup

OLD = time.time() - 100 * 3600


def make_old(*paths: Path) -> None:
    """Backdate everything under each path, so the six-hour 'a render is still writing' guard passes."""
    for root in paths:
        for p in sorted(root.rglob("*"), reverse=True):
            os.utime(p, (OLD, OLD))
        os.utime(root, (OLD, OLD))


class Trash:
    """Stands in for `api_ext._to_trash`, which shells out to the Finder.

    It really moves the folder into a trash directory, so "it was swept" is asserted by the folder
    being gone rather than by a flag the test itself set.
    """

    def __init__(self, root: Path, works: bool = True):
        self.dir = root / "trash"
        self.dir.mkdir(exist_ok=True)
        self.works, self.moved = works, []

    def __call__(self, path: Path) -> bool:
        if not self.works:
            return False
        dest = self.dir / path.name
        n = 1
        while dest.exists():          # the Finder's Trash renames on a collision; so does this
            dest = self.dir / f"{path.name}-{n}"
            n += 1
        path.rename(dest)
        self.moved.append(path.name)
        return True


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "video").mkdir()
    (tmp_path / "uploads").mkdir()
    (tmp_path / ".assemble").mkdir()
    return tmp_path


def test_the_parts_of_a_film_that_is_already_there_are_moved_to_the_trash(ws, monkeypatch):
    film = ws / "video" / "科普.mp4"
    film.write_bytes(b"x" * 10)
    parts = ws / ".assemble" / "科普"
    parts.mkdir()
    (parts / "s001.mp4").write_bytes(b"y" * 100)
    make_old(parts)
    trash = Trash(ws)
    monkeypatch.setattr("app.api_ext._to_trash", trash)

    got = cleanup.sweep(ws)

    assert got["moved"] == ["科普"]
    assert got["bytes"] == 100, "the report has to say how much it got back"
    assert not parts.exists(), "the parts were not actually moved"
    assert film.exists(), "the film itself is a deliverable and must never be touched"


def test_a_render_whose_film_is_not_in_the_workspace_is_left_alone(ws, monkeypatch):
    """A render that failed or was abandoned leaves no film — and those parts are the only copy of what
    it did produce. This is the case the user's phrase 'delete the failed intermediates' would remove,
    and it is deliberately refused."""
    parts = ws / ".assemble" / "某片"
    parts.mkdir()
    (parts / "s001.mp4").write_bytes(b"y" * 100)
    make_old(parts)
    trash = Trash(ws)
    monkeypatch.setattr("app.api_ext._to_trash", trash)

    assert cleanup.sweep(ws)["moved"] == []
    assert parts.is_dir() and (parts / "s001.mp4").is_file()


def test_a_name_that_merely_begins_with_the_film_is_not_treated_as_finished(ws, monkeypatch):
    """⭐ The measured trap: `颅内动脉瘤介入治疗科普` vs `…-300s`. Only the second has a film, and the
    film's name begins with the first one's — so a prefix test sweeps the wrong folder."""
    (ws / "video" / "科普-300s.mp4").write_bytes(b"x" * 10)
    finished = ws / ".assemble" / "科普-300s"
    abandoned = ws / ".assemble" / "科普"
    for d in (finished, abandoned):
        d.mkdir()
        (d / "s001.mp4").write_bytes(b"y" * 100)
    make_old(finished, abandoned)
    trash = Trash(ws)
    monkeypatch.setattr("app.api_ext._to_trash", trash)

    assert cleanup.sweep(ws)["moved"] == ["科普-300s"]
    assert abandoned.is_dir(), "the abandoned attempt's parts were thrown away by a prefix match"


def test_a_folder_anything_wrote_to_recently_is_left_alone(ws, monkeypatch):
    """A render in flight writes its parts one at a time, so age is what keeps a running assembly out
    of this — the film may already exist from the previous run."""
    (ws / "video" / "科普.mp4").write_bytes(b"x" * 10)
    parts = ws / ".assemble" / "科普"
    parts.mkdir()
    (parts / "s001.mp4").write_bytes(b"y" * 100)     # fresh mtime, deliberately
    trash = Trash(ws)
    monkeypatch.setattr("app.api_ext._to_trash", trash)

    assert cleanup.sweep(ws)["moved"] == []
    assert parts.is_dir()


def test_what_the_user_fed_in_is_never_touched(ws, monkeypatch):
    (ws / "uploads" / "用户给的录音.m4a").write_bytes(b"z" * 10)
    (ws / "video" / "科普.mp4").write_bytes(b"x" * 10)
    parts = ws / ".assemble" / "科普"
    parts.mkdir()
    (parts / "s001.mp4").write_bytes(b"y" * 100)
    make_old(ws / "uploads", parts)
    trash = Trash(ws)
    monkeypatch.setattr("app.api_ext._to_trash", trash)

    cleanup.sweep(ws)

    assert (ws / "uploads" / "用户给的录音.m4a").is_file(), "user material was removed"
    assert (ws / "video" / "科普.mp4").is_file()
    assert not parts.exists()


def test_a_folder_that_cannot_be_trashed_is_reported_and_left_in_place(ws, monkeypatch):
    """`api_ext._to_trash` returns False when this machine has no way to trash a folder, and the app's
    rule is that it then does nothing at all — never an `rm` behind the user's back."""
    (ws / "video" / "科普.mp4").write_bytes(b"x" * 10)
    parts = ws / ".assemble" / "科普"
    parts.mkdir()
    (parts / "s001.mp4").write_bytes(b"y" * 100)
    make_old(parts)
    monkeypatch.setattr("app.api_ext._to_trash", Trash(ws, works=False))

    got = cleanup.sweep(ws)

    assert got["moved"] == [] and got["refused"] == ["科普"]
    assert parts.is_dir() and (parts / "s001.mp4").is_file(), "it was deleted instead of refused"


def test_plan_reports_without_moving_anything(ws, monkeypatch):
    (ws / "video" / "科普.mp4").write_bytes(b"x" * 10)
    parts = ws / ".assemble" / "科普"
    parts.mkdir()
    (parts / "s001.mp4").write_bytes(b"y" * 100)
    make_old(parts)
    monkeypatch.setattr("app.api_ext._to_trash", Trash(ws, works=False))

    planned = cleanup.plan(ws)

    assert [item["name"] for item in planned] == ["科普"]
    assert planned[0]["bytes"] == 100
    assert parts.is_dir(), "plan is supposed to have no side effects"


def test_the_startup_sweep_walks_every_group(ws, tmp_path, monkeypatch):
    """`sweep_all` is what the app actually runs. A sweep that only ever looked at one workspace would
    leave the other groups' parts behind for good."""
    other = tmp_path / "second"
    for root in (ws, other):
        (root / "video").mkdir(parents=True, exist_ok=True)
        (root / ".assemble" / "科普").mkdir(parents=True, exist_ok=True)
        (root / "video" / "科普.mp4").write_bytes(b"x")
        (root / ".assemble" / "科普" / "s001.mp4").write_bytes(b"y" * 100)
        make_old(root / ".assemble" / "科普")
    trash = Trash(tmp_path)
    monkeypatch.setattr("app.api_ext._to_trash", trash)

    class FakeStore:
        def list_groups(self):
            return [{"id": "a"}, {"id": "b"}]

        def workspace_path(self, gid):
            return {"a": ws, "b": other}[gid]

    got = cleanup.sweep_all(FakeStore())

    assert got["moved"] == ["科普", "科普"] and got["groups"] == 2
    assert not (ws / ".assemble" / "科普").exists()
    assert not (other / ".assemble" / "科普").exists()
