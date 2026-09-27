"""Where a program installed outside this app is looked for, and how it is found by name.

One list, in a module with no other imports, because more than one place has to agree on it —
`attachments` (the media binaries), the environment a member's code is run in — and a second copy
would drift into "the app finds it in one path and not in another", which is a bug report nobody
can reproduce. It is a leaf module on purpose: `attachments` pulls in the library and the store,
which pull in `media`, which imports `coderun`, so a member's code run cannot reach the list
through `attachments` at all.

The trap this exists for is the same everywhere: an app started from the Finder inherits launchd's
minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), which carries neither a Homebrew prefix nor
`~/.local/bin`. So "the user installed it" and "this app can find it" become two different facts,
with nothing on screen to explain the gap — video frames that are never taken, a transcriber that
is never found, a render that answers `command not found`.

`~/.local/bin` is in the list for the tools a user installs by hand: `pipx install …`,
`pip install --user …` and `uv tool install …` all put their entry points there.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), "/usr/bin", "/bin")


def tool(name: str) -> str | None:
    """A binary by name, from PATH or a usual install prefix."""
    found = shutil.which(name)
    if found:
        return found
    for folder in TOOL_DIRS:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# The variables that decide where a model cache is. Model weights are **input**, not something a run
# produces: they are downloaded once and reused, so a child has to be able to see the ones the user
# already has. That matters because one caller deliberately redirects `HOME` (`localcmd._env` does it
# so npm and Chrome caches do not land in the real home directory) — and with `HOME` redirected,
# huggingface_hub resolves its cache to `<redirected home>/.cache/huggingface`, finds nothing, and
# reaches for the network. Measured (2026-09-25): `huggingface.co` is unreachable from this machine,
# so a 2.3 GB model sitting in `~/.cache/huggingface/hub` was reported as "not downloaded" after a
# **398-second** timeout, and the tool looked broken when the only problem was where it was looking.
_MODEL_CACHE_ENV = ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_ENDPOINT",
                    "HF_HUB_OFFLINE", "TRANSFORMERS_CACHE", "TORCH_HOME")


def model_cache_env(real_home: "str | os.PathLike[str] | None" = None) -> dict[str, str]:
    """The variables a child needs to find the model caches this machine already has.

    The user's own settings win — an `HF_ENDPOINT` mirror, an `HF_HOME` they moved somewhere else —
    and only a missing `HF_HOME` is filled in, because that is the variable that actually decides
    where `huggingface_hub` looks. Merge it into a child's environment **before** redirecting `HOME`,
    never after, or this has no effect: these are absolute paths, and they are exactly the ones the
    redirected `HOME` would otherwise invent.
    """
    home = Path(real_home or os.environ.get("HOME") or Path.home()).expanduser()
    out = {k: os.environ[k] for k in _MODEL_CACHE_ENV if os.environ.get(k)}
    out.setdefault("HF_HOME", str(home / ".cache" / "huggingface"))
    return out


def search_path(current: str | None = None) -> str:
    """`current` (or this process's PATH) with the install prefixes it is missing.

    Appended, never prepended: whatever the caller put on its own PATH wins, which is what makes
    this safe to add to a child's environment — a version the user placed deliberately earlier on
    their shell's PATH is still the one that runs.
    """
    raw = os.environ.get("PATH", "") if current is None else current
    path = [p for p in raw.split(os.pathsep) if p]
    for folder in TOOL_DIRS:
        if folder not in path and Path(folder).is_dir():
            path.append(folder)
    return os.pathsep.join(path)
