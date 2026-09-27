"""The measured address+model list, pinned.

The list exists because of one specific real failure: WorkBuddy shows this user's DeepSeek entry as
"DeepSeek-V4 Flash", and the API answers *"The supported API model names are deepseek-flash,
deepseek-v4-pro, but you passed DeepSeek-V4 Flash"*. A model name is an API id, not what a desktop
app calls it. So the table has to stay honest in two ways that fail quietly:

* **`measured` must be a subset of `models`.** A pair claimed as "answered a real call from here" but
  absent from what the service serves is exactly the lie the list exists to prevent — and it reads
  perfectly well, which is why it needs a test rather than a careful reading.
* **Every address must pass `_endpoint`.** It goes into the same field the user types into by hand,
  so a value that field rejects is not a preset, it is a trap that fails on save.

No network: this checks the table, not the services.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import external
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def test_every_preset_has_an_address_the_settings_field_would_accept():
    assert external.MODEL_PRESETS, "an empty list would silently remove the whole point"
    for p in external.MODEL_PRESETS:
        assert external._endpoint(p["base_url"]) == p["base_url"]


def test_measured_is_always_a_subset_of_what_the_service_serves():
    for p in external.MODEL_PRESETS:
        assert set(p["measured"]) <= set(p["models"]), p["id"]
        assert p["measured"], f"{p['id']} claims nothing was ever called"


def test_every_preset_offers_a_model_and_says_where_the_key_comes_from():
    for p in external.MODEL_PRESETS:
        assert p["models"] and all(m.strip() for m in p["models"]), p["id"]
        assert p["where"].strip() and p["where_zh"].strip(), p["id"]


def test_no_model_name_is_written_as_a_display_name():
    """Model ids are lower-case and hyphenated as a rule; the failure this guards against was a
    capitalized, spaced display name, so a space in a model id is a smell worth failing on."""
    for p in external.MODEL_PRESETS:
        for m in p["models"]:
            assert " " not in m, f"{p['id']}: {m!r} looks like a display name, not an API id"


def test_the_overview_carries_them_so_the_dialog_can_offer_them(client):
    d = client.get("/api/external").json()
    assert d["model_presets"] and d["presets_verified"]
    first = d["model_presets"][0]
    for key in ("id", "name", "base_url", "models", "measured", "where"):
        assert key in first, key


def test_the_localized_copy_keeps_the_fields_a_dialog_reads():
    got = external.model_presets()
    assert len(got) == len(external.MODEL_PRESETS)
    for g in got:
        assert g["id"] and g["name"] and g["base_url"] and g["models"]
