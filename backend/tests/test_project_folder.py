"""The folder a project works in, and the task it is on.

Two things the sidebar shows under every project, and both are about **one** fact rather than a copy
of it:

  * the folder is the group's **workspace** — the identical path the chat's workspace panel lists,
    resolved by the same function (`Store.workspace_path`). A second way of working it out would be
    a sidebar pointing at one directory while the agents write into another;
  * the task is read from the group's newest task board, and what is shown follows one rule: what is
    running now, else what is next unfinished, else what it just finished.

And the one destructive action in the app: emptying that folder. What is pinned here is that it
**moves to the Trash** rather than deleting, that it refuses when the caller's idea of what is in
there no longer matches, and that the project keeps working afterwards.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api_ext, planner
from app.main import create_app
from tests.conftest import FakeLLM


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的。"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.data = tmp_path / "data"
        yield c


def gid(c) -> str:
    return c.get("/api/groups").json()[0]["id"]


def a_message(c, gid_: str) -> str:
    store = c.app.state.store
    return store.add_message(gid_, "agent", store.list_agents()[0]["id"], "Storyboard", "分镜好了。")["id"]


def a_board(c, gid_: str, tasks: list[dict]) -> str:
    store = c.app.state.store
    return store.add_message(gid_, "plan", None, "系统", "计划", meta={"kind": "plan", "status": "running", "tasks": tasks})["id"]


# ------------------------------------------------------------------ one folder, one path
def test_the_sidebar_folder_is_the_groups_workspace_and_not_a_second_guess(client):
    """The chat's workspace panel and the sidebar's folder row have to be the same directory: the
    agents write into one place, and a panel pointing somewhere else is how a user concludes the
    work went missing."""
    g = gid(client)
    row = next(x for x in client.get("/api/groups").json() if x["id"] == g)

    assert row["folder"]["path"] == row["workspace_path"]
    assert row["folder"]["path"] == str(client.app.state.store.workspace_path(g))
    assert client.get(f"/api/groups/{g}/folder").json()["path"] == row["folder"]["path"]
    # The workspace endpoint the chat uses reports the same root.
    view = client.get(f"/api/groups/{g}/workspace").json()
    assert view["path"] == row["folder"]["path"], view.keys()


def test_the_name_shown_is_the_shortest_one_that_identifies_the_folder(client, tmp_path):
    """An app-managed folder is named after the group's id — `a54f9f874a82` tells nobody anything —
    so it is shown under the *group's* name. A folder the user picked keeps its own name, because
    that is what they called it."""
    store = client.app.state.store
    g = gid(client)
    row = next(x for x in client.get("/api/groups").json() if x["id"] == g)
    assert row["folder"]["mine"] is False
    assert row["folder"]["name"] == row["name"]
    assert Path(row["folder"]["path"]).name != row["folder"]["name"], "the id is not the name shown"

    mine = tmp_path / "my-project"
    mine.mkdir()
    store.update_group(g, {"workspace": str(mine)})
    row = next(x for x in client.get("/api/groups").json() if x["id"] == g)
    assert row["folder"] == {"name": "my-project", "path": str(mine), "mine": True}


def test_what_is_in_the_folder_is_counted_before_anything_is_done_to_it(client, tmp_path):
    """The confirmation has to say what it means: how many files, how much data, and a few names."""
    store = client.app.state.store
    g = gid(client)
    mine = tmp_path / "proj"
    (mine / "tasks" / "t1").mkdir(parents=True)
    (mine / "a.md").write_text("x" * 100, encoding="utf-8")
    (mine / "tasks" / "t1" / "b.md").write_text("y" * 50, encoding="utf-8")
    store.update_group(g, {"workspace": str(mine)})

    got = client.get(f"/api/groups/{g}/folder").json()
    assert got["files"] == 2 and got["bytes"] == 150
    assert set(got["items"]) == {"a.md", "tasks/t1/b.md"}
    assert got["mine"] is True and got["exists"] is True


# ------------------------------------------------------------------ renaming it
def test_renaming_is_for_the_users_own_folder_only(client, tmp_path):
    """The app-managed folder *is* the mapping from a group to a directory. Renaming it would leave
    a group whose workspace no longer resolves — and the next write would land in a fresh empty
    folder while the files sat next to it — so it is refused with the reason."""
    store = client.app.state.store
    g = gid(client)
    managed = client.post(f"/api/groups/{g}/folder/rename", json={"name": "whatever"})
    assert managed.status_code == 400 and "managed" in managed.text.lower() or "程序管理" in managed.text

    mine = tmp_path / "before"
    mine.mkdir()
    store.update_group(g, {"workspace": str(mine)})
    got = client.post(f"/api/groups/{g}/folder/rename", json={"name": "after"})
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "after"
    assert (tmp_path / "after").is_dir() and not mine.exists()
    # The group follows the folder: the workspace is updated, it is not left pointing at the old name.
    assert store.get_group(g)["workspace"] == str(tmp_path / "after")
    assert store.workspace_path(g) == tmp_path / "after"


def test_a_name_that_is_not_a_name_is_refused(client, tmp_path):
    """A path separator, a parent reference or a hidden name is somebody asking for something else."""
    store = client.app.state.store
    g = gid(client)
    mine = tmp_path / "proj"
    mine.mkdir()
    store.update_group(g, {"workspace": str(mine)})

    for bad in ("", "   ", "../escape", "a/b", ".hidden", "."):
        got = client.post(f"/api/groups/{g}/folder/rename", json={"name": bad})
        assert got.status_code == 400, (bad, got.status_code)
    # …and a name that is already taken is refused rather than merged into.
    (tmp_path / "taken").mkdir()
    assert client.post(f"/api/groups/{g}/folder/rename", json={"name": "taken"}).status_code == 400
    assert mine.is_dir(), "nothing moved"


# ------------------------------------------------------------------ emptying it
def test_emptying_the_folder_moves_it_to_the_trash_and_never_deletes_it(client, monkeypatch, tmp_path):
    """Every write an agent made in this project lives there, so the action is built as the one that
    can destroy work: it goes to the Trash, it needs confirming, and the project keeps working."""
    store = client.app.state.store
    g = gid(client)
    mine = tmp_path / "proj"
    (mine / "tasks").mkdir(parents=True)
    (mine / "tasks" / "out.md").write_text("work", encoding="utf-8")
    store.update_group(g, {"workspace": str(mine)})

    moved: list[str] = []
    trash = tmp_path / "Trash"
    trash.mkdir()

    def fake_trash(path: Path) -> bool:
        moved.append(str(path))
        shutil.move(str(path), str(trash / path.name))     # what the Finder does, not `rm -rf`
        return True

    monkeypatch.setattr(api_ext, "_to_trash", fake_trash)

    # Not confirmed: nothing happens at all.
    assert client.post(f"/api/groups/{g}/folder/delete", json={"confirm": False}).status_code == 400
    assert mine.is_dir() and not moved

    # A wrong idea of what is in there (the dialog was open while things changed): refused, untouched.
    stale = client.post(f"/api/groups/{g}/folder/delete", json={"confirm": True, "expect_files": 99})
    assert stale.status_code == 409 and mine.is_dir() and not moved
    assert (mine / "tasks" / "out.md").is_file()

    got = client.post(f"/api/groups/{g}/folder/delete", json={"confirm": True, "expect_files": 1})
    assert got.status_code == 200, got.text
    assert moved == [str(mine)]
    assert (trash / "proj" / "tasks" / "out.md").is_file(), "it was moved, not deleted"
    # The group is not left pointing at a folder that is in the Trash, and it can work again.
    assert store.get_group(g)["workspace"] == ""
    assert store.workspace_dir(g).is_dir()


def test_a_machine_that_cannot_trash_refuses_instead_of_deleting(client, monkeypatch, tmp_path):
    """No trash helper and no Finder: the answer is "I cannot do this safely", never a plain delete."""
    store = client.app.state.store
    g = gid(client)
    mine = tmp_path / "proj"
    mine.mkdir()
    (mine / "keep.md").write_text("do not lose me", encoding="utf-8")
    store.update_group(g, {"workspace": str(mine)})
    monkeypatch.setattr(api_ext, "_to_trash", lambda path: False)

    got = client.post(f"/api/groups/{g}/folder/delete", json={"confirm": True, "expect_files": 1})
    assert got.status_code == 400 and "Trash" in got.text
    assert (mine / "keep.md").read_text(encoding="utf-8") == "do not lose me"


def test_an_app_managed_folder_is_recreated_empty_after_being_emptied(client, monkeypatch):
    """A managed group must be able to keep writing: its workspace is made again, empty."""
    store = client.app.state.store
    g = gid(client)
    from app import api_ext as mod
    monkeypatch.setattr(mod, "_to_trash", lambda path: True)

    row = client.post(f"/api/groups/{g}/folder/delete", json={"confirm": True})
    assert row.status_code == 200 and row.json()["trashed"]
    assert store.workspace_dir(g).is_dir()
    assert store.get_group(g)["workspace"] == ""


# ------------------------------------------------------------------ what it is doing
def test_the_task_shown_is_what_is_running_then_what_is_next_then_what_just_finished():
    """One rule, three cases, and the order is the point: a step that finished an hour ago must not
    stand in for the one that is running right now."""
    running = {"meta": {"status": "running", "tasks": [
        {"id": "t1", "title": "脚本", "owner": "Copywriter", "status": "done"},
        {"id": "t2", "title": "分镜", "owner": "Storyboard", "status": "running"},
    ]}, "at": 5.0}
    assert planner.headline(running)["title"] == "分镜"
    assert planner.headline(running)["done"] == 1 and planner.headline(running)["total"] == 2

    waiting = {"meta": {"status": "running", "tasks": [
        {"id": "t1", "title": "脚本", "owner": "Copywriter", "status": "done"},
        {"id": "t2", "title": "分镜", "owner": "Storyboard", "status": "pending"},
    ]}, "at": 5.0}
    assert planner.headline(waiting)["title"] == "分镜"

    finished = {"meta": {"status": "done", "tasks": [
        {"id": "t1", "title": "脚本", "owner": "Copywriter", "status": "done"},
        {"id": "t2", "title": "分镜", "owner": "Storyboard", "status": "done"},
    ]}, "at": 5.0}
    assert planner.headline(finished)["title"] == "分镜"
    assert planner.headline(finished)["status"] == "done"
    # A board that never had tasks, or a project that never had a board, has nothing to show.
    assert planner.headline({"meta": {}, "at": 1.0}) is None
    assert planner.headline(None) is None


def test_every_project_row_carries_its_folder_and_its_task(client):
    """One request feeds the whole sidebar: a second request per project is what makes a panel fill
    in row by row."""
    g = gid(client)
    a_board(client, g, [{"id": "t1", "title": "脚本", "owner": "Copywriter", "status": "running"}])
    row = next(x for x in client.get("/api/groups").json() if x["id"] == g)

    assert row["task"]["title"] == "脚本" and row["task"]["total"] == 1
    assert row["folder"]["path"] == row["workspace_path"]
    # …and a project with no board says so by leaving `task` null rather than inventing one.
    empty = client.app.state.store.create_group("空的", None, member_ids=[])["id"]
    other = next(x for x in client.get("/api/groups").json() if x["id"] == empty)
    assert other["task"] is None
