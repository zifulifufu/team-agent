"""Hot-path regression tests: WAL writes, streamed delta coalescing, and
localhost calls bypassing the proxy.

All three are changes you only notice once the app runs, so the tests pin the
behaviour and its edges to stop anyone reverting them by accident.
"""

from __future__ import annotations

from app.net import client, is_local_url
from app.orchestrator import DELTA_BATCH_CHARS, Orchestrator
from app.router import ModelRouter
from app.store import Store
from tests.conftest import FakeLLM


class Collector:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, ev: dict) -> None:
        self.events.append(ev)


# ------------------------------------------------------------ local address detection
def test_is_local_url_covers_loopback_and_lan() -> None:
    local = [
        "http://127.0.0.1:11434/api/tags",   # local Ollama
        "http://localhost:8765",             # local backend
        "127.0.0.1:11434",                   # no scheme
        "::1",                               # bare IPv6
        "[::1]:11434",                       # bracketed IPv6
        "http://192.168.1.10:8898/v1",       # machine on the LAN
        "http://10.0.0.5/v1",
        "http://172.16.5.9/v1",
        "http://169.254.1.1/x",
        "http://mac.local:11434",
    ]
    for url in local:
        assert is_local_url(url), url

    remote = ["https://api.deepseek.com", "https://github.com/x/y", "http://example.com", "http://8.8.8.8"]
    for url in remote:
        assert not is_local_url(url), url


def test_client_bypasses_proxy_only_for_local_targets() -> None:
    # localhost / LAN: connect directly, never hand loopback traffic to a
    # proxy such as Clash
    assert client("http://127.0.0.1:11434")._trust_env is False
    assert client("http://192.168.1.10:8898/v1")._trust_env is False
    # real internet: still honour the system proxy (on a corporate network it is
    # often the only way out)
    assert client("https://api.deepseek.com")._trust_env is True
    # clients probing local services force a direct connection explicitly
    assert client(timeout=2.5, trust_env=False)._trust_env is False


# --------------------------------------------------------------- WAL writes
def test_store_uses_wal_with_normal_sync(store: Store) -> None:
    """The default delete journal with synchronous=FULL fsyncs on every commit;
    WAL + NORMAL measured about 8x faster.
"""
    assert str(store._db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1   # 1 = NORMAL
    assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_wal_files_are_cleaned_up_by_restore(store: Store, tmp_path) -> None:
    """Restoring a backup must not leave .restore-*.db temp files behind
    (-wal/-shm included)."""
    backup = tmp_path / "b.db"
    store.backup_to(backup, include_keys=True)
    store.restore_from(backup)
    leftovers = [p.name for p in (store.data_dir / "backups").iterdir() if p.name.startswith(".restore-")]
    assert leftovers == []


# ------------------------------------------------------- streamed delta coalescing
async def test_deltas_are_coalesced_but_text_stays_complete(store: Store) -> None:
    store.update_settings({"memory_auto_extract": False, "perm_mode": "allow_all"})
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    reply = "这是一段用来测量流式粒度的回复内容。" * 20        # 340 chars
    fake = FakeLLM(default=reply)                              # one chunk every 3 chars
    orch = Orchestrator(store, ModelRouter(store, fake))
    group = store.list_groups()[0]

    coll = Collector()
    await orch.handle_user_message(group["id"], "写点什么", coll)
    await orch.drain()

    deltas = [e for e in coll.events if e["type"] == "delta"]
    merged = "".join(e["text"] for e in deltas)
    chunks = (len(reply) + 2) // 3

    assert reply in merged                       # coalescing must drop no text and reorder nothing
    assert len(deltas) < chunks / 3              # an order of magnitude fewer events than model chunks
    assert all(len(e["text"]) <= max(DELTA_BATCH_CHARS * 2, 64) for e in deltas)   # nothing dumped in one go
