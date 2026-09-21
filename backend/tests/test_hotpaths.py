"""热路径回归测试:WAL 写入、流式分片合并、本机地址不走代理。

这三个都是「跑起来才看得见」的改动,用测试把行为和边界固定住,避免以后被无意改回去。
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


# --------------------------------------------------------------- 本机地址判定
def test_is_local_url_covers_loopback_and_lan() -> None:
    local = [
        "http://127.0.0.1:11434/api/tags",   # 本机 Ollama
        "http://localhost:8765",             # 本机后端
        "127.0.0.1:11434",                   # 没写 scheme
        "::1",                               # 裸 IPv6
        "[::1]:11434",                       # 带方括号的 IPv6
        "http://192.168.1.10:8898/v1",       # 内网工作站
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
    # 本机 / 局域网:直连,绝不把回环请求发给 Clash 之类的代理
    assert client("http://127.0.0.1:11434")._trust_env is False
    assert client("http://192.168.1.10:8898/v1")._trust_env is False
    # 真外网:仍然尊重系统代理(公司网络里往往只有走代理才出得去)
    assert client("https://api.deepseek.com")._trust_env is True
    # 探测本机服务的客户端显式强制直连
    assert client(timeout=2.5, trust_env=False)._trust_env is False


# ------------------------------------------------------------------ WAL 写入
def test_store_uses_wal_with_normal_sync(store: Store) -> None:
    """默认 delete journal + synchronous=FULL 会让每次提交都 fsync;WAL+NORMAL 实测快 8 倍。"""
    assert str(store._db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1   # 1 = NORMAL
    assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_wal_files_are_cleaned_up_by_restore(store: Store, tmp_path) -> None:
    """恢复备份后不允许留下 .restore-*.db 的临时文件(含 -wal/-shm)。"""
    backup = tmp_path / "b.db"
    store.backup_to(backup, include_keys=True)
    store.restore_from(backup)
    leftovers = [p.name for p in (store.data_dir / "backups").iterdir() if p.name.startswith(".restore-")]
    assert leftovers == []


# -------------------------------------------------------------- 流式分片合并
async def test_deltas_are_coalesced_but_text_stays_complete(store: Store) -> None:
    store.update_settings({"memory_auto_extract": False, "perm_mode": "allow_all"})
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    reply = "这是一段用来测量流式粒度的回复内容。" * 20        # 340 字
    fake = FakeLLM(default=reply)                              # 每 3 个字符一片
    orch = Orchestrator(store, ModelRouter(store, fake))
    group = store.list_groups()[0]

    coll = Collector()
    await orch.handle_user_message(group["id"], "写点什么", coll)
    await orch.drain()

    deltas = [e for e in coll.events if e["type"] == "delta"]
    merged = "".join(e["text"] for e in deltas)
    chunks = (len(reply) + 2) // 3

    assert reply in merged                       # 合并不能丢字、不能改顺序
    assert len(deltas) < chunks / 3              # 事件数应比模型分片数少一个量级
    assert all(len(e["text"]) <= max(DELTA_BATCH_CHARS * 2, 64) for e in deltas)   # 没有把整篇一次塞进来
