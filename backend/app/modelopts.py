"""Data for the "choose a model" dialog: merges the bundled catalog, the provider's
live listing, and the models you already added into a single table.

Each entry carries: strength tags, whether it is already added, whether it is "new"
(appeared since the last visit), and whether the provider has retired/disabled it.
How "new" is decided: one set of "already seen model IDs" is kept per provider; the
first time the dialog opens, every current entry is recorded as seen (so nothing is
flagged new), and from then on IDs that appear via a catalog update or the live
listing count as "new" until "mark all as read" clears them.
"""

from __future__ import annotations

from .discovery import fetch_models
from .media import purpose_of
from .store import Store


def _entry_view(store: Store, prov: dict, model_name: str, entry: dict | None, have: dict,
                mode: str | None = None) -> dict:
    mine = have.get(model_name)
    return {
        "id": model_name,
        # What this is for. The provider's own word wins (`mode` in its listing); the name is the
        # fallback. The dialog shows it, because a gateway lists chat and image models together and
        # picking an image model for a member fails only after the choice has been made.
        "use": purpose_of(model_name, mode),
        "name": (entry or {}).get("name") or (mine or {}).get("display_name") or model_name,
        "summary": (entry or {}).get("summary", ""),
        "context": (entry or {}).get("context"),
        "tier": (entry or {}).get("tier"),
        "size_gb": (entry or {}).get("size_gb"),
        "params": (entry or {}).get("params"),
        "strengths": mine["strengths"] if mine else store.catalog.strengths_for(prov, model_name),
        "in_catalog": entry is not None,
        "legacy": bool((entry or {}).get("legacy")),
        "preview": bool((entry or {}).get("preview")),
        "added": mine is not None,
        "enabled": mine["enabled"] if mine else None,
    }


def model_options(store: Store, pid: str) -> dict:
    prov = store.get_provider(pid)
    if not prov:
        raise KeyError(pid)
    cat = store.catalog
    key = cat.key_for(prov)
    retired = cat.retired_of(key)
    live = store.get_model_live(pid)
    live_ids = set(live["ids"]) if live else None
    live_modes = live["modes"] if live else {}
    have = {m["model_name"]: m for m in store.list_models() if m["provider_id"] == pid}

    ordered: list[str] = []
    for e in cat.models_of(key):
        ordered.append(e["id"])
    for i in (live["ids"] if live else []):
        if i not in ordered:
            ordered.append(i)
    for i in have:
        if i not in ordered:
            ordered.append(i)

    seen = store.get_model_seen(pid)
    if seen is None:  # first open: everything currently listed counts as seen
        store.set_model_seen(pid, ordered)
        seen = list(ordered)
    seen_set = set(seen)

    items = []
    for mid in ordered:
        v = _entry_view(store, prov, mid, cat.find(key, mid), have, live_modes.get(mid))
        v["live"] = (mid in live_ids) if live_ids is not None else None
        v["is_new"] = mid not in seen_set
        v["retired_reason"] = retired.get(mid)
        # Added by you but absent from the provider's live listing: most likely retired.
        # Not applicable to local providers (Ollama), whose listing means "installed".
        v["gone"] = bool(live_ids is not None and not prov["is_local"] and mid in have and mid not in live_ids)
        if prov["is_local"]:
            v["installed"] = (mid in live_ids) if live_ids is not None else None
        items.append(v)

    # Order: new first -> already added -> flagship/legacy last
    tier_rank = {"flagship": 0, "balanced": 1, "fast": 2, None: 3}
    order = {mid: i for i, mid in enumerate(ordered)}
    items.sort(key=lambda v: (not v["is_new"], v["legacy"] or bool(v["retired_reason"]), tier_rank.get(v["tier"], 3), order[v["id"]]))
    return {
        "provider_id": pid,
        "catalog_version": cat.version,
        "catalog_source": cat.source,
        "catalog_key": key,
        "live_fetched_at": live["fetched_at"] if live else None,
        "new_count": sum(1 for v in items if v["is_new"]),
        "models": items,
    }


def models_for_use(store: Store, pid: str, use: str) -> list[dict]:
    """The models of one provider that are meant for one thing: the choices an image or video
    setting offers as its model name.

    Taken from the live listing when there is one — that is where the provider's own answer lives —
    and from the rows already added otherwise.
    """
    prov = store.get_provider(pid)
    if not prov:
        raise KeyError(pid)
    live = store.get_model_live(pid) or {"ids": [], "modes": {}}
    rows = {m["model_name"]: m for m in store.list_provider_models(pid)}
    out: list[dict] = []
    for mid in live["ids"]:
        if purpose_of(mid, live["modes"].get(mid)) != use:
            continue
        row = rows.get(mid)
        out.append({"id": mid, "added": row is not None, "enabled": bool(row["enabled"]) if row else None})
    if not out:      # nothing was fetched yet: fall back to what has been added
        out = [{"id": n, "added": True, "enabled": bool(m["enabled"])}
               for n, m in rows.items() if m["use"] == use]
    return sorted(out, key=lambda e: e["id"])


def mark_seen(store: Store, pid: str) -> None:
    data = model_options(store, pid)
    store.set_model_seen(pid, [m["id"] for m in data["models"]])


async def refresh_live(store: Store, pid: str) -> list[str]:
    """Query the provider for its live listing, cache it — ids *and* what each model is for — and
    return the id list. Raises DiscoveryError on failure."""
    prov = store.get_provider(pid)
    if store.get_model_seen(pid) is None:
        model_options(store, pid)  # record the "seen" baseline first, so only IDs new
        # to this live listing count as new
    entries = await fetch_models(prov, timeout=store.get_settings().get("request_timeout", 60))  # type: ignore[arg-type]
    ids = [e["id"] for e in entries]
    modes = {e["id"]: e["mode"] for e in entries if e.get("mode")}
    store.set_model_live(pid, ids, modes)
    # A refresh is also the moment the rows already added can be corrected: the provider just told
    # us which of them are not chat models, and a name-based guess is all we had before.
    store.sync_model_uses(pid, modes)
    return ids
