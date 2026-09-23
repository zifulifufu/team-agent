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
