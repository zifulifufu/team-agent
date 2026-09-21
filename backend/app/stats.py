"""Usage statistics: computed entirely from local message records, nothing is uploaded.

Note: token usage and cost are not tracked yet (some providers omit `usage` in
streaming responses) — only call counts, fallback counts, and latency.
"""

from __future__ import annotations

from . import i18n

import time
from collections import defaultdict
from datetime import datetime, timedelta

from .store import Store


def compute_stats(store: Store, days: int = 14, now: float | None = None) -> dict:
    days = max(1, min(int(days), 90))
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now).date()
    first_day = today - timedelta(days=days - 1)
    since = datetime.combine(first_day, datetime.min.time()).timestamp()

    local_ids = {m["id"] for m in store.list_models() if m.get("is_local")}
    labels = {m["id"]: m["display_name"] for m in store.list_models()}

    per_day = {(first_day + timedelta(days=i)).isoformat(): {"date": (first_day + timedelta(days=i)).isoformat(),
                                                               "count": 0, "fallbacks": 0}
               for i in range(days)}
    per_model: dict[str, dict] = defaultdict(lambda: {"count": 0, "lat_sum": 0.0, "lat_n": 0})
    total = fallbacks = local_calls = 0
    lat_sum = 0.0
    lat_n = 0

    for r in store.agent_message_rows(since):
        total += 1
        day = datetime.fromtimestamp(r["created_at"]).date().isoformat()
        if day in per_day:
            per_day[day]["count"] += 1
        if r["fallback_from"]:
            fallbacks += 1
            if day in per_day:
                per_day[day]["fallbacks"] += 1
        mid = r["model_id"]
        if not mid:
            continue
        pm = per_model[mid]
        pm["count"] += 1
        if mid in local_ids:
            local_calls += 1
        ok = [a for a in r["meta"].get("attempts", []) if a.get("status") == "ok"]
        if ok:
            lat = float(ok[-1].get("latency_ms") or 0)
            pm["lat_sum"] += lat
            pm["lat_n"] += 1
            lat_sum += lat
            lat_n += 1

    by_model = sorted(
        (
            {
                "model_id": mid,
                "label": labels.get(mid) or (i18n.pick_now(f"{mid[4:].capitalize()} (external)", f"{mid[4:].capitalize()}(外部)") if mid.startswith("ext:") else mid),
                "is_local": mid in local_ids,
                "count": v["count"],
                "avg_latency_ms": round(v["lat_sum"] / v["lat_n"]) if v["lat_n"] else None,
            }
            for mid, v in per_model.items()
        ),
        key=lambda x: (-x["count"], x["model_id"]),
    )
    return {
        "days": days,
        "total_requests": total,
        "fallbacks": fallbacks,
        "local_calls": local_calls,
        "avg_latency_ms": round(lat_sum / lat_n) if lat_n else None,
        "groups": len(store.list_groups()),
        "by_model": by_model,
        "by_day": list(per_day.values()),
    }
