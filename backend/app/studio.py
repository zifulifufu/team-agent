"""The private studio: your own footage, and every take made from it.

Why this exists as a module of its own: three of the video zone's blocks — expressions and motion,
the digital human, and scenes — are the same shape the user described in one sentence: **upload your
own material → keep it private → generate from a text prompt → look at the result → fix it → keep
the history → choose one for the film.** Building that shape three times would be three places for
the privacy rule to be got wrong, so it is built once here.

⚠️⚠️ **This shelf is private, and that is the whole point of it.** Everything else in this app that
holds pictures or clips shares them: the device reference library is copied into every group's
workspace on purpose, and a group's own output is meant to be read by the members. This one is the
opposite — it holds the user's own face and their own footage, so:

* it lives under `<data dir>/studio/`, **not** in any group's workspace and **not** in a knowledge
  base. Nothing here is picked up by `list_workspace_files`, by library search, or by an export;
* members reach it only through an explicit `asset:` reference, and what comes back is the **take**
  they asked for — never the original material;
* deleting an asset takes its takes with it (`remove_asset`), because a face that has been deleted
  from the shelf must not still be reachable through something derived from it.

The layout is the same three-part shape the other user-addable shelves use (`voices/`, `layouts/`,
`music/`, `workflows/`): a folder per thing, a sidecar saying what it is, and a name to refer to it
by. Folders rather than one flat directory because an asset and its takes are a unit — moving or
removing one has to move or remove the other.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import i18n

__all__ = ["FOLDER_NAME", "KINDS", "use_folder", "reload", "folder_of", "assets", "asset", "errors",
           "takes", "take", "add_asset", "update_asset", "remove_asset", "add_take", "update_take",
           "remove_take", "file_of_asset", "file_of_take", "asset_directory", "take_directory"]

FOLDER_NAME = "studio"
ASSETS = "assets"
TAKES = "takes"
ASSET_SIDECAR = "asset.json"
TAKE_SIDECAR = "take.json"

# What the material is. Closed on purpose: it decides which generator can be pointed at it, and a
# free-form string would make "which of these can drive a talking head" a guess.
KINDS = ("photo", "video", "audio")

_FOLDER: "Path | None" = None
_ASSETS: dict[str, dict] = {}
_TAKES: dict[str, dict] = {}          # take id -> row (with its asset id inside)
_ERRORS: list[dict] = []
_SIGN: tuple = ()


def use_folder(folder: "str | Path | None") -> dict:
    """Point the studio at a directory. Called once from `create_app`, like the other shelves."""
    global _FOLDER
    _FOLDER = Path(folder).expanduser() if folder else None
    return reload(force=True)


def folder_of() -> "Path | None":
    return _FOLDER


def _note(name: str, why: str, path: str = "") -> None:
    _ERRORS.append({"name": name, "why": why, "path": path})


def _slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(text or "")).strip("-._")
    return (s[:limit] or "asset")


def _sign() -> tuple:
    """Cheap change detector: both trees' mtimes plus how many sidecars are in each.

    The count matters as much as the mtime: two takes written inside the same filesystem timestamp
    tick would otherwise look like no change at all, and the page would keep showing one result.
    """
    if _FOLDER is None:
        return ()
    out = []
    for sub in (ASSETS, TAKES):
        d = _FOLDER / sub
        try:
            out.append(d.stat().st_mtime if d.is_dir() else 0.0)
            out.append(sum(1 for _ in d.rglob("*.json")) if d.is_dir() else 0)
        except OSError:
            out += [0.0, 0]
    return tuple(out)


def reload(*, force: bool = False) -> dict:
    """Re-read both trees. Called on startup and after every write."""
    global _ASSETS, _TAKES, _ERRORS, _SIGN
    if _FOLDER is None:
        _ASSETS, _TAKES, _ERRORS, _SIGN = {}, {}, [], ()
        return {"assets": 0, "takes": 0, "errors": 0}
    sign = _sign()
    if not force and sign == _SIGN:
        return {"assets": len(_ASSETS), "takes": len(_TAKES), "errors": len(_ERRORS)}
    _SIGN = sign
    _ASSETS, _TAKES, _ERRORS = {}, {}, []

    root = _FOLDER / ASSETS
    if root.is_dir():
        for sidecar in sorted(root.rglob(ASSET_SIDECAR)):
            body = _read(sidecar)
            if body is None:
                continue
            aid = str(body.get("id") or sidecar.parent.name)
            body["id"] = aid
            body["dir"] = str(sidecar.parent)
            _ASSETS[aid] = body

    root = _FOLDER / TAKES
    if root.is_dir():
        for sidecar in sorted(root.rglob(TAKE_SIDECAR)):
            body = _read(sidecar)
            if body is None:
                continue
            tid = str(body.get("id") or sidecar.parent.name)
            body["id"] = tid
            body["dir"] = str(sidecar.parent)
            _TAKES[tid] = body
    return {"assets": len(_ASSETS), "takes": len(_TAKES), "errors": len(_ERRORS)}


def _read(sidecar: Path) -> "dict | None":
    """A sidecar that does not parse is **named and skipped**, the same rule the other shelves use.

    Passing it over in silence would leave an asset that looks like it simply has no title and no
    tags — and material chosen for the wrong reason is worse than material not chosen at all. Here
    it is more than untidy: a face this app can no longer describe is one it must not generate from.
    """
    try:
        body = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _note(sidecar.parent.name, i18n.pick_now(
            f"Its sidecar could not be read ({type(e).__name__}), so it is not usable.",
            f"它的 sidecar 读不出来（{type(e).__name__}），所以不能用。"),
            str(sidecar))
        return None
    if not isinstance(body, dict):
        _note(sidecar.parent.name, i18n.pick_now("Its sidecar is not an object.",
                                                 "它的 sidecar 不是一个对象。"), str(sidecar))
        return None
    return body


def errors() -> list[dict]:
    return list(_ERRORS)


# ------------------------------------------------------------------ the assets
def assets(*, kind: str = "", tag: str = "") -> dict[str, dict]:
    """Everything usable, newest first, optionally filtered. Filtering here rather than in the page
    keeps "what can drive a talking head" one answer."""
    reload()
    rows = list(_ASSETS.values())
    if kind:
        rows = [r for r in rows if str(r.get("kind")) == kind]
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    rows.sort(key=lambda r: float(r.get("created") or 0), reverse=True)
    return {r["id"]: r for r in rows}


def asset(asset_id: str) -> "dict | None":
    reload()
    return _ASSETS.get(str(asset_id))


def asset_directory(asset_id: str) -> "Path | None":
    row = asset(asset_id)
    return Path(row["dir"]) if row else None


def file_of_asset(asset_id: str) -> "Path | None":
    """The material itself. Only the backend asks for this — a member gets takes, not originals."""
    d = asset_directory(asset_id)
    if d is None or not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name != ASSET_SIDECAR and not p.name.startswith("."):
            return p
    return None


def add_asset(*, title: str, kind: str, filename: str, data: bytes, tags=(),
              note: str = "", mime: str = "") -> dict:
    """Put one piece of the user's own material on the shelf. Returns the row.

    What it is (`kind`) is taken from the caller, which got it from the file itself — not from the
    extension: a `.mov` that is really a still is a thing that happens, and it decides whether this
    asset can be used as a first frame or as a voice. `mime` is recorded for the same reason — when
    the page previews the material, a wrong media type makes a player refuse a perfectly good file.
    """
    if _FOLDER is None:
        raise KeyError(i18n.pick_now("There is no studio folder, so nothing can be kept.",
                                     "没有可用的工作室目录，所以什么都存不下。"))
    k = str(kind or "").strip().lower()
    if k not in KINDS:
        raise ValueError(i18n.pick_now(
            f"\"{kind}\" is not one of {', '.join(KINDS)} — the kind decides which generator can use it.",
            f"「{kind}」不在 {', '.join(KINDS)} 里 —— 类型决定哪个生成器能用它。"))
    if not data:
        raise ValueError(i18n.pick_now("That file was empty.", "这个文件是空的。"))

    from .store import new_id
    aid = new_id()
    suffix = Path(filename or "").suffix.lower()[:10] or ".bin"
    d = _FOLDER / ASSETS / aid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"source{suffix}").write_bytes(data)
    row = {"id": aid, "title": str(title or "").strip() or Path(filename or "").stem or aid,
           "kind": k, "tags": [str(t) for t in tags if str(t).strip()],
           "note": str(note or "").strip(), "created": time.time(),
           "bytes": len(data), "original_name": str(filename or ""), "mime": str(mime or "")}
    (d / ASSET_SIDECAR).write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
    reload(force=True)
    return row


def update_asset(asset_id: str, **patch) -> dict:
    row = asset(asset_id)
    if row is None:
        raise KeyError(asset_id)
    for key in ("title", "note"):
        if key in patch and patch[key] is not None:
            row[key] = str(patch[key]).strip()
    if "tags" in patch and patch["tags"] is not None:
        row["tags"] = [str(t) for t in patch["tags"] if str(t).strip()]
    if "kind" in patch and str(patch["kind"]).lower() in KINDS:
        row["kind"] = str(patch["kind"]).lower()
    Path(row["dir"], ASSET_SIDECAR).write_text(json.dumps(_clean(row), ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    reload(force=True)
    return asset(asset_id) or row


def remove_asset(asset_id: str) -> bool:
    """Remove the material **and every take made from it**.

    ⚠️ Taking the takes too is the point, not tidiness: a take is generated *from* the face that was
    just deleted, and leaving it behind means the thing the user asked to be gone is still on disk
    under another name. The two directories are removed together, and never one without the other.
    """
    import shutil
    d = asset_directory(asset_id)
    if d is None:
        return False
    try:
        shutil.rmtree(d)
        t = _FOLDER / TAKES / str(asset_id) if _FOLDER else None
        if t and t.is_dir():
            shutil.rmtree(t)
    except OSError as e:
        _note(asset_id, i18n.pick_now(f"Could not remove it: {e}", f"删不掉它：{e}"))
        return False
    reload(force=True)
    return True


# ------------------------------------------------------------------ the takes
def takes(asset_id: str) -> list[dict]:
    """One asset's generation history, newest first — this is what "look at the result and fix it"
    reads: the versions sit side by side and none of them overwrites another."""
    reload()
    rows = [r for r in _TAKES.values() if str(r.get("asset")) == str(asset_id)]
    rows.sort(key=lambda r: float(r.get("created") or 0), reverse=True)
    return rows


def take(take_id: str) -> "dict | None":
    reload()
    return _TAKES.get(str(take_id))


def take_directory(take_id: str) -> "Path | None":
    row = take(take_id)
    return Path(row["dir"]) if row else None


def file_of_take(take_id: str) -> "Path | None":
    d = take_directory(take_id)
    if d is None or not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name != TAKE_SIDECAR and not p.name.startswith("."):
            return p
    return None


def add_take(*, asset_id: str, prompt: str, params: "dict | None" = None, state: str = "running",
             name: str = "") -> dict:
    """Open a record for one generation, before it has produced anything.

    ⚠️ Written **first**, the same way `music.publish` writes its sidecar first: a crash between the
    two then leaves "a record of an attempt with no file", which the page can show and explain,
    rather than a file with nothing saying where it came from.
    """
    if _FOLDER is None:
        raise KeyError(i18n.pick_now("There is no studio folder.", "没有可用的工作室目录。"))
    if asset(asset_id) is None:
        raise KeyError(asset_id)
    from .store import new_id
    tid = new_id()
    d = _FOLDER / TAKES / str(asset_id) / tid
    d.mkdir(parents=True, exist_ok=True)
    row = {"id": tid, "asset": str(asset_id), "name": _slug(name or prompt or "take"),
           "prompt": str(prompt or ""), "params": dict(params or {}), "state": state,
           "created": time.time(), "seconds": 0.0, "bytes": 0,
           "reviewed": False, "chosen": False, "error": ""}
    (d / TAKE_SIDECAR).write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
    reload(force=True)
    return row


def update_take(take_id: str, **patch) -> dict:
    row = take(take_id)
    if row is None:
        raise KeyError(take_id)
    for key in ("state", "error", "name", "prompt"):
        if key in patch and patch[key] is not None:
            row[key] = str(patch[key])
    for key in ("seconds", "bytes"):
        if key in patch and patch[key] is not None:
            row[key] = float(patch[key])
    for key in ("reviewed", "chosen"):
        if key in patch and patch[key] is not None:
            row[key] = bool(patch[key])
    if "params" in patch and patch["params"] is not None:
        row["params"] = dict(patch["params"])
    Path(row["dir"], TAKE_SIDECAR).write_text(json.dumps(_clean(row), ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    reload(force=True)
    return take(take_id) or row


def remove_take(take_id: str) -> bool:
    import shutil
    d = take_directory(take_id)
    if d is None:
        return False
    try:
        shutil.rmtree(d)
    except OSError as e:
        _note(take_id, i18n.pick_now(f"Could not remove it: {e}", f"删不掉它：{e}"))
        return False
    reload(force=True)
    return True


def _clean(row: dict) -> dict:
    """What actually goes into a sidecar: the in-memory row minus the two fields the loader adds."""
    return {k: v for k, v in row.items() if k not in ("dir",)}
