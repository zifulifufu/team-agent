"""Regressions from the third read-only review (media, updates, backups, Obsidian).

Each case names the failure it protects against: they are all "the second copy of something was
lost while the first was already gone".
"""

from __future__ import annotations

import json

import pytest

from app.catalog import Catalog
from app.obsidian import ObsidianSync
from app.store import Store
from app.updater import GitHubError, Updater, sha256_hex


# ------------------------------------------------------- Obsidian: the copy is the last copy
def test_the_memory_survives_when_the_recovery_copy_cannot_be_written(tmp_path, monkeypatch):
    """The note is already deleted in the vault by the time we get here, so failing to make the
    recovery copy means the memory is the only place the content still exists."""
    st = Store(tmp_path / "data")
    vault = tmp_path / "vault"
    vault.mkdir()
    st.update_settings({"obsidian_dir": str(vault)})
    mem = st.add_memory("结论:这个方案可行", "global", "", "fact")
    ob = ObsidianSync(st)
    ob.sync()                                          # writes the note and maps it
    note = next(vault.rglob("*.md"))
    note.unlink()                                      # the user deleted it in Obsidian

    def boom(*_a, **_kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(ob, "_stash", boom)
    rep = ob.sync()

    assert [m["id"] for m in st.list_memories()] == [mem["id"]], "the memory was deleted anyway"
    assert rep["warnings"], "the failure to keep a copy was not reported"
    assert rep["deleted_memories"] == 0


def test_a_normal_delete_still_works(tmp_path):
    """The guard above must not turn 'deleted in Obsidian' into 'never deleted'.

    Two memories, one note removed: that is an ordinary delete. (Removing the *last* note on purpose
    trips the mass-missing protection instead — it cannot tell that apart from an unmounted vault.)
    """
    st = Store(tmp_path / "data")
    vault = tmp_path / "vault"
    vault.mkdir()
    st.update_settings({"obsidian_dir": str(vault)})
    st.add_memory("留下的记忆", "global", "", "fact")
    st.add_memory("会被删掉的记忆", "global", "", "fact")
    ob = ObsidianSync(st)
    ob.sync()
    notes = list(vault.rglob("*.md"))
    assert len(notes) == 2
    notes[0].unlink()

    rep = ob.sync()

    assert rep["deleted_memories"] == 1
    assert len(st.list_memories()) == 1
    stashed = list(vault.glob("_deleted/*.md")) + list(vault.glob("_已删除/*.md"))
    assert stashed, "no recovery copy was kept"


# ------------------------------------------------------- skill install: preview == installed
async def test_a_skill_install_refuses_content_that_changed_since_the_preview(store):
    up = Updater(store, "0.0.0")
    previewed = "---\nname: Demo\ndescription: d\n---\nbody the user read"

    async def file_now(_repo, _path, _ref=""):
        return {"content": "---\nname: Demo\ndescription: d\n---\nbody that changed", "sha": "s1", "html_url": ""}

    up.file = file_now                                  # type: ignore[assignment]
    with pytest.raises(GitHubError) as err:
        await up.install_skill("owner/repo", "skills/demo/SKILL.md", "", False, sha256_hex(previewed))
    assert err.value.status == 412

    skill = await up.install_skill("owner/repo", "skills/demo/SKILL.md", "", False,
                                   sha256_hex("---\nname: Demo\ndescription: d\n---\nbody that changed"))
    assert skill.name == "Demo"


async def test_a_skill_install_without_a_hash_still_works(store):
    """Automatic updates compare the upstream blob sha to decide *whether* to update, so they pass
    no hash — that path has to keep working."""
    up = Updater(store, "0.0.0")

    async def file_now(_repo, _path, _ref=""):
        return {"content": "---\nname: Auto\ndescription: d\n---\nbody", "sha": "s2", "html_url": ""}

    up.file = file_now                                  # type: ignore[assignment]
    assert (await up.install_skill("owner/repo", "skills/auto/SKILL.md")).name == "Auto"


# ------------------------------------------------------- catalog: crash-safe writes
def test_a_catalog_write_that_fails_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    c = Catalog(tmp_path)
    c.save_override({"version": "1", "providers": {"a": {"name": "A"}}})
    before = c.override_path.read_text(encoding="utf-8")

    def boom(*_a, **_kw):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(json, "dumps", boom)
    with pytest.raises(RuntimeError):
        c.save_override({"version": "2", "providers": {}})

    assert c.override_path.read_text(encoding="utf-8") == before, "the catalog was left broken"
    assert not list(tmp_path.glob("*.tmp")), "a temp file was left behind"
