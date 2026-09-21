"""Bilingual built-in content: the shipped catalogs and the API that serves them.

The rule these tests protect: data files keep **English in the base field and
Chinese in the `<field>_zh` sibling**, and the API returns only one language at a
time, chosen from `?lang=` or `Accept-Language`, with English as the default.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

from app import strengths
from app.main import create_app
from app.store import Store

HAN = re.compile(r"[\u4e00-\u9fff]")
DATA = Path(__file__).resolve().parent.parent / "app" / "data"


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _chinese_offender(node, path="") -> str | None:
    """First Chinese string found outside a `_zh` field, or None."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("_zh"):
                continue
            hit = _chinese_offender(v, f"{path}.{k}")
            if hit:
                return hit
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hit = _chinese_offender(v, f"{path}[{i}]")
            if hit:
                return hit
    elif isinstance(node, str) and HAN.search(node):
        return f"{path}: {node[:60]}"
    return None


def _zh_keys(node, out: list[str] | None = None) -> list[str]:
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("_zh"):
                out.append(k)
            _zh_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _zh_keys(v, out)
    return out


# --------------------------------------------------------------------- data files
def test_catalog_file_is_english_first_with_chinese_alongside() -> None:
    data = _load("catalog.json")
    assert _chinese_offender(data) is None, "catalog.json 的基础字段必须只有英文"
    assert data["note_zh"], "中文版本要保留在 note_zh 里"
    rows = [m for p in data["providers"].values() for m in p["models"]]
    assert len(rows) >= 100
    assert all(m.get("summary") for m in rows), "每个条目都要有英文简介"
    # Chinese may only live in a _zh field; entries whose source text is already
    # English have no _zh counterpart
    assert not any(HAN.search(m["summary"]) for m in rows)
    bilingual = [m for m in rows if m.get("summary_zh")]
    assert len(bilingual) >= len(rows) - 2, "绝大多数条目应有中文版本"
    for m in rows:
        if m.get("summary_zh"):
            assert HAN.search(m["summary_zh"]), m["id"]
    reasons = [r for p in data["providers"].values() for r in p.get("retired", [])]
    assert reasons and all(r.get("reason") and r.get("reason_zh") for r in reasons)


def test_local_models_file_is_english_first_with_chinese_alongside() -> None:
    data = _load("local_models.json")
    assert _chinese_offender(data) is None, "local_models.json 的基础字段必须只有英文"
    for fam in data["families"]:
        assert fam["desc"] and fam["desc_zh"], fam["id"]
        assert set(fam["strengths"]) <= set(strengths.TAG_IDS), fam["id"]     # ASCII ids, not Chinese names
        for m in fam["models"]:
            if m.get("note"):
                assert not HAN.search(m["note"]) and m["note_zh"], m["tag"]


# ------------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    async def fake(**_: object) -> str:
        return "ok"

    app = create_app(tmp_path / "data", completion_fn=fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def test_local_catalog_defaults_to_english_and_serves_chinese_on_request(client: TestClient) -> None:
    default = client.get("/api/local/catalog").json()
    assert not HAN.search(default["note"])
    assert not _zh_keys(default), "接口不应把 _zh 中间字段透出去"
    assert default["families"] and not any(HAN.search(f["desc"]) for f in default["families"])

    by_query = client.get("/api/local/catalog?lang=zh").json()
    assert HAN.search(by_query["note"])
    assert HAN.search(by_query["families"][0]["desc"])

    by_header = client.get("/api/local/catalog", headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}).json()
    assert HAN.search(by_header["note"])                                       # q-weighted headers are understood too
    assert client.get("/api/local/catalog", headers={"Accept-Language": "fr-FR"}).json()["note"] == default["note"]
    # the same data, structured identically in both languages
    assert [f["id"] for f in by_query["families"]] == [f["id"] for f in default["families"]]
    assert [m["tag"] for m in by_query["families"][0]["models"]] == [m["tag"] for m in default["families"][0]["models"]]


def test_model_lists_and_catalog_rows_follow_the_language(client: TestClient) -> None:
    en = client.get("/api/models").json()
    zh = client.get("/api/models?lang=zh").json()
    assert en and len(en) == len(zh)
    assert any(m.get("summary") for m in en)
    assert not any(HAN.search(m.get("summary") or "") for m in en)
    assert any(HAN.search(m.get("summary") or "") for m in zh)
    assert not _zh_keys(en)

    # catalog rows in the model picker follow the language as well
    opts_en = client.get("/api/providers/deepseek/model-options").json()
    opts_zh = client.get("/api/providers/deepseek/model-options?lang=zh").json()
    first_en = next(v for v in opts_en["models"] if v["summary"])
    first_zh = next(v for v in opts_zh["models"] if v["id"] == first_en["id"])
    assert not HAN.search(first_en["summary"]) and HAN.search(first_zh["summary"])


def test_retired_reason_is_localized(client: TestClient) -> None:
    """Retirement reasons follow the language too. This asks the catalog directly,
    because a model that was never added does not show up in the options.
"""
    from app import i18n

    cat = client.app.state.store.catalog
    en = cat.retired_of("deepseek")["deepseek-chat"]
    assert "Retired" in en
    i18n.set_current("zh")
    try:
        assert "停用" in cat.retired_of("deepseek")["deepseek-chat"]
    finally:
        i18n.set_current("en")


def test_strength_tag_ids_stay_ascii_and_labels_are_localized(client: TestClient) -> None:
    en = client.get("/api/strengths").json()["tags"]
    zh = client.get("/api/strengths").json()  # same ids, labels follow the language
    assert [t["id"] for t in en] == [t["id"] for t in zh["tags"]]
    assert all(re.fullmatch(r"[a-z-]+", t["id"]) for t in en)
    assert {t["label"] for t in en} >= {"Coding", "Writing", "Local"}
    zh_tags = client.get("/api/strengths", headers={"Accept-Language": "zh-CN"}).json()["tags"]
    assert {t["label"] for t in zh_tags} >= {"代码", "写作", "本地"}


# ------------------------------------------------------------------- migration
def test_legacy_chinese_tags_in_existing_database_are_normalized(tmp_path: Path) -> None:
    """Old databases store Chinese tag ids; upgrading swaps them for ASCII ids
    automatically, keeping the values and losing no user data.
"""
    data = tmp_path / "data"
    store = Store(data)
    aid = store.list_agents()[0]["id"]
    mid = store.list_models()[0]["id"]
    # imitate the Chinese tags an older version wrote
    store._x("UPDATE agents SET tags=? WHERE id=?", (json.dumps(["写作", "代码"], ensure_ascii=False), aid))
    store._x("UPDATE models SET strengths=? WHERE id=?", (json.dumps(["中文"], ensure_ascii=False), mid))
    store._x("DELETE FROM meta WHERE key='tags_to_ascii_ids'")      # back to the pre-upgrade state

    store2 = Store(data)
    tags = next(a for a in store2.list_agents() if a["id"] == aid)["tags"]
    assert tags == ["writing", "coding"]
    assert next(m for m in store2.list_models() if m["id"] == mid)["strengths"] == ["chinese"]
    # unknown tags are dropped by the existing rule instead of corrupting the row
    assert strengths.clean_tags(["写作", "谁啊"]) == ["writing"]


def test_unknown_or_missing_language_falls_back_to_english() -> None:
    from app import i18n

    assert i18n.resolve(None, None) == "en"
    assert i18n.resolve("zh-TW", None) == "zh"
    assert i18n.resolve("en-GB", "zh") == "en"          # an explicit lang beats the request header
    assert i18n.resolve("klingon", "zh-CN") == "zh"
    assert i18n.resolve(None, "de-DE,fr;q=0.9") == "en"


def test_localize_keeps_lists_and_never_leaks_zh_keys() -> None:
    from app import i18n

    node = {"a": "A", "a_zh": "甲", "rows": [{"b": "B", "b_zh": "乙"}, {"c": "C"}]}
    assert i18n.localize(node, "zh") == {"a": "甲", "rows": [{"b": "乙"}, {"c": "C"}]}
    assert i18n.localize(node, "en") == {"a": "A", "rows": [{"b": "B"}, {"c": "C"}]}
    assert i18n.localize("plain", "zh") == "plain"


def test_language_middleware_does_not_change_stored_data(tmp_path: Path) -> None:
    """The request language only shapes how things are displayed; it must never
    change the values in the database.
"""
    data = tmp_path / "data"
    store = Store(data)
    before = store.get_settings()["route_chain"]
    app = create_app(data, completion_fn=lambda **_: SimpleNamespace())  # type: ignore[arg-type]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.get("/api/models?lang=zh")
        c.get("/api/local/catalog?lang=zh")
    assert Store(data).get_settings()["route_chain"] == before
