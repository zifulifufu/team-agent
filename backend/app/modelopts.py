"""「选择模型」对话框的数据:把随程序的目录、服务商实时返回的清单、你已经添加的模型合并成一张表。

每个条目带有:强项标签、是否已添加、是否「新」(自上次查看后新出现)、是否已被服务商下线/停用。
判断「新」的办法:每个服务商记一份「已看过的模型 ID」;第一次打开时把当前所有条目记为已看过(不会全标成新),
以后目录更新或实时清单里多出来的 ID 就是「新」,点「全部标为已读」后清掉。
"""

from __future__ import annotations

from .discovery import fetch_model_ids
from .store import Store


def _entry_view(store: Store, prov: dict, model_name: str, entry: dict | None, have: dict) -> dict:
    mine = have.get(model_name)
    return {
        "id": model_name,
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
    if seen is None:  # 第一次打开:现有的都算看过
        store.set_model_seen(pid, ordered)
        seen = list(ordered)
    seen_set = set(seen)

    items = []
    for mid in ordered:
        v = _entry_view(store, prov, mid, cat.find(key, mid), have)
        v["live"] = (mid in live_ids) if live_ids is not None else None
        v["is_new"] = mid not in seen_set
        v["retired_reason"] = retired.get(mid)
        # 服务商实时清单里已经没有、但你添加过的模型:多半已下线。本地服务(Ollama)的清单是「已安装」,不适用。
        v["gone"] = bool(live_ids is not None and not prov["is_local"] and mid in have and mid not in live_ids)
        if prov["is_local"]:
            v["installed"] = (mid in live_ids) if live_ids is not None else None
        items.append(v)

    # 排序:新的在前 → 已添加 → 主力/旧版靠后
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


def mark_seen(store: Store, pid: str) -> None:
    data = model_options(store, pid)
    store.set_model_seen(pid, [m["id"] for m in data["models"]])


async def refresh_live(store: Store, pid: str) -> list[str]:
    """向服务商查询实时清单并缓存,返回 ID 列表。失败会抛 DiscoveryError。"""
    prov = store.get_provider(pid)
    if store.get_model_seen(pid) is None:
        model_options(store, pid)  # 先把「已看过」的基线记下来,这次实时清单里多出来的才算新
    ids = await fetch_model_ids(prov, timeout=store.get_settings().get("request_timeout", 60))  # type: ignore[arg-type]
    store.set_model_live(pid, ids)
    return ids
