"""Do not let a reply that quotes a credential become part of the transcript.

Installed from the template gallery as a draft, switched off.

`post_reply` runs just before a reply is stored. Blocking means the reply is not kept at all, and
the reason is shown in the group instead — so the group learns that it happened rather than
silently losing an answer. Extend PATTERNS with whatever your own material must not carry.
"""

import re

PATTERNS = [
    (r"sk-[A-Za-z0-9_-]{16,}", "an API key"),
    (r"(?i)\b(?:password|passwd|secret|token)\s*[:=]\s*\S+", "a password or token"),
    (r"\b\d{17}[\dXx]\b", "what looks like an ID number"),
]


def handle(event, payload):
    text = payload.get("text") or ""
    for pattern, what in PATTERNS:
        if re.search(pattern, text):
            return {"block": True,
                    "reason": "the reply quotes " + what + " — put it somewhere safe, not in the chat"}
    return {"block": False}
