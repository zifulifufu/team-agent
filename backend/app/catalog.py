"""Model catalog: ships with a snapshot (app/data/catalog.json) and allows a newer
version to override it.

The override lives at catalog.json in the data directory (pulled from GitHub by the
"Update" page, or placed there manually); whichever of the two has the larger
version wins. The catalog is only a "list to choose from" — the models that can
actually be called are always determined by the provider's /models endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from . import i18n, strengths
from .versions import is_newer

SHIPPED = Path(__file__).parent / "data" / "catalog.json"


def _load(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("providers"), dict):
            return d
    except (OSError, ValueError):
        pass
    return None


def validate(data: object) -> str | None:
    """Returns an error description, or None when valid. Used to validate a catalog
downloaded from the network."""
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        return i18n.pick_now("version is missing", "缺少 version")
    provs = data.get("providers")
    if not isinstance(provs, dict) or not provs:
        return i18n.pick_now("providers is missing", "缺少 providers")
    for pid, p in provs.items():
        if not isinstance(p, dict) or not isinstance(p.get("models"), list):
            return i18n.pick_now(f"{pid}: models has the wrong shape", f"{pid}: models 格式不对")
        for m in p["models"]:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
                return i18n.pick_now(f"{pid}: a model entry is missing its id", f"{pid}: 模型条目缺少 id")
    return None


class Catalog:
    def __init__(self, data_dir: Path):
        self.override_path = Path(data_dir) / "catalog.json"
        self.data: dict = {"version": "", "providers": {}}
        self.source = "shipped"
        self.reload()

    def reload(self) -> None:
        base = _load(SHIPPED) or {"version": "", "providers": {}}
        over = _load(self.override_path)
        if over and is_newer(over.get("version", ""), base.get("version", "")):
            self.data, self.source = over, "override"
        else:
            self.data, self.source = base, "shipped"
        self._by_id: dict[str, tuple[str, dict]] = {}
        for pid, p in self.data["providers"].items():
            for m in p.get("models", []):
                self._by_id.setdefault(m["id"].lower(), (pid, m))

    @property
    def version(self) -> str:
        return str(self.data.get("version", ""))

    def save_override(self, data: dict) -> None:
        self.override_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        self.reload()

    # ------------------------------------------------------------------ lookup
    def key_for(self, provider: dict) -> str | None:
        """Map a user's provider onto a provider in the catalog: first by id, then by the
domain of the API address."""
        provs = self.data["providers"]
        if provider["id"] in provs:
            return provider["id"]
        host = urlparse(provider.get("base_url") or "").netloc
        if host:
            for pid, p in provs.items():
                if p.get("base_url") and urlparse(p["base_url"]).netloc == host:
                    return pid
        return None

    def models_of(self, key: str | None) -> list[dict]:
        """Catalog rows for a provider, with the text in the language of the request."""
        return i18n.localize(list(self.data["providers"].get(key or "", {}).get("models", [])))

    def retired_of(self, key: str | None) -> dict[str, str]:
        rows = i18n.localize(self.data["providers"].get(key or "", {}).get("retired", []))
        return {r["id"]: r.get("reason") or i18n.pick_now("Retired", "已停用") for r in rows}

    def find(self, key: str | None, model_name: str) -> dict | None:
        low = model_name.lower()
        for m in self.models_of(key):
            if m["id"].lower() == low or low in [a.lower() for a in ([m.get("alias")] if m.get("alias") else []) + m.get("aliases", [])]:
                return m
        hit = self._by_id.get(low)
        return i18n.localize(hit[1]) if hit else None

    def defaults(self, key: str) -> list[str]:
        """Models added by default when a provider is created: one flagship plus one fast
tier (legacy and preview entries are skipped)."""
        ms = [m for m in self.models_of(key) if not m.get("legacy") and not m.get("preview")]
        out: list[str] = []
        main = next((m for m in ms if m.get("tier") in ("flagship", "balanced")), None)
        fast = next((m for m in ms if m.get("tier") == "fast"), None)
        for m in (main, fast):
            if m and m["id"] not in out:
                out.append(m["id"])
        return out

    # --------------------------------------------------------------- strengths
    def strengths_for(self, provider: dict, model_name: str) -> list[str]:
        key = self.key_for(provider)
        return strengths.infer(model_name, self.find(key, model_name), is_local=bool(provider.get("is_local")))

    def describe(self, provider: dict, model_name: str) -> dict:
        """Catalog information for display in the UI (summary, context, status)."""
        key = self.key_for(provider)
        e = self.find(key, model_name)
        retired = self.retired_of(key).get(model_name)
        return {
            "summary": (e or {}).get("summary", ""),
            "context": (e or {}).get("context"),
            "tier": (e or {}).get("tier"),
            "legacy": bool((e or {}).get("legacy")) or bool(retired),
            "preview": bool((e or {}).get("preview")),
            "retired_reason": retired,
            "in_catalog": e is not None,
        }
