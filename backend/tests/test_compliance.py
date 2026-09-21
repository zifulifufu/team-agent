"""Behaviour regressions tied to commercial compliance: no outbound calls by
default, secrets never stored in plaintext, licences and third-party notices in
place.

If any of these gets reverted, commercial delivery breaks (customer security
reviews / licence obligations), so the tests pin them down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import secrets as secrets_store
from app.presets import DEFAULT_SETTINGS
from app.store import Store


def _raw_key(st: Store, pid: str = "deepseek") -> str:
    """Read the database directly to see what is really persisted (no parsing involved)."""
    return st._one("SELECT api_key FROM providers WHERE id=?", (pid,))["api_key"]


# -------------------------------------------------- no outbound calls by default
def test_auto_check_updates_is_off_by_default(store: Store) -> None:
    assert DEFAULT_SETTINGS["auto_check_updates"] is False
    assert store.get_settings()["auto_check_updates"] is False


def test_old_db_with_auto_check_on_is_turned_off_once(tmp_path: Path) -> None:
    """Old installs carried the value "on" - upgrading turns it off once; anything the
    user turns on afterwards must not be reverted.
"""
    d = tmp_path / "data"
    st = Store(d)
    # recreate the old state: migration not yet run and auto-check still on
    st._x("DELETE FROM meta WHERE key='auto_check_off_by_default'")
    st.update_settings({"auto_check_updates": True})
    st._db.close()

    st2 = Store(d)
    assert st2.get_settings()["auto_check_updates"] is False, "升级时应把自动外联关掉"
    st2.update_settings({"auto_check_updates": True})          # the user turns it on later, deliberately
    st2._db.close()

    st3 = Store(d)
    assert st3.get_settings()["auto_check_updates"] is True, "迁移只能做一次,不能反复覆盖用户的选择"
    st3._db.close()


# ------------------------------------------- no bundled third-party content any more
def test_no_bundled_third_party_content_ships_with_the_app() -> None:
    """The app ships no upstream project content, avoiding the Apache-2.0
    obligations that redistribution would bring.
"""
    data = Path(__file__).resolve().parent.parent / "app" / "data"
    assert not (data / "awesome_apps.json").exists()
    assert not (data / "awesome_zh.json").exists()
    # the remaining bundled data must be our own catalogs / lists
    assert (data / "catalog.json").exists() and (data / "local_models.json").exists()


# --------------------------------------------- secrets never stored in plaintext
class _FakeKeychain:
    """Fake keychain backend: exercises the logic without pushing test data into the
    real keychain.
"""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.fail_write = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_FakeKeychain":
        def put(ref: str, value: str) -> bool:
            if self.fail_write or not value:
                return False
            self.items[ref] = value
            return True

        monkeypatch.setattr(secrets_store, "backend_available", lambda: True)
        monkeypatch.setattr(secrets_store, "put", put)
        monkeypatch.setattr(secrets_store, "get", lambda ref: self.items.get(ref))
        monkeypatch.setattr(secrets_store, "delete", lambda ref: self.items.pop(ref, None) is not None or True)
        secrets_store.forget_cache()
        return self


def test_api_key_is_stored_as_a_keychain_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kc = _FakeKeychain().install(monkeypatch)
    st = Store(tmp_path / "data")
    st.update_provider("deepseek", {"api_key": "sk-live-abcdef123456"})

    assert _raw_key(st) == "keychain:provider:deepseek"          # only a reference lives in the database
    assert "sk-live" not in json.dumps(st._q("SELECT * FROM providers"), ensure_ascii=False)
    assert kc.items["provider:deepseek"] == "sk-live-abcdef123456"
    assert st.get_provider("deepseek")["api_key"] == "sk-live-abcdef123456"   # reads still yield the real value
    assert st.list_providers()[0]["api_key"] == "sk-live-abcdef123456"       # callers notice nothing

    st.update_provider("deepseek", {"api_key": ""})
    assert "provider:deepseek" not in kc.items                  # clearing also removes the keychain entry
    assert _raw_key(st) == ""


def test_secret_backend_reports_where_keys_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = Store(tmp_path / "data")
    assert st.secret_backend() == "plaintext"          # conftest disables the keychain
    _FakeKeychain().install(monkeypatch)
    assert st.secret_backend() == "keychain"


def test_keychain_reference_survives_backup_without_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backup made without keys must contain no trace of them, not even a reference."""
    _FakeKeychain().install(monkeypatch)
    st = Store(tmp_path / "data")
    st.update_provider("deepseek", {"api_key": "sk-live-abcdef123456"})
    out = tmp_path / "b.db"
    st.backup_to(out, include_keys=False)
    import sqlite3
    assert sqlite3.connect(out).execute("SELECT api_key FROM providers WHERE id='deepseek'").fetchone()[0] == ""
    # a backup that includes keys carries the real values (handy when moving machines)
    out2 = tmp_path / "b2.db"
    st.backup_to(out2, include_keys=True)
    assert sqlite3.connect(out2).execute("SELECT api_key FROM providers WHERE id='deepseek'").fetchone()[0] == "sk-live-abcdef123456"


def test_migration_moves_plaintext_keys_into_keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys sitting in plaintext in an old database move into the keychain on upgrade."""
    st = Store(tmp_path / "data")
    st.update_provider("deepseek", {"api_key": "sk-old-abcdef123456"})
    assert _raw_key(st) == "sk-old-abcdef123456"                # no keychain yet (conftest disabled it), so plaintext
    st._x("DELETE FROM meta WHERE key='keys_to_keychain'")      # back to the old state: migration not yet run
    st._db.close()

    kc = _FakeKeychain().install(monkeypatch)
    st2 = Store(tmp_path / "data")
    assert _raw_key(st2) == "keychain:provider:deepseek"
    assert kc.items["provider:deepseek"] == "sk-old-abcdef123456"
    assert st2.get_provider("deepseek")["api_key"] == "sk-old-abcdef123456"


def test_migration_keeps_plaintext_when_keychain_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the move fails (keychain locked or denied), keep the plaintext as is - a
    migration must never lose a key.
"""
    st = Store(tmp_path / "data")
    st.update_provider("deepseek", {"api_key": "sk-old-abcdef123456"})
    st._x("DELETE FROM meta WHERE key='keys_to_keychain'")
    st._db.close()

    kc = _FakeKeychain()
    kc.fail_write = True
    kc.install(monkeypatch)
    st2 = Store(tmp_path / "data")
    assert _raw_key(st2) == "sk-old-abcdef123456"
    assert st2.get_provider("deepseek")["api_key"] == "sk-old-abcdef123456"


def test_github_token_is_stored_as_a_keychain_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kc = _FakeKeychain().install(monkeypatch)
    st = Store(tmp_path / "data")
    st.update_settings({"github_token": "ghp_testtoken0123456789"})
    assert st._one("SELECT value FROM settings WHERE key='github_token'")["value"] == '"keychain:github-token:default"'
    assert kc.items["github-token:default"] == "ghp_testtoken0123456789"
    assert st.get_settings()["github_token"] == "ghp_testtoken0123456789"


@pytest.mark.skipif(not os.environ.get("TEAM_AGENT_KEYCHAIN_TEST"),
                    reason="需要真实钥匙串(会往登录钥匙串写一条测试项再删掉),默认跳过;设 TEAM_AGENT_KEYCHAIN_TEST=1 可运行")
def test_real_keychain_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round trip against the real keychain (writes one test item to your login
    keychain and deletes it straight afterwards).
"""
    monkeypatch.delenv(secrets_store.DISABLE_ENV, raising=False)
    assert secrets_store.backend_available()
    ref = secrets_store.ref_name("selftest", "roundtrip")
    try:
        assert secrets_store.put(ref, "sk-selftest-123456")
        secrets_store.forget_cache(ref)
        assert secrets_store.get(ref) == "sk-selftest-123456"
    finally:
        secrets_store.delete(ref)
        secrets_store.forget_cache(ref)
        assert secrets_store.get(ref) is None
