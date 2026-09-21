"""商用合规相关的行为回归:默认不外联、密钥不落明文、许可与第三方声明就位。

这些点一旦被改回去,商用交付就会出问题(客户安全审查 / 许可义务),所以用测试钉住。
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
    """直接读库,看真正落盘的是什么(不走任何解析)。"""
    return st._one("SELECT api_key FROM providers WHERE id=?", (pid,))["api_key"]


# --------------------------------------------------------------- 默认不外联
def test_auto_check_updates_is_off_by_default(store: Store) -> None:
    assert DEFAULT_SETTINGS["auto_check_updates"] is False
    assert store.get_settings()["auto_check_updates"] is False


def test_old_db_with_auto_check_on_is_turned_off_once(tmp_path: Path) -> None:
    """旧版本的存量值是「开」——升级时关一次;之后用户自己开的不会被改回去。"""
    d = tmp_path / "data"
    st = Store(d)
    # 造出「迁移还没跑过、且自动检查是开着的」旧库状态
    st._x("DELETE FROM meta WHERE key='auto_check_off_by_default'")
    st.update_settings({"auto_check_updates": True})
    st._db.close()

    st2 = Store(d)
    assert st2.get_settings()["auto_check_updates"] is False, "升级时应把自动外联关掉"
    st2.update_settings({"auto_check_updates": True})          # 用户之后主动打开
    st2._db.close()

    st3 = Store(d)
    assert st3.get_settings()["auto_check_updates"] is True, "迁移只能做一次,不能反复覆盖用户的选择"
    st3._db.close()


# --------------------------------------------------------------- 不再内置第三方内容
def test_no_bundled_third_party_content_ships_with_the_app() -> None:
    """程序不附带上游项目的内容,避免再分发带来的 Apache-2.0 义务。"""
    data = Path(__file__).resolve().parent.parent / "app" / "data"
    assert not (data / "awesome_apps.json").exists()
    assert not (data / "awesome_zh.json").exists()
    # 自带的其余数据都必须是自己整理的目录/清单
    assert (data / "catalog.json").exists() and (data / "local_models.json").exists()


# --------------------------------------------------------------- 密钥不落明文
class _FakeKeychain:
    """假的钥匙串后端:用来验证逻辑,又不往真实钥匙串里塞测试数据。"""

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

    assert _raw_key(st) == "keychain:provider:deepseek"          # 库里只有引用
    assert "sk-live" not in json.dumps(st._q("SELECT * FROM providers"), ensure_ascii=False)
    assert kc.items["provider:deepseek"] == "sk-live-abcdef123456"
    assert st.get_provider("deepseek")["api_key"] == "sk-live-abcdef123456"   # 读出来仍是真值
    assert st.list_providers()[0]["api_key"] == "sk-live-abcdef123456"       # 调用方无感

    st.update_provider("deepseek", {"api_key": ""})
    assert "provider:deepseek" not in kc.items                  # 清空会顺手删掉钥匙串条目
    assert _raw_key(st) == ""


def test_secret_backend_reports_where_keys_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = Store(tmp_path / "data")
    assert st.secret_backend() == "plaintext"          # conftest 关掉了钥匙串
    _FakeKeychain().install(monkeypatch)
    assert st.secret_backend() == "keychain"


def test_keychain_reference_survives_backup_without_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不含密钥的备份里不能出现任何形式的密钥(引用也不行)。"""
    _FakeKeychain().install(monkeypatch)
    st = Store(tmp_path / "data")
    st.update_provider("deepseek", {"api_key": "sk-live-abcdef123456"})
    out = tmp_path / "b.db"
    st.backup_to(out, include_keys=False)
    import sqlite3
    assert sqlite3.connect(out).execute("SELECT api_key FROM providers WHERE id='deepseek'").fetchone()[0] == ""
    # 含密钥的备份则要能带走真值(便于换机器)
    out2 = tmp_path / "b2.db"
    st.backup_to(out2, include_keys=True)
    assert sqlite3.connect(out2).execute("SELECT api_key FROM providers WHERE id='deepseek'").fetchone()[0] == "sk-live-abcdef123456"


def test_migration_moves_plaintext_keys_into_keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧库里明文存着的 Key,升级时搬进钥匙串。"""
    st = Store(tmp_path / "data")
    st.update_provider("deepseek", {"api_key": "sk-old-abcdef123456"})
    assert _raw_key(st) == "sk-old-abcdef123456"                # 此时没有钥匙串(conftest 关掉了),是明文
    st._x("DELETE FROM meta WHERE key='keys_to_keychain'")      # 回到「迁移还没跑过」的旧库状态
    st._db.close()

    kc = _FakeKeychain().install(monkeypatch)
    st2 = Store(tmp_path / "data")
    assert _raw_key(st2) == "keychain:provider:deepseek"
    assert kc.items["provider:deepseek"] == "sk-old-abcdef123456"
    assert st2.get_provider("deepseek")["api_key"] == "sk-old-abcdef123456"


def test_migration_keeps_plaintext_when_keychain_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """搬不动(钥匙串被锁/被拒)就原样留着明文 —— 绝不能因为迁移把 Key 弄丢。"""
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


@pytest.mark.skipif(not os.environ.get("TEAM_AGENT_KEYCHAIN_TEST"), reason="需要真实钥匙串,默认跳过")
def test_real_keychain_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实钥匙串的往返验证(会往你的登录钥匙串里写一条测试项,跑完即删)。"""
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
