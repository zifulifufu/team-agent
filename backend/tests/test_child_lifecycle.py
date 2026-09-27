"""What the app must stop when it stops: the servers it started itself.

⚠️ The two here — ComfyUI (`comfy_runtime`) and the embedding server (`embed`) — are both launched
with `start_new_session=True`. That flag is load-bearing: it is what lets a startup timeout kill the
child's whole process tree (`coderun.kill_group`). Its price is that the child becomes its own
**session leader**, so it never receives the signal that ends this process.

There is therefore no second chance and no safety net: quitting the app is the only thing that ever
stops them. If it does not, they hold a loaded diffusion model and a loaded sentence-transformers
model in RAM until the machine is rebooted — and ComfyUI keeps its port, which additionally makes the
next "start ComfyUI" silently adopt the stale instance instead of failing loudly.
"""
from __future__ import annotations

import os
import signal
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import comfy_runtime, embed
from app.main import create_app


def _running_child() -> subprocess.Popen:
    """A stand-in server: really running, and in its own session — the shape under test."""
    return subprocess.Popen(["/bin/sleep", "300"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _exit_code(child: subprocess.Popen) -> int | None:
    """Reaps the child and returns its exit code — or `None` if it is **still alive**, which is the
    failure under test. Returning the value rather than raising `TimeoutExpired` is the point: a
    process that was not killed has to be an assertable fact, not a second kind of error."""
    try:
        return child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return None


def test_a_comfyui_this_app_started_is_stopped_on_shutdown(tmp_path):
    runtime = comfy_runtime.LocalRuntime(tmp_path)
    child = _running_child()
    runtime._children["http://127.0.0.1:8188"] = child
    try:
        assert runtime.stop() == [child.pid]
        assert _exit_code(child) == -signal.SIGKILL, "the instance was not killed"
        assert runtime._children == {}, "a stopped child must not be remembered as running"
    finally:
        child.kill()


def test_a_comfyui_that_was_already_answering_is_not_ours_to_kill(tmp_path):
    """`ensure` never starts a second copy of the same weights — it adopts an instance that answers.
    Such an instance was not started by this process, so it is not in `_children`, and `stop` must
    not go hunting for it by address: it may be the user's own ComfyUI."""
    runtime = comfy_runtime.LocalRuntime(tmp_path)
    bystander = _running_child()
    try:
        assert runtime.stop() == []
        assert bystander.poll() is None, "a server this app did not start was killed"
    finally:
        bystander.kill()
        bystander.wait()


def test_an_embedding_server_this_app_started_is_stopped_on_shutdown():
    child = _running_child()
    embed._STARTED[child.pid] = child
    try:
        assert embed.stop() == [child.pid]
        assert _exit_code(child) == -signal.SIGKILL, "the server was not killed"
        assert embed._STARTED == {}
    finally:
        child.kill()


def test_a_stopped_server_does_not_stay_a_zombie():
    """⛔ Found end to end, not imagined. After the kill the port was already refusing connections —
    the memory really was back — while `os.kill(pid, 0)` still succeeded. The child had been killed
    but **never waited for**, so it sat in the process table as a zombie for as long as the app lived.
    "The app closed it" and "it is gone" have to be the same sentence."""
    child = _running_child()
    embed._STARTED[child.pid] = child
    try:
        assert embed.stop() == [child.pid]
        with pytest.raises(OSError):
            os.kill(child.pid, 0)        # a zombie still answers `kill(pid, 0)`; a reaped one does not
    finally:
        child.kill()


def test_stop_reports_only_servers_that_were_still_running():
    """A pid that exited on its own must not be reported as stopped. Otherwise the shutdown log
    claims to have killed something that was already gone, and a real leak hides behind it."""
    child = _running_child()
    child.kill()
    child.wait()
    embed._STARTED[child.pid] = child
    assert embed.stop() == []
    assert embed._STARTED == {}


def test_quitting_the_app_stops_the_servers_it_started(tmp_path, monkeypatch):
    """The tests above prove `stop` works; this one proves **the app calls it**. Without it the
    servers are stopped only by code nobody runs, which is the same as not stopping them — and that
    is precisely how this was broken: `LocalRuntime` had no stop method at all."""
    stopped: list[str] = []
    monkeypatch.setattr(comfy_runtime.LocalRuntime, "stop",
                        lambda self: stopped.append("comfyui") or [])
    monkeypatch.setattr(embed, "stop", lambda: stopped.append("embed") or [])

    app = create_app(tmp_path / "data", completion_fn=None, background=False)
    with TestClient(app, base_url="http://127.0.0.1"):
        pass                 # the lifespan's `finally` is what runs on the way out

    assert stopped == ["comfyui", "embed"]
