"""Check a message before it leaves this machine for a chat channel.

Installed from the template gallery as a draft, switched off.

This is the last gate: what it lets through is gone. It is deliberately different from
`post_reply`, which guards the record (what stays here) — this one guards the door (what goes out),
and unlike that one it fails closed: if this hook cannot run, the message is held back.

PATTERNS is by nature a rough net. Use it as a starting point, not as a guarantee — a message can
carry a phone number in words, and no regex will catch that.
"""

import re

PATTERNS = [
    (r"\b1[3-9]\d{9}\b", "a phone number"),
    (r"\b\d{17}[\dXx]\b", "an ID number"),
]


def handle(event, payload):
    text = payload.get("text") or ""
    found = [what for pattern, what in PATTERNS if re.search(pattern, text)]
    if found:
        return {"block": True, "reason": "it contains " + " and ".join(found)}
    return {"block": False}
