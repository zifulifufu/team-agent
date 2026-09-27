"""What the user thought of a reply, and what the app does with it.

A rating that only lives in the message row is decoration. This module is the small amount of
vocabulary and arithmetic that turns "the user clicked a thumb" into something a person (and the
process engineer) can act on: a closed set of verdicts, a per-member scoreboard with the notes
attached, and the rule for which messages may be rated at all.

What is deliberately *not* here: anything that changes a member's behaviour by itself. A thumbs-down
does not silently rewrite a prompt or switch a model — it is evidence, shown to the user and to the
process engineer, who then decides. A scoreboard that edits prompts on its own would be a system
nobody can reason about, and the first time it got one wrong there would be no way to see why.

The counterpart in the interface is the feedback panel, and the ratings travel with the message:
`note` is optional, `rating` is `up` / `down` / `""` (cleared, which is how a mis-click is undone).
"""

from __future__ import annotations

from typing import Any

from . import i18n

# The closed vocabulary. `""` is not "no opinion" but "cleared".
RATINGS: tuple[str, ...] = ("up", "down")
MAX_NOTE = 500


class FeedbackError(ValueError):
    """A rating that cannot be stored — always the caller's input, never the store's state."""


def clean(rating: Any) -> str:
    """The verdict as it will be stored: one of `RATINGS`, or `""` to clear.

    Refused rather than coerced. Storing "maybe" as "down" would put a verdict in the user's mouth
    they did not give, and this number is meant to be quoted back at people.
    """
    got = str(rating or "").strip().lower()
    if got in ("", "clear", "none"):
        return ""
    if got not in RATINGS:
        raise FeedbackError(i18n.pick_now(
            f"A rating is one of {', '.join(RATINGS)} or empty to clear it, not \"{got}\".",
            f"评价只能是 {'、'.join(RATINGS)} 或留空(撤销),不是「{got}」。"))
    return got


def clean_note(note: Any) -> str:
    """The note beside a thumb: trimmed, and cut rather than refused when it is very long.

    Cut, not refused: the note is a remark, and losing a whole verdict because someone pasted a
    paragraph would be worse than keeping the first 500 characters of it.
    """
    return " ".join(str(note or "").split())[:MAX_NOTE]


def may_rate(row: dict) -> bool:
    """Whether this message can be judged: a member's reply, and nothing else.

    The user's own message has no member to answer for it, and a system note (a pause, a warning) was
    written by this app rather than by a model — a verdict on either would put rows on a scoreboard
    that no member can do anything about, which is the fastest way to make a scoreboard worthless.
    """
    return str(row.get("sender_type") or "") == "agent"


def summary(rows: list[dict]) -> dict:
    """`{members: [...], totals: {...}}` — the scoreboard, biggest first.

    Members with only down-votes and members with only up-votes are both here, on purpose: a list
    that hides the bad news is the one kind of scoreboard nobody should trust. Sorting is by number
    of ratings, then by name, so the order does not move around while the user is reading it.
    """
    buckets: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("agent_id") or row.get("agent_name") or "")
        got = buckets.setdefault(key, {"agent_id": key, "name": row.get("agent_name") or "?",
                                       "up": 0, "down": 0, "notes": []})
        if row.get("rating") == "up":
            got["up"] += 1
        elif row.get("rating") == "down":
            got["down"] += 1
        if str(row.get("note") or "").strip():
            got["notes"].append({"rating": row.get("rating") or "", "note": row["note"],
                                 "message_id": row.get("message_id") or "",
                                 "at": row.get("created_at") or 0.0,
                                 "group": row.get("group_name") or "",
                                 "text": (row.get("text") or "")[:400]})
    members = sorted(buckets.values(), key=lambda m: (-(m["up"] + m["down"]), m["name"]))
    return {
        "members": members,
        "totals": {"up": sum(m["up"] for m in members), "down": sum(m["down"] for m in members),
                   "rated": len(rows)},
    }
