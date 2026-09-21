"""测试用的假 codebuddy:按文档里的 stream-json 格式输出,并把收到的参数/标准输入/环境记到日志里。
用环境变量 CODEBUDDY_FAKE_MODE 控制行为:ok | delta | slow | error | crash | auth | raw | version"""
import json
import os
import sys
import time

argv = sys.argv[1:]
mode = os.environ.get("CODEBUDDY_FAKE_MODE", "ok")
if "--version" in argv:
    print("9.9.9-fake")
    sys.exit(0)
stdin = sys.stdin.read()
log = os.environ.get("CODEBUDDY_FAKE_LOG")
if log:
    with open(log, "w") as f:
        json.dump({"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
                   "env": sorted(os.environ.keys())}, f)


def out(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)


if mode == "crash":
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
if mode == "auth":
    sys.stderr.write("Error: not logged in, please login first\n")
    sys.exit(1)
if mode == "raw":
    print("这是纯文本输出,不是 JSON")
    sys.exit(0)
if mode == "slow":
    time.sleep(60)

out({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "fake-model", "tools": ["Read"]})
if mode == "delta":
    out({"type": "stream_event", "event": {"type": "message_start"}})
    for piece in ("你", "好,", "我是假 ", "WorkBuddy"):
        out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": piece}}})
    out({"type": "assistant", "session_id": "sess-1", "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "你好,我是假 WorkBuddy"}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": "你好,我是假 WorkBuddy",
         "session_id": "sess-1", "total_cost_usd": 0.0012, "duration_ms": 321, "num_turns": 1, "permission_denials": []})
    sys.exit(0)

out({"type": "assistant", "session_id": "sess-1", "parent_tool_use_id": None,
     "message": {"role": "assistant", "content": [
         {"type": "text", "text": "我先看看文件。"},
         {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "/tmp/x.md"}}]}})
out({"type": "user", "session_id": "sess-1", "parent_tool_use_id": None,
     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "文件内容 abc"}]}})
out({"type": "assistant", "session_id": "sess-1", "parent_tool_use_id": "sub-1",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "(子智能体的中间话,不该出现)"}]}})
if mode == "error":
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "sess-1",
         "errors": ["模型服务返回 401 unauthorized"], "duration_ms": 10, "num_turns": 1})
    sys.exit(1)
out({"type": "assistant", "session_id": "sess-1", "parent_tool_use_id": None,
     "message": {"role": "assistant", "content": [{"type": "text", "text": "读完了:文件里写着 abc。 @Copywriter 请确认。"}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": "读完了:文件里写着 abc。 @Copywriter 请确认。",
     "session_id": "sess-1", "total_cost_usd": 0.01, "duration_ms": 1234, "num_turns": 2,
     "permission_denials": [{"tool_name": "Bash", "tool_input": {}}]})
