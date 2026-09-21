"""Members writing and running code: the workspace is a boundary, not a suggestion."""

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import FakeLLM
from tests.test_collab import setup


async def _yes(spec, args):                           # noqa: ANN001
    return True


@pytest.fixture
def code_env(store, make_router):
    """A group whose host may run code."""
    store.update_settings({"code_enabled": True, "code_timeout": 10, "tool_output_limit": 4000})
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    return orch, store, g


async def call(orch, store, g, args, approve=None):
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0])
    return await orch.toolhub.call(ctx, "run_code", args, approve)


# ------------------------------------------------------------------ availability
async def test_tool_is_absent_until_the_switch_is_on(store, make_router):
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    assert "run_code" not in ctx.tools

    store.update_settings({"code_enabled": True})
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    assert ctx.tools["run_code"]["risk"] == "exec", "without an explicit risk it would be filed as read-only"


async def test_it_asks_before_running(store, make_router):
    """The default permission mode is ask_risky, so an exec tool must ask every single time.

    The shared `store` fixture sets allow_all so that other tests do not have to approve
    every call; this test switches back to the product default on purpose.
    """
    store.update_settings({"code_enabled": True, "perm_mode": "ask_risky"})
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    seen = []

    async def approve(spec, args):
        seen.append(spec["name"])
        return False                                  # the user says no

    out = await call(orch, store, g, {"language": "python", "code": "print(1)"}, approve)
    assert seen == ["run_code"] and out.denied and not out.ok
    assert "did not approve" in out.text
    runs = store.data_dir / "workspace" / ".runs"
    assert not runs.exists() or not list(runs.iterdir())   # nothing was written, let alone run


# ------------------------------------------------------------------ running
async def test_python_runs_and_reports_exit_code(code_env):
    orch, store, g = code_env
    out = await call(orch, store, g, {"language": "python", "code": "print('hello', 6 * 7)"}, _yes)
    assert out.ok and "hello 42" in out.text and "exit code 0" in out.text


async def test_failure_still_returns_the_output(code_env):
    orch, store, g = code_env
    out = await call(orch, store, g, {"language": "python", "code": "import sys\nprint('boom')\nsys.exit(3)"}, _yes)
    assert not out.ok and "boom" in out.text and "exit code 3" in out.text


async def test_shell_runs(code_env):
    orch, store, g = code_env
    out = await call(orch, store, g, {"language": "shell", "code": "echo one two"}, _yes)
    assert out.ok and "one two" in out.text


async def test_empty_and_unknown_language_are_refused(code_env):
    orch, store, g = code_env
    assert not (await call(orch, store, g, {"language": "python", "code": "   "}, _yes)).ok
    out = await call(orch, store, g, {"language": "brainfuck", "code": "++"}, _yes)
    assert not out.ok and "python or shell" in out.text


async def test_files_written_stay_in_the_workspace(code_env):
    orch, store, g = code_env
    code = "open('note.txt','w').write('kept')\nprint(open('note.txt').read())"
    out = await call(orch, store, g, {"language": "python", "code": code}, _yes)
    assert out.ok and "kept" in out.text
    assert (store.data_dir / "workspace" / "note.txt").read_text() == "kept"


# ------------------------------------------------------------------ boundaries
async def test_cwd_cannot_leave_the_workspace(code_env):
    orch, store, g = code_env
    for bad in ("../../..", "sub/../../.."):
        out = await call(orch, store, g, {"language": "python", "code": "print(1)", "cwd": bad}, _yes)
        assert not out.ok and "outside the workspace" in out.text, bad


async def test_absolute_cwd_is_refused_rather_than_reinterpreted(code_env):
    """`/etc` must not silently become `<workspace>/etc`: the run would look successful while
    looking at a different directory than the model asked for."""
    orch, store, g = code_env
    for bad in ("/etc", "/tmp", "~/"):
        out = await call(orch, store, g, {"language": "python", "code": "print(1)", "cwd": bad}, _yes)
        assert not out.ok and "absolute path" in out.text, bad
    assert not (store.data_dir / "workspace" / "etc").exists()


async def test_subdirectory_is_allowed(code_env):
    orch, store, g = code_env
    out = await call(orch, store, g, {"language": "python", "code": "import os\nprint(os.path.basename(os.getcwd()))", "cwd": "pkg/sub"}, _yes)
    assert out.ok and "sub" in out.text


async def test_the_child_does_not_inherit_the_app_token(code_env, monkeypatch):
    """The app's own token and any key in the parent environment must not be readable inside."""
    orch, store, g = code_env
    monkeypatch.setenv("TEAM_AGENT_TOKEN", "deadbeef")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    code = "import os\nprint(sorted(k for k in os.environ if 'TOKEN' in k or 'API' in k))"
    out = await call(orch, store, g, {"language": "python", "code": code}, _yes)
    assert out.ok and "TEAM_AGENT_TOKEN" not in out.text and "OPENAI_API_KEY" not in out.text
    assert "[]" in out.text


async def test_timeout_kills_the_whole_process_tree(code_env):
    """A sleep in a subprocess must die with the run, not outlive it."""
    orch, store, g = code_env
    store.update_settings({"code_timeout": 1})
    child = "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\nprint('spawned', flush=True)\ntime.sleep(30)"
    t0 = time.time()
    out = await call(orch, store, g, {"language": "python", "code": child}, _yes)
    assert not out.ok and "was killed" in out.text
    assert time.time() - t0 < 8, "the timeout has to actually fire"


async def test_long_output_is_cut(code_env):
    orch, store, g = code_env
    store.update_settings({"tool_output_limit": 500})
    out = await call(orch, store, g, {"language": "python", "code": "print('x' * 5000)"}, _yes)
    assert "output cut" in out.text and len(out.text) < 900


async def test_custom_workdir_is_used(tmp_path, store, make_router):
    store.update_settings({"code_enabled": True, "code_workdir": str(tmp_path / "somewhere")})
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    out = await orch.toolhub.call(ctx, "run_code", {"language": "python", "code": "import os\nprint(os.getcwd())"}, _yes)
    assert out.ok and str(tmp_path / "somewhere") in out.text


# ------------------------------------------------------------------ end to end
async def test_a_member_runs_code_and_reads_the_output(store, make_router):
    """The whole path: a member calls the tool, the program runs, the output goes back."""
    from tests.test_collab import Collector, call as tool_call, last_user

    def script(messages):
        if "<tool_result" in last_user(messages):
            return "算完了,结果是 42。"
        return "我先算一下。" + tool_call("run_code", language="python", code="print(6 * 7)")

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    store.update_settings({"code_enabled": True, "perm_mode": "allow_all"})   # the approval path is covered above
    c = Collector()
    await orch.handle_user_message(g["id"], "帮我算 6 乘 7", c)
    await orch.drain()

    ends = c.ends()
    # One bubble: the preamble, then the answer after the tool came back
    assert ends[-1]["content"].endswith("算完了,结果是 42。") and "我先算一下" in ends[-1]["content"]
    # The run is visible to the user in the tool trace, not silently swallowed
    meta = json.dumps(ends[-1]["meta"], ensure_ascii=False)
    assert "run_code" in meta and "exit code 0" in meta and "42" in meta
