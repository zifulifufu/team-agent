"""Write one line per finished round and per tool call.

Installed from the template gallery as a draft, switched off.

Observers may return nothing at all: what they answer is ignored, they are told what happened and
nothing else. The file is written next to this hook — a hook runs with its own folder as the
working directory — one JSON object per line, so `jq` and `grep` both work on it.

`audit.jsonl` is in your hooks directory. Move it wherever the log belongs by writing an absolute
path into PATH below.
"""

import json
import os
import time

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.jsonl")


def handle(event, payload):
    row = {"at": round(time.time(), 3), "event": event, "group": payload.get("group_id", "")}
    if event == "round.end":
        row |= {"entries": payload.get("entries"), "seconds": payload.get("seconds"),
                "agents": payload.get("agents")}
    elif event == "tool.called":
        row |= {"tool": payload.get("tool"), "ok": payload.get("ok"), "ms": payload.get("ms")}
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return None
