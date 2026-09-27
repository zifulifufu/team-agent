"""Start an explicitly configured local ComfyUI installation when it is needed."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import httpx

from . import coderun, i18n


class LocalRuntime:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._lock = asyncio.Lock()
        self._children: dict[str, subprocess.Popen] = {}

    async def ensure(self, provider: dict, cfg: dict, *, timeout: float = 120) -> tuple[bool, str]:
        if provider.get("kind") != "comfyui" or not cfg.get("comfyui_auto_start"):
            return True, ""
        base = str(provider.get("base_url") or "").rstrip("/")
        try:
            url = urlsplit(base)
            port = url.port or 80
        except ValueError:
            return False, i18n.pick_now("Invalid ComfyUI address.", "ComfyUI 地址无效。")
        if (not provider.get("is_local") or url.scheme != "http"
                or url.hostname not in ("127.0.0.1", "localhost", "::1")
                or url.path not in ("", "/") or url.username or url.password or url.query or url.fragment):
            return False, i18n.pick_now(
                "Automatic startup requires a local HTTP loopback address at the API root.",
                "自动启动仅支持标为本地的 HTTP 回环地址,地址应指向接口根路径。")
        async with self._lock:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                async def reachable() -> bool:
                    try:
                        response = await client.get(base + "/system_stats")
                    except httpx.ConnectError:
                        return False
                    # A responding service is never replaced or interrupted. The regular probe
                    # will explain authentication, version or workflow errors.
                    return response.status_code > 0

                try:
                    if await reachable():
                        return True, ""
                    root_text = str(cfg.get("comfyui_dir") or "").strip()
                    python_text = str(cfg.get("comfyui_python") or "").strip()
                    root, python = Path(root_text).expanduser(), Path(python_text).expanduser()
                    if (not root_text or not python_text or not root.is_absolute()
                            or not python.is_absolute() or not (root / "main.py").is_file()
                            or not python.is_file() or not os.access(python, os.X_OK)):
                        return False, i18n.pick_now(
                            "Set the existing ComfyUI folder and its Python executable under Model providers. No runtime or weights were downloaded.",
                            "请在模型服务商里填写现有 ComfyUI 目录及其 Python 可执行文件。程序没有下载运行环境或模型。")
                    child = self._children.get(base)
                    log = self.data_dir / "logs" / f"comfyui-{port}.log"
                    if child is None or child.poll() is not None:
                        log.parent.mkdir(parents=True, exist_ok=True)
                        with log.open("ab") as output:
                            child = subprocess.Popen(
                                [str(python), "-u", str(root / "main.py"), "--listen", url.hostname,
                                 "--port", str(port), "--disable-auto-launch"],
                                cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=True, env={**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"})
                        self._children[base] = child
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if await reachable():
                            return True, ""
                        if child.poll() is not None:
                            return False, i18n.pick_now(
                                f"ComfyUI exited during startup (code {child.returncode}). See {log}.",
                                f"ComfyUI 启动后退出(退出码 {child.returncode})。请查看日志:{log}。")
                        await asyncio.sleep(0.5)
                    return False, i18n.pick_now(
                        f"ComfyUI is still starting. Check again shortly; no second instance was started. Log: {log}",
                        f"ComfyUI 仍在启动,请稍后再检查;没有重复启动实例。日志:{log}")
                except (httpx.HTTPError, OSError) as error:
                    return False, i18n.pick_now(
                        f"ComfyUI startup failed: {type(error).__name__}: {error}",
                        f"ComfyUI 启动失败:{type(error).__name__}: {error}")

    def stop(self) -> list[int]:
        """Stop the instances **this process started**, and only those. Returns their pids.

        ⚠️ This is the only thing that ever stops them, and the reason is the `start_new_session=True`
        two lines up. That flag is not decoration: it is what makes a startup timeout able to kill the
        whole tree (`coderun.kill_group`). The price is that the child becomes its own **session
        leader**, so it never receives the signal that ends this process — quit the app and ComfyUI
        keeps running, holding a loaded diffusion model in RAM until the machine is rebooted. Measured
        on this project: the app could be quit and the server was still answering on its port.

        An instance that was already answering when `ensure` looked is deliberately **not** in
        `_children` (a second copy of the same weights is worse than sharing one), and it is left
        untouched here on purpose: it may be one the user started themselves, and this app does not
        own it.

        ⚠️ The `wait()` after the kill is not tidiness. A child that is killed but never waited for
        stays a **zombie**, and `os.kill(pid, 0)` still succeeds against a zombie — so it keeps
        showing up as a live process for as long as this one runs, which is precisely what "it did
        not close" looks like from the outside. Measured end to end: right after the kill the port
        was already refusing connections while the pid still looked alive.
        """
        stopped: list[int] = []
        for child in self._children.values():
            if child.poll() is None:
                coderun.kill_group(child.pid)
                stopped.append(child.pid)
                try:
                    child.wait(timeout=5)   # reap it — see the note below
                except subprocess.TimeoutExpired:
                    pass
        self._children.clear()
        return stopped
