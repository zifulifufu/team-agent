"""The music shelf: what a film can be scored with, where each track came from, and which one fits.

A music shelf is `<data dir>/music/` full of audio files, plus an **optional** sidecar `.json` of the
same name saying what the track is: its mood, its tags, where it came from, under what licence, its
BPM. A file with no sidecar still plays — its name is its label — it just carries less to choose on.

Why the choosing deserves a module of its own: what a "background music director" does is not
"hand over a track", it is **pick the right one for this film** — what the film is about, how long it
is, whether anyone is speaking over it and in what mood decide which track, at what level, and
whether the music should get out of the narrator's way. Those reasons can only be trusted if they are
written down next to the file, so they are data (the sidecar), not a sentence inside a prompt.

The rules are the same four as the other shelves (`workflows/`, `layouts/`, `voices/`):

* a sidecar that does not parse **takes its track out of the shelf and says so**. Passing over the
  broken sidecar in silence would leave a track that looks like it simply has no metadata — and a
  track chosen for the wrong reason is worse than one chosen for none;
* a name that was never registered **raises and lists the ones that exist** — never a fallback,
  because a fallback here means a film scored with music nobody asked for;
* two tracks claiming one name is an error, not a last-one-wins;
* a `mood` outside the vocabulary is an error rather than a silent string match.

⚠️ The vocabulary is deliberately closed. Free-form mood strings make `pick()` a string comparison
that always "finds" something and is therefore never wrong and never useful.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import i18n

__all__ = ["FOLDER_NAME", "AUDIO_SUFFIXES", "MOODS", "use_folder", "reload", "available", "names",
           "row", "errors", "file_of", "resolve", "pick", "describe", "folder_of", "publish"]

FOLDER_NAME = "music"
MANIFEST_SUFFIX = ".json"
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")

# A closed vocabulary, so that "calm" means one thing. `tags` is the free-form half on purpose:
# "piano", "no drums", "under a CT scan" are things no vocabulary would have predicted.
MOODS = ("calm", "warm", "tense", "sad", "uplifting", "epic", "neutral")

_USER: dict[str, dict] = {}
_USER_ERRORS: list[dict] = []
_FOLDER: "Path | None" = None
_SIGN: tuple = ()


def use_folder(folder: "str | Path | None") -> dict:
    """Point the shelf at a directory. Called once from `create_app`, like the other three."""
    global _FOLDER
    _FOLDER = Path(folder).expanduser() if folder else None
    return reload(force=True)


def _note(name: str, why: str, path: str = "") -> None:
    _USER_ERRORS.append({"name": name, "why": why, "path": path})


def _sign() -> tuple:
    """A cheap fingerprint of the folder: names and mtimes, so an edit is noticed."""
    if not _FOLDER or not _FOLDER.is_dir():
        return ()
    out = []
    for p in sorted(_FOLDER.iterdir()):
        try:
            out.append((p.name, p.stat().st_mtime_ns, p.stat().st_size))
        except OSError:
            pass
    return tuple(out)


def folder_of() -> "Path | None":
    return _FOLDER


def _clean_mood(value: object, where: str) -> str:
    mood = str(value or "").strip().lower()
    if not mood:
        return "neutral"
    if mood not in MOODS:
        raise ValueError(i18n.pick_now(
            f"mood \"{mood}\" is not one of: {', '.join(MOODS)}",
            f"mood「{mood}」不在词表里:{'、'.join(MOODS)}"))
    return mood


def _parse_file(path: Path) -> dict:
    """One track from its audio file (and its sidecar, when there is one)."""
    name = path.stem
    meta: dict = {}
    side = path.with_suffix(MANIFEST_SUFFIX)
    if side.is_file():
        try:
            body = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(i18n.pick_now(
                f"{side.name} is not valid JSON: {e}",
                f"{side.name} 不是合法 JSON:{e}")) from e
        if not isinstance(body, dict):
            raise ValueError(i18n.pick_now(f"{side.name} must hold an object",
                                           f"{side.name} 里应该是一个对象"))
        meta = body
    seconds = meta.get("seconds") or 0.0
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise ValueError(i18n.pick_now(f"seconds in {side.name} is not a number",
                                       f"{side.name} 里的 seconds 不是数字")) from None
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        raise ValueError(i18n.pick_now(f"tags in {side.name} must be a list",
                                       f"{side.name} 里的 tags 应该是数组"))
    return {
        "name": name,
        "title": str(meta.get("title") or name),
        "mood": _clean_mood(meta.get("mood"), name),
        "tags": [str(t) for t in tags if str(t).strip()],
        "bpm": meta.get("bpm") or 0,
        "seconds": round(seconds, 2),
        "source": str(meta.get("source") or ""),
        "licence": str(meta.get("licence") or ""),
        "file": str(path),
        "bytes": path.stat().st_size,
        "path": str(path),
    }


def reload(*, force: bool = False) -> dict:
    """Re-read the folder when it changed. Returns a small summary, like the other shelves."""
    global _USER, _USER_ERRORS, _SIGN
    sign = _sign()
    if not force and sign == _SIGN:
        return {"tracks": len(_USER), "errors": len(_USER_ERRORS), "changed": False}
    _SIGN = sign
    _USER, _USER_ERRORS = {}, []
    if not _FOLDER:
        return {"tracks": 0, "errors": 0, "changed": True}
    if not _FOLDER.is_dir():
        _note(_FOLDER.name, i18n.pick_now("the music folder does not exist",
                                          "音乐目录不存在"), str(_FOLDER))
        return {"tracks": 0, "errors": 1, "changed": True}
    for path in sorted(_FOLDER.iterdir()):
        if path.suffix.lower() not in AUDIO_SUFFIXES or not path.is_file():
            continue
        try:
            track = _parse_file(path)
        except ValueError as e:
            # Named, never skipped: a broken sidecar otherwise reads as "no metadata".
            _note(path.name, str(e), str(path))
            continue
        if track["name"] in _USER:
            _note(track["name"], i18n.pick_now("two tracks claim this name",
                                               "有两份音乐用了同一个名字"), str(path))
            continue
        _USER[track["name"]] = track
    return {"tracks": len(_USER), "errors": len(_USER_ERRORS), "changed": True}


def available() -> dict[str, dict]:
    reload()
    return dict(_USER)


def names() -> list[str]:
    return sorted(available())


def row(name: str) -> "dict | None":
    return available().get(str(name))


def errors() -> list[dict]:
    return list(_USER_ERRORS)


def file_of(name: str) -> "Path | None":
    track = row(name)
    return Path(track["file"]) if track else None


def remove(name: str) -> bool:
    """Take one track off the shelf — the audio **and** its sidecar.

    Returns False when there is no such track instead of raising: "it is already gone" is a fine
    answer to "delete it", and the caller only needs to know whether anything was removed.

    ⚠️ Only ever touches files the shelf itself listed (`row()` reads the folder, so a name that
    climbs out of it is not a key and comes back False here) — the same rule `resolve` follows.
    """
    track = row(str(name))
    if not track:
        return False
    path = Path(track["file"])
    path.unlink(missing_ok=True)
    path.with_suffix(MANIFEST_SUFFIX).unlink(missing_ok=True)
    reload(force=True)
    return True


def resolve(value: str) -> tuple[str, dict]:
    """`(name, track)` for a value that is either a shelf name or a path to an audio file.

    The two are told apart by the filesystem, not by a prefix: if there is a file there, it is a
    file; only then is the shelf consulted. Anything else **raises with the shelf listed**, because
    the alternative — scoring the film with whatever happened to be first — is the failure this
    whole module exists to prevent.
    """
    raw = str(value or "").strip()
    if not raw:
        raise KeyError(i18n.pick_now("no music was named", "没有指定音乐"))
    candidate = Path(raw).expanduser()
    if candidate.is_file() and candidate.suffix.lower() in AUDIO_SUFFIXES:
        track = {
            "name": candidate.stem, "title": candidate.stem, "mood": "neutral", "tags": [],
            "bpm": 0, "seconds": 0.0, "source": str(candidate), "licence": "", "file": str(candidate),
            "bytes": candidate.stat().st_size, "path": str(candidate),
        }
        return track["name"], track
    track = row(raw)
    if track:
        return raw, track
    have = names()
    raise KeyError(i18n.pick_now(
        f"there is no music called \"{raw}\". On the shelf: "
        + (", ".join(have) if have else "(the shelf is empty)"),
        f"音乐库里没有「{raw}」。现有的是:"
        + ("、".join(have) if have else "(库是空的)")))


def describe(track: dict) -> str:
    """One line about a track, for a note or a receipt: what it is and where it came from."""
    bits = [track.get("title") or track.get("name") or "?"]
    bits.append(track.get("mood") or "neutral")
    if track.get("bpm"):
        bits.append(f"{track['bpm']} bpm")
    if track.get("seconds"):
        bits.append(f"{track['seconds']:g}s")
    if track.get("licence"):
        bits.append(str(track["licence"]))
    if track.get("source"):
        bits.append(str(track["source"]))
    return " · ".join(str(b) for b in bits)


def pick(*, mood: str = "", tags: "list[str] | tuple[str, ...]" = (), seconds: float = 0.0,
         spoken: bool = False) -> tuple[str, dict, str]:
    """Choose a track for this film, and say why.

    Returns `(name, track, why)`. The *why* is not decoration: a score the user cannot argue with is
    a score they cannot correct either, so the reason travels back with the choice.

    Ranking, in order: a track whose mood was asked for beats one merely tagged `neutral`; a track
    long enough to not loop beats one that has to; a track that already suits a film with narration
    (no lyrics to fight the voice) beats one that does not. `seconds` of 0 means "length unknown".
    """
    shelf = available()
    if not shelf:
        raise KeyError(i18n.pick_now(
            "the music shelf is empty, so there is nothing to choose from",
            "音乐库是空的,没有可挑的曲子"))
    want_mood = _clean_mood(mood, "pick") if mood else ""
    want_tags = {str(t).strip().lower() for t in tags if str(t).strip()}
    scored: list[tuple[float, str, dict, list[str]]] = []
    for name, track in shelf.items():
        score, why = 0.0, []
        if want_mood:
            if track["mood"] == want_mood:
                score += 5.0
                why.append(i18n.pick_now(f"mood is {want_mood}", f"情绪是 {want_mood}"))
            elif track["mood"] == "neutral":
                score += 1.0
                why.append(i18n.pick_now("mood is open (neutral)", "情绪是中性、不挑场合"))
        have = {t.lower() for t in track["tags"]}
        matched = sorted(want_tags & have)
        if matched:
            score += 2.0 * len(matched)
            why.append(i18n.pick_now("tagged " + ", ".join(matched), "标签命中 " + "、".join(matched)))
        if seconds and track["seconds"]:
            if track["seconds"] >= seconds:
                score += 2.0
                why.append(i18n.pick_now("long enough on its own", "长度够,不用循环"))
            else:
                why.append(i18n.pick_now(
                    f"shorter than the film ({track['seconds']:g}s), so it will loop",
                    f"比片子短({track['seconds']:g}s),要循环"))
        if spoken:
            # A track with vocals fights a narrator; instrumental is the safe default under a voice.
            if "vocal" in have or "lyrics" in have or "sung" in have:
                score -= 3.0
                why.append(i18n.pick_now("has vocals, and someone is speaking",
                                         "带人声,而片中有旁白"))
            else:
                score += 1.0
                why.append(i18n.pick_now("no vocals to fight the narration", "没有会跟旁白打架的人声"))
        scored.append((score, name, track, why))
    scored.sort(key=lambda s: (-s[0], s[1]))
    _, name, track, why = scored[0]
    reason = "；".join(why) or i18n.pick_now("the only one on the shelf", "库里只有这一首")
    return name, track, reason


def _slug(text: str, limit: int = 48) -> str:
    """A file-safe name for whatever the caller called this track."""
    keep: list[str] = []
    for ch in str(text or "").strip():
        if ch.isalnum() or ch in "-_":
            keep.append(ch)
        elif ch in " \t/\\:：,，。.·|":
            keep.append("-")
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out or "track")[:limit]


def publish(name: str, data: bytes, *, suffix: str = ".mp3", mood: str = "neutral",
            tags: "list[str] | tuple[str, ...]" = (), seconds: float = 0.0, source: str = "",
            licence: str = "", title: str = "") -> Path:
    """Put a freshly made track on the shelf, sidecar and all. Returns the audio path.

    Why a generator that does not publish is half a feature: the shelf is what the *choosing* reads,
    so a piece that only lands in a workspace has to be found and moved by hand before any film can
    be scored with it — and the film being scored right now is the one that just made it.

    The sidecar is written **first** and the audio moved in after, deliberately: a crash between the
    two leaves "a sidecar with no track" (which the shelf ignores, and which says what was attempted)
    rather than "a track with no length, no mood and no source" — and that second one would be picked
    for the wrong film, because a track with nothing recorded about it is a track that matches
    nothing and therefore loses nothing.

    A name already on the shelf is **not** overwritten: publishing twice makes `name` and `name-2`.
    Overwriting would mean a film already scored with the old take suddenly references the new one.
    """
    folder = _FOLDER
    if folder is None:
        raise KeyError(i18n.pick_now(
            "There is no music folder to publish into, so the track has nowhere to go.",
            "没有可上架的音乐目录,这首曲子无处安放。"))
    if not folder.is_dir():
        folder.mkdir(parents=True, exist_ok=True)
    stem = _slug(name)
    n = 1
    while (folder / f"{stem}{suffix}").exists() or (folder / f"{stem}{MANIFEST_SUFFIX}").exists():
        n += 1
        stem = f"{_slug(name)}-{n}"
    meta = {
        "title": title or name, "mood": _clean_mood(mood, stem),
        "tags": [str(t) for t in tags if str(t).strip()],
        "seconds": round(float(seconds or 0.0), 2),
        "source": source, "licence": licence,
    }
    (folder / f"{stem}{MANIFEST_SUFFIX}").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    dest = folder / f"{stem}{suffix}"
    dest.write_bytes(data)
    reload(force=True)
    return dest
