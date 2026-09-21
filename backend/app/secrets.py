"""Store sensitive values such as API keys in the system keychain instead of writing
them in plaintext into SQLite.

Why this exists: a customer security review always checks "are secrets stored in
plaintext on disk?". On macOS we use the built-in `security` command to read and
write the login keychain (service=team-agent, account=<ref name>) and leave only a
reference marker in the database; that way backups, exports, and casually copied
.db files never carry the secrets along.

When it is not possible (non-macOS, locked keychain, command denied) we **fall
back** to the previous plaintext storage and record which storage mode was used —
a Key is never dropped just because the keychain cannot be read.

Known trade-off: `security add-generic-password` can only pass the password as a
**command-line argument**, so other processes of the same user may see it in the
process arguments within a very short window. Compared with "plaintext sitting on
disk indefinitely and spreading through backups" this is still a clear improvement;
eliminating that too would require a dependency like keyring or a signed helper program.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Any

SERVICE = "team-agent"
REF_PREFIX = "keychain:"          # stored in the DB as this prefix, meaning the real value lives in the keychain
DISABLE_ENV = "TEAM_AGENT_NO_KEYCHAIN"      # set to 1 to force-disable the keychain (for tests, to avoid polluting the real one)
_TIMEOUT = 10

_cache: dict[str, str] = {}


def backend_available() -> bool:
    """Whether a usable keychain backend exists (currently only macOS `security`)."""
    if os.environ.get(DISABLE_ENV):
        return False
    return platform.system() == "Darwin" and bool(shutil.which("security"))


def ref_name(scope: str, ident: str) -> str:
    """Entry name inside the keychain, e.g. provider:deepseek / github-token."""
    return f"{scope}:{ident}" if ident else scope


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REF_PREFIX)


def make_ref(ref: str) -> str:
    return REF_PREFIX + ref


def parse_ref(value: str) -> str:
    return value[len(REF_PREFIX):] if is_ref(value) else value


def _run(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(["security", *args], capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, (p.stdout or "").strip()


def put(ref: str, value: str) -> bool:
    """Write to the keychain. Returns True on success; any failure returns False
    (the caller decides how to fall back)."""
    if not backend_available() or not value:
        return False
    # -U: update if it already exists, so a duplicate add does not fail
    code, _ = _run(["add-generic-password", "-U", "-a", ref, "-s", SERVICE, "-w", value])
    if code != 0:
        return False
    _cache[ref] = value
    return True


def get(ref: str) -> str | None:
    """Read from the keychain. Returns None when unavailable (never written / deleted /
    different machine)."""
    if not backend_available():
        return None
    if ref in _cache:
        return _cache[ref]
    code, out = _run(["find-generic-password", "-a", ref, "-s", SERVICE, "-w"])
    if code != 0:
        return None
    _cache[ref] = out
    return out


def delete(ref: str) -> bool:
    if not backend_available():
        return False
    _cache.pop(ref, None)
    code, _ = _run(["delete-generic-password", "-a", ref, "-s", SERVICE])
    return code == 0


def forget_cache(ref: str | None = None) -> None:
    """For tests: clear the in-memory cache so the next read really hits the keychain."""
    if ref is None:
        _cache.clear()
    else:
        _cache.pop(ref, None)
