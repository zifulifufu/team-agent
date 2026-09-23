"""A group's workspace: where the user points it, and everything that has to follow.

The workspace used to be one thing only — a folder the app made for each group under its own data
directory. Now the user can point a group at one of their own folders, which means "the group's
workspace" is no longer a formula `<base>/<id>` that any caller can work out for itself: every
reader has to ask the store, and the ones that did not would keep writing into the app's folder
while the user looked at an empty project directory.

That is the failure this file exists to catch — it is silent, and it looks like "nothing happened"
rather than an error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import coderun
from app.main import create_app
from app.store import Store
from tests.conftest import FakeLLM
from tests.test_collab import setup


def client(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="OK"))
    return TestClient(app, base_url="http://127.0.0.1"), app


@pytest.fixture
def api(tmp_path):
    return client(tmp_path, "data")[0]


@pytest.fixture
def project(tmp_path):
    """A directory of the user's own — outside the app's data folder, the way a real one is."""
    d = tmp_path / "my project"
    d.mkdir()
    return d


def make_group(api, project=None, name="A project") -> dict:
    body = {"name": name, "member_ids": [], "host_agent_id": None}
    if project is not None:
        body["workspace"] = str(project)
    r = api.post("/api/groups", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ choosing one
def test_a_group_starts_with_the_folder_the_app_manages(api, tmp_path):
    g = make_group(api)
    assert g["workspace"] == ""
    # The stored value is empty ("the app handles it"), but the page still needs a path to show.
    assert g["workspace_path"] == str(tmp_path / "data" / "workspaces" / g["id"])
    assert Path(g["workspace_path"]).is_dir(), "the folder is made with the group, not on first use"


def test_a_folder_the_user_picks_becomes_the_group_s_workspace(api, project):
    g = make_group(api, project)
    assert g["workspace"] == str(project)
    assert g["workspace_path"] == str(project)
    # and the store agrees, which is what every other reader goes through
    assert api.app.state.store.workspace_dir(g["id"]) == project
    # nothing was made inside the app's own folder for this group
    assert not (project.parent / "data" / "workspaces" / g["id"]).exists()


def test_the_path_is_stored_absolute_and_expanded(api, project, monkeypatch):
    """`~` and a relative spelling are fine to type, but what gets written down is the resolved
    directory — the value is read back by a later process, and two spellings of one folder must not
    look like two different workspaces."""
    monkeypatch.setenv("HOME", str(project.parent))     # so that "~" resolves inside this test
    g = make_group(api, "~/my project")
    assert g["workspace"] == str(project)
    assert g["workspace_path"] == str(project)


def test_it_can_be_changed_and_handed_back(api, project, tmp_path):
    g = make_group(api)
    managed = g["workspace_path"]

    r = api.patch(f"/api/groups/{g['id']}", json={"workspace": str(project)})
    assert r.status_code == 200, r.text
    assert r.json()["workspace_path"] == str(project)

    r = api.patch(f"/api/groups/{g['id']}", json={"workspace": ""})
    assert r.status_code == 200, r.text
    assert r.json()["workspace"] == "" and r.json()["workspace_path"] == managed
    # …and it comes back on the list too, so nothing has to be reloaded specially
    assert next(x for x in api.get("/api/groups").json() if x["id"] == g["id"])["workspace_path"] == managed


# ------------------------------------------------------------------ what is refused
def test_a_directory_that_is_not_there_is_refused_before_a_group_is_made(api, tmp_path):
    before = len(api.get("/api/groups").json())
    r = api.post("/api/groups", json={"name": "x", "member_ids": [], "workspace": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "no directory at" in r.json()["detail"]
    assert len(api.get("/api/groups").json()) == before, "nothing was created"
    assert not (tmp_path / "nope").exists(), "and nothing was invented on disk either"


def test_a_file_is_refused_with_the_reason(api, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi", encoding="utf-8")
    r = api.post("/api/groups", json={"name": "x", "member_ids": [], "workspace": str(f)})
    assert r.status_code == 400 and "not a directory" in r.json()["detail"]


def test_an_unwritable_directory_is_refused(api, project):
    if os.geteuid() == 0:
        pytest.skip("root ignores the permission bits, so this cannot be tested as root")
    project.chmod(0o500)                      # readable, not writable
    try:
        r = api.post("/api/groups", json={"name": "x", "member_ids": [], "workspace": str(project)})
        assert r.status_code == 400 and "may not" in r.json()["detail"]
    finally:
        project.chmod(0o700)


def test_a_bad_path_on_an_existing_group_leaves_it_alone(api, project, tmp_path):
    """The group keeps working in the folder it had; a refused edit must not half-apply."""
    g = make_group(api, project)
    r = api.patch(f"/api/groups/{g['id']}", json={"workspace": str(tmp_path / "gone")})
    assert r.status_code == 400
    assert api.app.state.store.workspace_dir(g["id"]) == project


# ------------------------------------------------------------------ everything follows the choice
async def test_a_run_lands_in_the_folder_the_user_picked(store, make_router, project):
    """The end-to-end version of the whole feature: a member writes a file, and it appears in the
    user's own directory rather than in the app's."""
    store.update_settings({"code_enabled": True, "code_timeout": 20})
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    store.update_group(g["id"], {"workspace": str(project)})

    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    out = await orch.toolhub.call(ctx, "run_code",
                                  {"language": "python", "code": "open('made-by-a-member.txt','w').write('hi')"},
                                  None)

    assert out.ok, out.text
    assert (project / "made-by-a-member.txt").read_text() == "hi"
    assert not (Path(store.data_dir) / "workspaces" / g["id"]).exists()


def test_an_uploaded_file_lands_in_the_chosen_folder(api, project):
    """Attachments go to the group's workspace too, and they were the call site most likely to be
    left behind: the row only stores a path relative to the workspace."""
    g = make_group(api, project)
    r = api.post(f"/api/groups/{g['id']}/attachments", params={"filename": "notes.txt"},
                 content=b"hello there", headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    assert list((project / "uploads").glob("*.txt")), "the bytes are in the user's folder"
    assert not (project.parent / "data" / "workspaces" / g["id"]).exists()


def test_an_attachment_is_read_back_out_of_the_chosen_folder(api, project):
    """…and reading it again has to resolve the same way, or the file is stored and then invisible."""
    g = make_group(api, project)
    row = api.post(f"/api/groups/{g['id']}/attachments", params={"filename": "notes.txt"},
                   content=b"hello there", headers={"Content-Type": "application/octet-stream"}).json()
    r = api.get(f"/api/attachments/{row['id']}")
    assert r.status_code == 200 and r.content == b"hello there"


def test_a_generated_clip_is_served_out_of_the_chosen_folder(api, project):
    """The route that hands a generated video back resolves it inside the group's workspace. If it
    still resolved `<base>/<id>` it would answer 404 for a file that is plainly there."""
    g = make_group(api, project)
    (project / "video").mkdir()
    (project / "video" / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    r = api.get(f"/api/groups/{g['id']}/video/clip.mp4")
    assert r.status_code == 200 and r.content.startswith(b"\x00\x00\x00\x18ftyp")


def test_the_workspace_view_says_which_kind_it_is(api, project):
    managed = make_group(api, name="managed")
    picked = make_group(api, project, name="picked")
    assert api.get(f"/api/groups/{managed['id']}/workspace").json()["managed"] is True
    view = api.get(f"/api/groups/{picked['id']}/workspace").json()
    assert view["managed"] is False and view["path"] == str(project)


# ------------------------------------------------------------------ the startup backfill
def test_the_backfill_makes_managed_folders_and_leaves_the_users_alone(tmp_path):
    """`ensure_workspaces` runs at every start. For a group the user pointed somewhere it must do
    nothing: recreating a folder they deleted on purpose is not a decision a startup routine gets
    to make."""
    data = tmp_path / "data"
    st = Store(data)
    managed = st.create_group("managed")
    picked = st.create_group("picked", workspace=str(tmp_path / "deleted-by-the-user"))
    settings = st.get_settings()

    coderun.ensure_workspaces(data, settings, [(g["id"], g["workspace"]) for g in st.list_groups()])

    assert (data / "workspaces" / managed["id"]).is_dir()
    assert not (tmp_path / "deleted-by-the-user").exists()
    # It is still made when something actually needs it — a decision with somebody behind it.
    assert st.workspace_dir(picked["id"]) == tmp_path / "deleted-by-the-user"


def test_a_gid_is_still_checked(tmp_path):
    st = Store(tmp_path / "data")
    with pytest.raises(ValueError):
        st.workspace_dir("../../etc")


# ------------------------------------------------------------------ the resolver itself
def test_the_pure_resolver_prefers_the_chosen_path(tmp_path, project):
    settings = {}
    data = tmp_path / "data"
    assert coderun.workspace_path(data, settings, "abc") == data / "workspaces" / "abc"
    assert coderun.workspace_path(data, settings, "abc", str(project)) == project
    # No group, no run: the base is returned for display only.
    assert coderun.workspace_path(data, settings, "") == data / "workspaces"
    assert coderun.normalise_workspace("") is None and coderun.normalise_workspace("   ") is None


def test_a_template_still_gets_the_managed_folder(store, make_router):
    """A template names its own members on purpose — the one place where choosing for the user is
    the feature. The workspace works the same way: nothing picked, so the app manages it."""
    from app import templates

    g = templates.create_group_from_template(store, templates.TEMPLATES[0]["id"])
    assert g and g["workspace"] == ""
    assert store.workspace_dir(g["id"]).is_dir()
