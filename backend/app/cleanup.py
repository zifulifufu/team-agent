"""Move the intermediates that are provably finished with to the Trash — and nothing else.

⚠️ The classification is **not invented here**. `api_ext.workspace_files` already states the
convention it filters the 成果 panel by: *a dot-prefixed path part is not a deliverable, and that is
the whole convention* (`assemble.SCRATCH == ".assemble"`, `WORKSPACE_SKIP == {".runs", ".tmp",
".extract"}`). The app knows which files are not deliverables — it just only ever **hid** them.

A film that renders leaves its parts behind. Measured 2026-09-28, two video groups:

    31dd6fef6e27   .assemble/   135.2 MB   ← parts of a film that is in `video/` at 116.5 MB
    a54f9f874a82   .assemble/    32.7 MB   ← parts of a film that is in `video/` at  31.9 MB

**168 MB of parts whose finished film was already sitting in the workspace**, against 0.8 MB and
116 MB of actual deliverables. That is the whole of what this module removes.

## What it removes, and what it refuses to

Removes: a `.assemble/<name>/` folder when `<name>` names a **video the workspace already has**.

Refuses, each for a measured reason rather than for caution:

* `uploads/` — the user fed those in. Never touched, whatever their age.
* `.assemble/<name>/` whose video is **not** in the workspace. That is a render that failed or was
  abandoned, and those parts are the only copy of what it did produce. "Delete the failed
  intermediates too" sounds like the same rule and is not — see `_finished` for the measurement.
* `.extract/` — a *cache* of text the app already read out of the workspace's files. Removing it is
  not unsafe, it is pointless: those files get read again, one model call each, and the largest one on
  this machine is 1.1 MB.
* Everything the 成果 panel shows (every non-dot path), at any age.

⚠️ Nothing here ever deletes. A folder goes to the Finder's Trash through `api_ext._to_trash`, and a
folder that cannot be trashed stays exactly where it is — that function's own rule is "this app has no
business removing a user's files", and a group workspace is where months of an agent's work can live.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import attachments
from .assemble import SCRATCH

# A render in flight writes its parts one at a time, so a folder anything wrote to recently is left
# alone. Six hours is far longer than a single assembly takes and far shorter than "next session".
HOURS = 6.0

# `assemble.py` files a run under `tempfile.mkdtemp(prefix=stem + "-")`, which puts exactly one dash
# between the folder and the film's name. Only that shape is unwrapped — never an arbitrary trailing
# word — so `交付-成片-60s.mp4` is not read as "the film 交付-成片".
_RUN_SUFFIX = re.compile(r"^[a-z0-9_]{6,10}$")


def _video_stems(workspace: Path) -> set[str]:
    """The stems of every video the workspace shows — i.e. every film it actually has."""
    out: set[str] = set()
    for path in workspace.rglob("*"):
        try:
            rel = path.relative_to(workspace)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if not path.is_file() or path.is_symlink():
                continue
        except OSError:
            continue
        if attachments.kind_of_name(path.name) == attachments.VIDEO:
            out.add(path.stem)          # `X.mp4.mp4` stems to `X.mp4`, which is what the folder is called
    return out


def _finished(name: str, films: set[str]) -> bool:
    """Is this scratch folder's film already in the workspace?

    ⚠️ **Exact stem equality — never a prefix test.** Measured 2026-09-28 on group `31dd6fef6e27`:
    `.assemble/颅内动脉瘤介入治疗科普/` (the abandoned first attempt, 7.9 MB) sits directly next to
    `.assemble/颅内动脉瘤介入治疗科普-300s/` (the finished one, 135 MB), and only the second has a
    film — `video/颅内动脉瘤介入治疗科普-300s.mp4`. A prefix test counts the **abandoned** folder as
    finished, because the finished film's name *starts with* it, and would throw away the only copy of
    that attempt. The two names differ by one word, which is exactly how close this gets.
    """
    return name in films or _strip_run_suffix(name) in films


def _strip_run_suffix(name: str) -> str:
    head, sep, tail = name.rpartition("-")
    return head if sep and _RUN_SUFFIX.match(tail) else name


def _quiet(path: Path, hours: float) -> bool:
    """Nothing under it has been written for `hours`."""
    try:
        newest = path.stat().st_mtime
    except OSError:
        return False
    for sub in path.rglob("*"):
        try:
            newest = max(newest, sub.stat().st_mtime)
        except OSError:
            continue
    return (time.time() - newest) > hours * 3600


def _bytes(path: Path) -> int:
    total = 0
    for sub in path.rglob("*"):
        try:
            if sub.is_file() and not sub.is_symlink():
                total += sub.stat().st_size
        except OSError:
            continue
    return total


def plan(workspace: Path, *, hours: float = HOURS) -> list[dict]:
    """What `sweep` would move — same rules, no side effects, so it can be shown before it is done."""
    if not workspace.is_dir():
        return []
    scratch = workspace / SCRATCH
    if not scratch.is_dir() or scratch.is_symlink():
        return []
    films = _video_stems(workspace)
    out: list[dict] = []
    for folder in sorted(scratch.iterdir()):
        try:
            if not folder.is_dir() or folder.is_symlink():
                continue
        except OSError:
            continue
        if not _finished(folder.name, films) or not _quiet(folder, hours):
            continue
        out.append({"path": folder, "name": folder.name, "bytes": _bytes(folder)})
    return out


def sweep(workspace: Path, *, hours: float = HOURS) -> dict:
    """Trash every finished-with scratch folder in one workspace. Returns what moved and what could not."""
    from .api_ext import _to_trash       # local import: this is storage-adjacent, api_ext is the router layer
    moved, refused, total = [], [], 0
    for item in plan(workspace, hours=hours):
        if _to_trash(item["path"]):
            moved.append(item["name"])
            total += item["bytes"]
        else:
            refused.append(item["name"])   # no way to trash it here — and it is never `rm`-ed instead
    return {"moved": moved, "refused": refused, "bytes": total}


def sweep_all(store, *, hours: float = HOURS) -> dict:
    """Every group's workspace, summed. What the app runs on its own at startup."""
    moved, refused, total, groups = [], [], 0, 0
    for group in store.list_groups():
        workspace = store.workspace_path(group["id"])
        try:
            if not workspace.is_dir():
                continue
        except OSError:
            continue
        got = sweep(workspace, hours=hours)
        if got["moved"] or got["refused"]:
            groups += 1
        moved += got["moved"]
        refused += got["refused"]
        total += got["bytes"]
    return {"moved": moved, "refused": refused, "bytes": total, "groups": groups}
