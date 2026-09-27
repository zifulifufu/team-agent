"""The local tools: where the program is, what argv we hand it, and what the child can see.

This module had **no tests** until 2026-09-25, and three separate defects lived in it because of
that — each one invisible to the settings page and each one ending a member's turn:

* the program could not be found at all if it was installed anywhere but `PATH` (VoiceStudio's own
  instruction is "clone it and run `uv sync`", which puts the console script in that clone's
  virtualenv), and there was no field to say where it was;
* the model cache became invisible because `_env` redirects `HOME`, so a synthesiser with 2.3 GB of
  weights on disk spent minutes trying to download them from a hub this machine cannot reach;
* the output file was named `.mp4` for every tool, which a speech synthesiser cannot write.

What the tests below pin down is the *agreement* between the parts, not any one of them: the path a
probe reports is the path a run starts, the cache the child sees is the one the user has, and the
name we hand a tool is one it can actually write.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app import bindirs, external, localcmd


def exe(tmp_path: Path, name: str = "omnivoice-infer", body: str = 'echo "usage: fake"') -> Path:
    """A stand-in for a tool's console script: a real, executable file that answers `--help`."""
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(0o755)
    return p


# ------------------------------------------------------------------ where the program is
def test_the_path_the_user_set_is_the_one_used(tmp_path):
    """The field exists because the install instruction puts this program somewhere no search can
    find. Nothing else in `argv_for`'s output may depend on how it was found."""
    p = exe(tmp_path)
    assert localcmd.exe_for("voicestudio", str(p)) == str(p)
    argv = localcmd.argv_for("voicestudio", out="/tmp/x.wav", instruction="你好", cli_path=str(p))
    assert argv[0] == str(p)
    assert argv[1:] == ["--text", "你好", "--output", "/tmp/x.wav"]


def test_a_probe_reports_the_program_a_run_will_start(tmp_path):
    """The whole point of one exit for "where is it": a settings page that says ready while the turn
    starts a different program is the failure this agreement prevents."""
    p = exe(tmp_path)
    info = localcmd.probe("voicestudio", cli_path=str(p))
    assert info["found"] is True and info["path"] == str(p)
    assert info["version"].startswith("usage: fake")        # read by running the resolved program
    assert localcmd.argv_for("voicestudio", out="/tmp/x.wav", instruction="x",
                             cli_path=str(p))[0] == info["path"]


def test_nothing_found_is_not_found_and_says_what_to_do(tmp_path):
    """A name that is not on `PATH` stays a miss — and the hint has to name the second way out, or
    the only advice a user gets is an install command they have already run."""
    info = localcmd.probe("voicestudio", cli_path=str(tmp_path / "not-here"))
    assert info["found"] is False and info["path"] == ""
    assert "not-here" in info["hint"]                       # the path they typed, quoted back
    assert "by hand" in info["hint"] or "手动指定" in info["hint"]


def test_a_broken_path_does_not_silently_fall_back_to_searching(tmp_path, monkeypatch):
    """If the user pointed at something, that is the answer. Falling back to a name search would turn
    a typo into "installed and ready", and then into a failure somewhere else."""
    monkeypatch.setattr(bindirs, "tool", lambda name: "/somewhere/else/" + name)
    asked = localcmd.exe_for("voicestudio", str(tmp_path / "typo"))
    assert asked == str(tmp_path / "typo")
    assert localcmd.probe("voicestudio", cli_path=asked)["found"] is False


def test_the_resolver_asks_the_module_that_already_knows(tmp_path, monkeypatch):
    """`voices` owns the same binary for narration, and it is the module that knows a cloning engine
    lives in a project's virtualenv. Two lookups for one program is how one path works and the other
    does not."""
    from app import voices

    monkeypatch.setattr(voices, "binary", lambda engine, override="": "/from/voices/" + engine)
    monkeypatch.setattr(bindirs, "tool", lambda name: None)
    assert localcmd.exe_for("voicestudio") == "/from/voices/" + localcmd.VOICE_ENGINE


# --------------------------------------------------------------- the name we give the output
def test_the_output_name_uses_the_tools_own_extension():
    """A synthesiser writes audio and takes the extension as the instruction about what to encode:
    handed `.mp4` it dies with `ValueError: Unsupported format: mp4`, after the model has loaded.
    A renderer still wants `.mp4`, so this is per row and not a global."""
    assert localcmd._safe_out("voicestudio", "T").endswith(".wav")
    assert localcmd._safe_out("remotion", "T").endswith(".mp4")
    assert localcmd.TOOLS["voicestudio"]["out_ext"] == ".wav"
    # …and the extension has to be one the collector will pick up afterwards, or the file the turn
    # produced is the file the group never sees.
    arts = localcmd.TOOLS["voicestudio"]["artifacts"]
    assert any(localcmd._safe_out("voicestudio", "T").endswith(a.lstrip("*")) for a in arts)


# ------------------------------------------------------------------- what the child can see
def test_the_model_cache_is_visible_even_though_home_moves(tmp_path, monkeypatch):
    """The measured failure: `HOME` is redirected into the tool's folder so npm and Chrome caches
    stay out of the real home — and huggingface_hub resolves its cache from `HOME`, so 2.3 GB of
    weights already on disk were reported as "not downloaded" after a 398-second timeout."""
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.delenv("HF_HOME", raising=False)
    env = localcmd._env(tmp_path)
    assert env["HOME"] == str(tmp_path)                                     # still redirected
    assert env["HF_HOME"] == "/Users/someone/.cache/huggingface"            # and still findable


def test_the_users_own_cache_settings_win(monkeypatch):
    """An `HF_ENDPOINT` mirror, or an `HF_HOME` they moved, is knowledge we do not have. Passing it
    through is also what lets a run work offline when the hub is unreachable."""
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    out = bindirs.model_cache_env()
    assert out["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert out["HF_HUB_OFFLINE"] == "1"
    assert out["HF_HOME"].endswith("/Users/someone/.cache/huggingface")


def test_a_model_cache_environment_is_never_empty(monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    assert bindirs.model_cache_env("/tmp/home")["HF_HOME"] == "/tmp/home/.cache/huggingface"


# ------------------------------------------------------------------------- what may be saved
def test_a_local_tool_may_be_pointed_at_a_program_and_a_gateway_may_not(tmp_path):
    """The `cli` branch restricts the file name to `codebuddy*` so a different program is not picked
    by mistake. That rule has no meaning for a local tool, whose program is called whatever its
    author called it — while the "must be an executable file" half applies to both."""
    p = exe(tmp_path, name="omnivoice-infer")
    cfg = external.clean_cfg({"cli_path": str(p)}, engine="voicestudio")
    assert cfg["cli_path"] == str(p)
    with pytest.raises(ValueError):
        external.clean_cfg({"cli_path": str(tmp_path / "missing")}, engine="voicestudio")
    plain = tmp_path / "not-exec"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        external.clean_cfg({"cli_path": str(plain)}, engine="voicestudio")
    with pytest.raises(ValueError):
        external.clean_cfg({"cli_path": str(p)}, engine="workbuddy")   # still refused there


def test_a_local_tool_ignores_the_fields_that_do_not_apply_to_it(tmp_path):
    """It is a program, not a conversation partner: a working directory or an extra directory would
    be silently ignored, so they are cleared rather than accepted."""
    cfg = external.clean_cfg({"cwd": "/tmp", "add_dirs": ["/tmp", "/usr"]}, engine="voicestudio")
    assert cfg["cwd"] == "" and cfg["add_dirs"] == []


def test_the_engine_is_told_where_the_program_is_by_any_of_its_members(store, tmp_path):
    """A local tool is one program on this machine, and where it lives is a fact about the machine.
    The field lives on a member, so the earliest member that has one answers for the engine — which
    is what stops the engine list saying "not ready" after the user has already pointed at it, and
    what keeps a second member of the same engine from failing on its first turn.

    (This machine really had two VoiceStudio members, one configured and one not.)
    """
    runner = external.ExternalRunner(store.data_dir, store=store)
    p = exe(tmp_path)
    assert runner.engine_path("voicestudio") == ""              # nobody has said yet

    store.create_agent("VoiceStudio", engine="voicestudio", engine_cfg={"cli_path": str(p)})
    assert runner.engine_path("voicestudio") == str(p)
    # What the orchestrator and the engine list both do with it: hand it to the one resolver. Every
    # member of this engine ends up starting this program, configured or not.
    assert localcmd.exe_for("voicestudio", runner.engine_path("voicestudio")) == str(p)
    assert localcmd.probe("voicestudio", cli_path=runner.engine_path("voicestudio"))["found"] is True

    store.create_agent("VoiceStudio2", engine="voicestudio", engine_cfg={})
    assert runner.engine_path("voicestudio") == str(p)          # still the one that was set
    # …and a different engine is unaffected: this is per engine, not a global.
    assert runner.engine_path("remotion") == ""


def test_what_a_member_is_told_when_its_own_call_is_unparsable():
    """The round that cost a whole delivery: seven parse failures from one member, the same arguments
    every time, because the feedback said what was expected and never quoted what had arrived."""
    from app.toolcall import ToolCall, parse_failure, parse_tool_calls

    _, bad = parse_tool_calls("<tool_call>这不是 JSON</tool_call>")
    said = parse_failure(bad[0])
    assert "Wrong shape" in said or "格式不对" in said
    assert "这不是 JSON" in said                 # what it sent, so a retry has something to correct
    # A long one is trimmed rather than echoed whole: this text goes back into the prompt.
    trimmed = parse_failure(ToolCall("", {}, "reason", "x" * 900))
    assert len(trimmed) < 400 and trimmed.endswith("…")
    # Nothing sent, nothing to quote: the reason alone is the whole message.
    assert parse_failure(ToolCall("", {}, "reason", "")) == "reason"


def test_a_failed_check_names_the_tool_not_the_runtime(monkeypatch):
    """⚠️ 实测的误报:`npx --no-install hyperframes --version` 退出码非零时,应用说的是
    「还不能用:缺少 /usr/local/bin/npx」—— 用户于是去查 npx,而 `npx --version` 明明能跑(10.9.4)。

    病根:那句「是不是 npx」是用**解析后的绝对路径**判断的(`str(check[0]).startswith("npx")`),
    而 `exe_for` 交给它的正是 `/usr/local/bin/npx` —— 永不以 "npx" 开头,于是走错分支、
    把 npx 的路径当成了缺的东西。真正缺的是**那个包**。
    现在不猜是哪个文件缺,只说「这个工具自己的检查命令失败了」,并保留安装命令。
    """
    monkeypatch.setattr(localcmd, "_exe_ok", lambda p: bool(p))
    monkeypatch.setattr(localcmd, "exe_for", lambda engine, cli_path="": "/usr/local/bin/npx")
    monkeypatch.setattr(localcmd, "_binary", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(localcmd, "_try", lambda *a, **k: (1, "", "npm error 404 Not Found"))
    monkeypatch.setattr(localcmd, "_node_major", lambda: 22)

    info = localcmd.probe("hyperframes")
    assert info["found"] is False
    # 名字说的是**这个工具**,不是它的运行时;而且安装命令仍在,用户照着做就行。
    assert "HyperFrames" in info["hint"], info["hint"]
    assert "npm install -g hyperframes" in info["hint"], info["hint"]
    assert "缺少 /usr/local/bin/npx" not in info["hint"], info["hint"]


def test_qwen3tts_is_a_speech_tool_with_a_real_check():
    """Qwen3-TTS(2026-09-25 接进来的旁白/配音引擎)在这一张表里的三条硬要求。

    ⚠️ 前两条都是"挑错会挑在最贵的地方":
    1. `out_ext` 必须是 `.wav` —— 合成器拿到 `.mp4` 会在**模型加载完之后**才报 `Unsupported format`,
       几分钟白等(VoiceStudio 那一行就是为了这个才单独写了 `out_ext`);
    2. `verify` 必须是**它自己的自检**(`--check` 会 import torch/qwen_tts 并逐个找权重),
       不能是 `--help`:那种检查在包没装好、权重没下完时也会回答"就绪",而**探针说假话**正是
       这张表存在的全部理由;
    3. 参考音色不在 `cmd` 里,而是走 `voice_engine`:旁白侧和群里的成员必须跑**同一个程序、同一段
       参考音频**,否则"视频里的旁白"和"群里念的那句"会是两个声音。
    """
    row = localcmd.row("qwen3tts")
    assert row is not None, "qwen3tts 应当在这张表里"
    assert row["out_ext"] == ".wav", f"合成器要写音频,不是 {row['out_ext']}"
    assert row["verify"] == ["qwen-tts-say", "--check"], f"检查命令是假的:{row['verify']}"
    assert row["voice_engine"] == "qwen3tts", "它同时是旁白引擎,这条要写出来"

    exe = "/opt/ta/qwen-tts-say"
    plain = localcmd.argv_for("qwen3tts", out="/tmp/o.wav", instruction="患者,男,68 岁。", cli_path=exe)
    assert plain == [exe, "--text", "患者,男,68 岁。", "--output", "/tmp/o.wav"], plain

    # 有参考音频时**委托给旁白侧**:同一套旗标、同一个程序(argv[0] 仍然是解析出来的那个)
    with_ref = localcmd.argv_for("qwen3tts", out="/tmp/o.wav", instruction="念这句",
                                 ref_audio="/tmp/ref.wav", ref_text="参考原话", cli_path=exe)
    assert with_ref[0] == exe, with_ref
    assert "--ref-audio" in with_ref and "/tmp/ref.wav" in with_ref, with_ref
    assert "--ref-text" in with_ref and "参考原话" in with_ref, with_ref
