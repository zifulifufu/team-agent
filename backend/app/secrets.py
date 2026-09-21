"""把 API Key 之类的敏感值放进系统钥匙串,而不是明文写进 SQLite。

为什么需要:客户的安全审查一定会查「密钥是否明文落盘」。macOS 上直接用系统自带的
`security` 命令行读写登录钥匙串(service=team-agent,account=<引用名>),数据库里只留一个
引用标记;这样备份、导出、随手拷 .db 文件都不会把密钥带走。

做不到的时候(非 macOS、钥匙串被锁、命令被拒)会**回退**成原来的明文存法,并把存储方式
标记出来 —— 绝不因为读不到钥匙串就把 Key 丢掉。

已知取舍:`security add-generic-password` 只能把密码作为**命令行参数**传入,同一用户的其他
进程在极短的时间窗口内可能从进程参数里看到它。相比「明文长期落盘、随备份扩散」,这仍是显著的改善;
如果这点也要消除,需要引入 keyring 之类的依赖或改用带签名的辅助程序。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Any

SERVICE = "team-agent"
REF_PREFIX = "keychain:"          # 数据库里存这个前缀,表示真正的值在钥匙串里
DISABLE_ENV = "TEAM_AGENT_NO_KEYCHAIN"      # 设成 1 就强制不用钥匙串(测试用,免得污染真实钥匙串)
_TIMEOUT = 10

_cache: dict[str, str] = {}


def backend_available() -> bool:
    """有没有可用的钥匙串后端(目前只支持 macOS 的 security)。"""
    if os.environ.get(DISABLE_ENV):
        return False
    return platform.system() == "Darwin" and bool(shutil.which("security"))


def ref_name(scope: str, ident: str) -> str:
    """钥匙串里的条目名,例如 provider:deepseek / github-token。"""
    return f"{scope}:{ident}" if ident else scope


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REF_PREFIX)


def make_ref(ref: str) -> str:
    return REF_PREFIX + ref


def parse_ref(value: str) -> str:
    return value[len(REF_PREFIX):] if is_ref(value) else value


def _run(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(["security", *args], capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, (p.stdout or "").strip()


def put(ref: str, value: str) -> bool:
    """写进钥匙串。成功返回 True;任何失败都返回 False(由调用方决定怎么回退)。"""
    if not backend_available() or not value:
        return False
    # -U: 已存在就更新,避免重复添加失败
    code, _ = _run(["add-generic-password", "-U", "-a", ref, "-s", SERVICE, "-w", value])
    if code != 0:
        return False
    _cache[ref] = value
    return True


def get(ref: str) -> str | None:
    """从钥匙串读。读不到(没写过 / 被删 / 换机器)返回 None。"""
    if not backend_available():
        return None
    if ref in _cache:
        return _cache[ref]
    code, out = _run(["find-generic-password", "-a", ref, "-s", SERVICE, "-w"])
    if code != 0:
        return None
    _cache[ref] = out
    return out


def delete(ref: str) -> bool:
    if not backend_available():
        return False
    _cache.pop(ref, None)
    code, _ = _run(["delete-generic-password", "-a", ref, "-s", SERVICE])
    return code == 0


def forget_cache(ref: str | None = None) -> None:
    """测试用:清掉内存缓存,强制下次真的去读钥匙串。"""
    if ref is None:
        _cache.clear()
    else:
        _cache.pop(ref, None)
