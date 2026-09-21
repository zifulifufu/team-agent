"""联调探针:在真机上检查 WorkBuddy 自带的 codebuddy 命令行能否被 Team Agent 驱动。结果写到 ./out/ 下,不改任何设置。
用法:cd ~/team-agent/_probe && ../.venv/bin/python probe_external.py"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import external  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
report = {"python": sys.version, "platform": sys.platform, "steps": {}}


def save():
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


async def raw_run(name, cfg, prompt):
    lc = external.find_launcher(cfg.get("cli_path", ""))
    if not lc:
        report["steps"][name] = {"error": "找不到命令行"}
        return
    args = external.build_args(cfg, "你是测试成员 WorkBuddy。")
    env = external.build_env(cfg, lc)
    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(
        *lc.argv, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(OUT), env=env, limit=16 * 1024 * 1024)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), 120)
    except asyncio.TimeoutError:
        proc.kill()
        report["steps"][name] = {"error": "120 秒超时"}
        return
    (OUT / f"{name}.stdout.jsonl").write_bytes(out)
    (OUT / f"{name}.stderr.txt").write_bytes(err)
    types = []
    for ln in out.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(ln)
            types.append(e.get("type", "?") + (":" + str(e["subtype"]) if e.get("subtype") else "")
                         + (":" + str(e["event"].get("type")) if isinstance(e.get("event"), dict) else ""))
        except ValueError:
            types.append("非JSON")
    report["steps"][name] = {
        "launcher": lc.argv, "via": lc.via, "args": [a if len(a) < 80 else a[:80] + "…" for a in args],
        "rc": proc.returncode, "seconds": round(time.time() - t0, 1), "stdout_bytes": len(out),
        "event_types": types[:60], "stderr_tail": err.decode("utf-8", "replace")[-500:],
    }


async def main():
    report["found"] = external.ExternalRunner(OUT).describe()
    save()
    runner = external.ExternalRunner(OUT)
    report["probe_version_only"] = await runner.probe({})
    save()
    prompt = "这是连通性测试。请只回复:OK"
    cfg = {**external.DEFAULT_CFG, "max_turns": 1, "timeout": 120}
    await raw_run("A_default_env", cfg, prompt)
    save()
    a = report["steps"].get("A_default_env", {})
    if a.get("rc") not in (0,) or not a.get("stdout_bytes"):
        await raw_run("B_workbuddy_config", {**cfg, "use_workbuddy_config": True}, prompt)
        save()
    ex = await runner.probe({}, live=True)
    report["probe_live_default"] = ex
    save()
    print("done; see", OUT / "report.json")


asyncio.run(main())
