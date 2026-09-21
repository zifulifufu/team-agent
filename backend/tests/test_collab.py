"""群主分工、强项协作、工具调用循环、资料库/记忆注入、MCP —— 用可脚本化的假模型端到端验证编排器。"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from app.orchestrator import Orchestrator
from tests.conftest import FakeLLM, chunk


def role(messages):
    m = re.match(r"你是「(.+?)」", messages[0]["content"])
    return m.group(1) if m else ""


def last_user(messages):
    return messages[-1]["content"]


def setup(store, make_router, fake, **cfg):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    if cfg:
        store.update_settings(cfg)
    orch = Orchestrator(store, make_router(fake))
    return orch, store.list_groups()[0]


class Collector:
    def __init__(self):
        self.events = []

    async def __call__(self, ev):
        self.events.append(ev)

    def ends(self):
        return [e["message"] for e in self.events if e["type"] == "message_end"]

    def streamed(self, mid):
        return "".join(e["text"] for e in self.events if e["type"] == "delta" and e["message_id"] == mid)


PLAN = json.dumps({
    "goal": "写发布会通知", "conventions": "统一称呼「各位同事」;日期写成 2026-10-12",
    "tasks": [
        {"id": "t1", "owner": "文案", "title": "初稿", "instruction": "写通知初稿", "strengths": ["写作"], "deliverable": "Markdown"},
        {"id": "t2", "owner": "校对", "title": "审校", "instruction": "审校初稿", "needs": ["t1"], "strengths": ["中文"]},
    ]}, ensure_ascii=False)


def plan_script(plan=PLAN):
    def script(messages):
        name, u = role(messages), last_user(messages)
        if name == "小助" and "【分工模式】" in u:
            return "思路:先写后审。\n<plan>" + plan + "</plan>"
        if name == "小助" and "【整合】" in u:
            return "最终通知:各位同事…(来自:文案)"
        if name == "文案" and "【分工任务" in u:
            return "【分工】我负责初稿;发挥写作;用无;承接无\n初稿正文ABC"
        if name == "校对" and "【分工任务" in u:
            return "【分工】我负责审校;发挥中文;用无;承接文案\n审校意见XYZ"
        return "好的"
    return script


# ------------------------------------------------------------------ 分工
async def test_host_plans_by_strengths_and_chains_outputs(store, make_router):
    fake = FakeLLM(default=plan_script())
    orch, g = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "帮我出一份发布会通知", c)
    await orch.drain()
    assert [m["sender_name"] for m in c.ends()] == ["小助", "文案", "校对", "小助"]

    plan_intro, draft, review, final = c.ends()
    assert plan_intro["content"] == "思路:先写后审。"                      # <plan> 不出现在聊天记录里
    streamed = c.streamed(plan_intro["id"])
    assert "<plan>" not in streamed and "goal" not in streamed and streamed.startswith("思路")
    assert draft["meta"]["task_id"] == "t1" and review["meta"]["task_id"] == "t2" and final["meta"]["task_id"] == "final"

    # 任务板消息:全部完成,并指向各成员的消息
    board = [m for m in store.list_messages(g["id"]) if m["sender_type"] == "plan"]
    assert len(board) == 1
    tasks = board[0]["meta"]["tasks"]
    assert board[0]["meta"]["status"] == "done" and [t["status"] for t in tasks] == ["done", "done"]
    assert tasks[0]["message_id"] == draft["id"] and tasks[1]["needs"] == ["t1"]
    assert sum(1 for e in c.events if e["type"] == "plan") >= 5          # 每次状态变化都推送

    calls = {(role(m), "整合" if "【整合】" in last_user(m) else "任务" if "【分工任务" in last_user(m) else "其它"): m
             for _, m in fake.calls}
    # 校对拿到上游成果、统一约定、声明要求,以及带强项的分工表
    p = last_user(calls[("校对", "任务")])
    assert "初稿正文ABC" in p and "各位同事" in p and "【分工】" in p and "← 你" in p
    sysmsg = calls[("校对", "任务")][0]["content"]
    assert "强项:" in sysmsg and "〔群主〕" in sysmsg and "模型:" in sysmsg
    # 文案的历史里不重复出现自己/他人的分工成果(成果只通过任务提示传递)
    assert "审校意见XYZ" not in json.dumps(calls[("文案", "任务")], ensure_ascii=False)
    # 群主整合时看到两位成员的成果
    integ = last_user(calls[("小助", "整合")])
    assert "初稿正文ABC" in integ and "审校意见XYZ" in integ

    # 行为记入记忆:谁做了什么
    acts = store.list_memories("group", g["id"], "action")
    assert acts and "文案" in acts[0]["content"] and "校对" in acts[0]["content"]


async def test_planning_instruction_lists_members_strengths_and_past_actions(store, make_router):
    fake = FakeLLM(default="直接回答")
    orch, g = setup(store, make_router, fake)
    store.add_memory("任务「写发布会通知」→ 文案(deepseek-flash);校对(deepseek-flash);共 9 秒", "group", g["id"], "action", "auto")
    c = Collector()
    await orch.handle_user_message(g["id"], "再写一份发布会通知", c)
    msgs = fake.calls[0][1]
    u = last_user(msgs)
    assert "【分工模式】" in u and "先判断" in u and "以往类似任务的做法" in u and "文案(deepseek-flash)" in u
    assert "【群成员与分工】" in msgs[0]["content"] and "小助(协调员)〔群主〕" in msgs[0]["content"]
    assert [m["sender_name"] for m in c.ends()] == ["小助"]                # 群主判断不需要分工 → 直接回答


async def test_invalid_plan_falls_back_with_notice(store, make_router):
    bad = json.dumps({"tasks": [{"owner": "路人甲", "instruction": "x"}]}, ensure_ascii=False)
    fake = FakeLLM(default=plan_script(bad))
    orch, g = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "写通知", c)
    assert [m["sender_name"] for m in c.ends()] == ["小助"]
    notes = [m["content"] for m in store.list_messages(g["id"]) if m["sender_type"] == "system"]
    assert any("不合规" in n and "路人甲" in n for n in notes)
    assert not [m for m in store.list_messages(g["id"]) if m["sender_type"] == "plan"]


async def test_plan_modes_off_explicit_mention_and_on(store, make_router):
    fake = FakeLLM(default=plan_script())
    orch, g = setup(store, make_router, fake, plan_mode="off")
    await orch.handle_user_message(g["id"], "写通知", Collector())
    assert "【分工模式】" not in last_user(fake.calls[0][1])               # 全局关闭
    store.update_settings({"plan_mode": "auto"})
    fake.calls.clear()
    await orch.handle_user_message(g["id"], "@文案 写通知", Collector())    # 点名 → 不分工
    assert all("【分工模式】" not in last_user(m) for _, m in fake.calls)
    fake.calls.clear()
    store.update_group(g["id"], {"ext": {"plan": "off"}})                  # 群内覆盖全局
    await orch.handle_user_message(g["id"], "写通知", Collector())
    assert "【分工模式】" not in last_user(fake.calls[0][1])
    store.update_group(g["id"], {"ext": {"plan": "on"}})
    fake2 = FakeLLM(default="我直接答了")                                   # 「总是分工」但群主没给计划
    orch2 = Orchestrator(store, make_router(fake2))
    await orch2.handle_user_message(g["id"], "写通知", Collector())
    assert "必须分工" in last_user(fake2.calls[0][1])
    notes = [m["content"] for m in store.list_messages(g["id"]) if m["sender_type"] == "system"]
    assert any("总是先分工" in n for n in notes)


async def test_failed_task_does_not_block_others_and_is_reported(store, make_router):
    inner = plan_script()

    def script(messages):
        if role(messages) == "文案" and "【分工任务" in last_user(messages):
            raise RuntimeError("model down")
        return inner(messages)

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "写通知", c)
    tasks = next(m for m in store.list_messages(g["id"]) if m["sender_type"] == "plan")["meta"]
    assert [t["status"] for t in tasks["tasks"]] == ["failed", "done"] and tasks["status"] == "done"
    review_prompt = next(last_user(m) for _, m in fake.calls if role(m) == "校对")
    assert "没有产出" in review_prompt                                        # 下游被告知上游缺失
    integ = next(last_user(m) for _, m in fake.calls if "【整合】" in last_user(m))
    assert "没有完成" in integ


async def test_cancel_marks_plan_stopped(store, make_router):
    started = asyncio.Event()
    inner = plan_script()

    async def fn(**kw):
        msgs = kw["messages"]
        if role(msgs) == "文案" and "【分工任务" in last_user(msgs):
            async def gen():
                yield chunk("开头")
                started.set()
                await asyncio.sleep(30)
            return gen()
        text = inner(msgs)

        async def gen2():
            yield chunk(text)
        return gen2()

    orch, g = setup(store, make_router, fn)
    c = Collector()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "写通知", c))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    board = next(m for m in store.list_messages(g["id"]) if m["sender_type"] == "plan")["meta"]
    assert board["status"] == "stopped" and [t["status"] for t in board["tasks"]] == ["stopped", "skipped"]
    assert not any(m["sender_name"] == "文案" for m in store.list_messages(g["id"]))


# ------------------------------------------------------------------ 工具循环
def write_plugin(store, orch):
    (store.data_dir / "plugins" / "demo.py").write_text(
        'PLUGIN = {"name": "演示", "description": "演示插件"}\n'
        "def register(reg):\n"
        '    reg.register("shout", "把文字变大写", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},\n'
        '                 lambda a: a["text"].upper())\n'
        '    reg.register("boom", "总是出错", None, lambda a: 1 / 0)\n',
        encoding="utf-8")
    orch.registry.load_plugins(store.data_dir / "plugins")


def call(name, **args):
    return f'<tool_call>{json.dumps({"name": name, "arguments": args}, ensure_ascii=False)}</tool_call>'


async def test_tool_loop_runs_plugin_and_hides_call_markup(store, make_router):
    def script(messages):
        if "<tool_result" in last_user(messages):
            return "工具说:HELLO,完毕"
        return "我来处理一下。" + call("shout", text="hello")

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    write_plugin(store, orch)
    assert orch.registry.plugins["demo"].tools == ["shout", "boom"] and orch.registry.plugins["demo"].name == "演示"
    store.update_group(g["id"], {"ext": {"plugins": ["demo"]}})
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 把 hello 变大写", c)
    msg = c.ends()[0]
    assert msg["content"] == "我来处理一下。\n\n工具说:HELLO,完毕"
    assert "<tool_call>" not in c.streamed(msg["id"]) and "tool_call" not in msg["content"]
    tr = msg["meta"]["tools"]
    assert tr[0]["name"] == "shout" and tr[0]["status"] == "ok" and tr[0]["args"] == {"text": "hello"} and tr[0]["preview"] == "HELLO"
    assert [e["call"]["status"] for e in c.events if e["type"] == "tool"] == ["running", "ok"]
    assert len(fake.calls) == 2
    second = fake.calls[1][1]
    assert second[-2]["role"] == "assistant" and "<tool_call>" in second[-2]["content"]
    assert '<tool_result name="shout" ok="true">\nHELLO' in second[-1]["content"]
    assert "shout(text: string)" in fake.calls[0][1][0]["content"]         # 工具清单进了系统提示词
    # 用了工具的一次协作会被记成「过往做法」
    assert any("shout" in m["content"] for m in store.list_memories("group", g["id"], "action"))


async def test_tool_errors_unknown_and_disabled_are_reported_to_model(store, make_router):
    seen = []

    def script(messages):
        u = last_user(messages)
        if "<tool_result" in u:
            seen.append(u)
            return "收到"
        return call("boom") + call("nope") + call("shout", text="x") + "<tool_call>坏格式</tool_call>"

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    write_plugin(store, orch)
    store.update_group(g["id"], {"ext": {"plugins": ["demo"]}})
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 试试", c)
    text = seen[0]
    assert 'name="boom" ok="false"' in text and "ZeroDivisionError" in text
    assert 'name="nope" ok="false"' in text and "没有名为 nope" in text
    assert 'name="shout" ok="true"' in text
    assert len(c.ends()[0]["meta"]["tools"]) == 3                           # 单次最多 3 个调用,第 4 个(坏格式)被丢弃


async def test_plugin_not_enabled_in_group_is_unavailable(store, make_router):
    fake = FakeLLM(default=lambda m: "ok")
    orch, g = setup(store, make_router, fake)
    write_plugin(store, orch)
    await orch.handle_user_message(g["id"], "@文案 hi", Collector())
    assert "shout(" not in fake.calls[0][1][0]["content"]
    assert "current_time" in fake.calls[0][1][0]["content"]                 # 内置工具始终可用


async def test_tool_rounds_are_capped_and_can_be_disabled(store, make_router):
    fake = FakeLLM(default=lambda m: "还要查" + call("current_time"))
    orch, g = setup(store, make_router, fake, tool_rounds=2)
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 一直查", c)
    assert len(fake.calls) == 3 and c.ends()                                 # 1 次 + 2 轮工具后强制收尾
    fake.calls.clear()
    store.update_settings({"tool_rounds": 0})
    await orch.handle_user_message(g["id"], "@文案 再来", Collector())
    assert len(fake.calls) == 1 and "【可用工具】" not in fake.calls[0][1][0]["content"]


async def test_midstream_reset_restores_earlier_visible_text(store, make_router):
    state = {"n": 0}

    async def fn(**kw):
        msgs = kw["messages"]
        model = kw["model"]
        state["n"] += 1

        async def gen():
            if "<tool_result" not in last_user(msgs):
                for i in range(0, 20, 5):
                    yield chunk(("先看资料。" + call("current_time"))[i:i + 5])
                yield chunk(("先看资料。" + call("current_time"))[20:])
                return
            if model.startswith("deepseek/"):
                yield chunk("半截")
                raise ConnectionError("断了")
            yield chunk("本地完整答复")
        return gen()

    orch, g = setup(store, make_router, fn)
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 看下时间", c)
    msg = c.ends()[0]
    mid = msg["id"]
    # 前端按事件顺序还原:reset 清空后要把第一轮已经展示过的文字补回去
    shown = ""
    for e in c.events:
        if e.get("message_id") != mid:
            continue
        if e["type"] == "delta":
            shown += e["text"]
        elif e["type"] == "reset":
            shown = ""
    assert shown.strip() == msg["content"] == "先看资料。\n\n本地完整答复"
    assert msg["fallback_from"] and msg["model_id"].startswith("ollama/")


# ---------------------------------------------------------------- 资料库/记忆
async def test_library_tool_and_refs_and_scope(store, make_router):
    def script(messages):
        if "<tool_result" in last_user(messages):
            return "依据资料:" + ("600" if "600" in last_user(messages) else "无")
        return call("library_search", query="住宿标准")

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    orch.library.add_text("差旅制度", "出差住宿标准:一线城市每晚不超过 600 元。")
    orch.library.add_text("食堂", "周一红烧肉")
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 出差住宿标准是多少", c)
    assert c.ends()[0]["content"] == "依据资料:600" and c.ends()[0]["meta"]["tools"][0]["name"] == "library_search"
    assert "《差旅制度》" in fake.calls[1][1][-1]["content"]
    # 限定只用「食堂」→ 搜不到差旅制度
    lib_ids = [d["id"] for d in store.list_docs() if d["title"] == "食堂"]
    store.update_group(g["id"], {"ext": {"library": {"mode": "selected", "ids": lib_ids}}})
    c2 = Collector()
    await orch.handle_user_message(g["id"], "@文案 出差住宿标准是多少", c2)
    assert c2.ends()[0]["content"] == "依据资料:无"
    # 关闭资料库 → 工具不出现
    store.update_group(g["id"], {"ext": {"library": {"mode": "off", "ids": []}}})
    fake.calls.clear()
    await orch.handle_user_message(g["id"], "@文案 hi", Collector())
    assert "- library_search(" not in fake.calls[0][1][0]["content"]


async def test_hash_refs_inline_document_text(store, make_router):
    fake = FakeLLM(default="好")
    orch, g = setup(store, make_router, fake)
    orch.library.add_text("差旅制度", "出差住宿标准:一线城市每晚不超过 600 元。")
    await orch.handle_user_message(g["id"], "@文案 按 #差旅制度 总结一下", Collector())
    s = fake.calls[0][1][0]["content"]
    assert "【用户引用的资料】" in s and "不超过 600 元" in s
    fake.calls.clear()
    await orch.handle_user_message(g["id"], "@文案 按 #不存在的文档 总结", Collector())
    assert "【用户引用的资料】" not in fake.calls[0][1][0]["content"]


async def test_memory_injected_scoped_and_toggleable_and_saved_by_tool(store, make_router):
    def script(messages):
        return call("memory_save", content="发布会主持人定为小王", kind="decision") if "<tool_result" not in last_user(messages) and "记住" in last_user(messages) else "好"

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    store.add_memory("永远用中文回复", "global", kind="preference", pinned=True)
    store.add_memory("别的群的事", "group", "other", "fact")
    await orch.handle_user_message(g["id"], "@文案 hi", Collector())
    s = fake.calls[0][1][0]["content"]
    assert "【记忆】" in s and "永远用中文回复" in s and "别的群的事" not in s
    store.update_group(g["id"], {"ext": {"memory": False}})                  # 本群关闭记忆
    fake.calls.clear()
    await orch.handle_user_message(g["id"], "@文案 hi", Collector())
    assert "【记忆】" not in fake.calls[0][1][0]["content"] and "memory_save" not in fake.calls[0][1][0]["content"]
    store.update_group(g["id"], {"ext": {"memory": True}})
    await orch.handle_user_message(g["id"], "@文案 记住:主持人是小王", Collector())
    assert any("小王" in m["content"] and m["source"] == "auto" for m in store.list_memories("group", g["id"]))
    store.update_group(g["id"], {"ext": {"memory": True}})
    # 含密钥的内容拒绝保存
    fake3 = FakeLLM(default=lambda m: call("memory_save", content="密码是 abc") if "<tool_result" not in last_user(m) else "ok")
    orch3 = Orchestrator(store, make_router(fake3))
    c = Collector()
    await orch3.handle_user_message(g["id"], "@文案 x", c)
    assert c.ends()[0]["meta"]["tools"][0]["status"] == "failed"
    assert not any("abc" in m["content"] for m in store.list_memories())


async def test_auto_extract_runs_in_background_after_reply(store, make_router):
    reply = json.dumps([{"scope": "global", "kind": "preference", "content": "汇报先给结论"}], ensure_ascii=False)

    def script(messages):
        return reply if "记忆整理员" in last_user(messages) else "这是结论:通过"

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake, memory_auto_extract=True)
    await orch.handle_user_message(g["id"], "@文案 给个结论", Collector())
    await orch.drain()
    assert any(m["content"] == "汇报先给结论" and m["source"] == "auto" for m in store.list_memories())


async def test_global_prompt_group_prompt_and_group_skills_reach_system_prompt(store, make_router):
    from app.tools import ensure_example_skills

    ensure_example_skills(store.data_dir / "skills")
    fake = FakeLLM(default="ok")
    orch, g = setup(store, make_router, fake)
    store.update_settings({"system_prompt": "你是「{{agent_name}}」。今天 {{date}}。群名 {{group_name}}。"})
    store.add_prompt("全局", "回答不超过 100 字", "general", use_globally=True)
    store.add_prompt("不启用", "这句不该出现", "general", use_globally=False)
    store.update_group(g["id"], {"prompt": "本群项目:{{group_name}};成员 {{members}}", "ext": {"skills": ["头脑风暴规则"]}})
    await orch.handle_user_message(g["id"], "@文案 hi", Collector())
    s = fake.calls[0][1][0]["content"]
    assert s.startswith("你是「文案」。今天 20") and "群名 产品发布小组" in s
    assert "【补充要求】" in s and "回答不超过 100 字" in s and "这句不该出现" not in s
    assert "【本群提示词】\n本群项目:产品发布小组;成员 小助、文案、分镜、校对" in s
    assert "【群聊规则:头脑风暴规则】" in s and "【技能:公文写作规范】" in s     # 群技能 + 成员自己的技能
    assert "{{" not in s


# ------------------------------------------------------------------- MCP
async def test_mcp_tools_are_called_through_real_stdio_server(store, make_router):
    def script(messages):
        if "<tool_result" in last_user(messages):
            return "结果是 " + re.search(r"ok=\"true\">\n(.*?)\n</tool_result>", last_user(messages), re.S).group(1)
        return call("mcp__echo__add", a=2, b=3)

    fake = FakeLLM(default=script)
    orch, g = setup(store, make_router, fake)
    srv = store.add_mcp("echo", sys.executable, [str(Path(__file__).parent / "mcp_echo_server.py")])
    store.update_group(g["id"], {"ext": {"mcp": [srv["id"]]}})
    try:
        c = Collector()
        await orch.handle_user_message(g["id"], "@文案 算 2+3", c)
        msg = c.ends()[0]
        assert msg["content"] == "结果是 5" and msg["meta"]["tools"][0]["name"] == "mcp__echo__add"
        assert "mcp__echo__echo(" in fake.calls[0][1][0]["content"] and "[echo]" in fake.calls[0][1][0]["content"]
        assert orch.mcp.state(srv["id"]).status == "ready"
    finally:
        await orch.mcp.shutdown()


async def test_unreachable_mcp_server_is_reported_once_and_does_not_block(store, make_router):
    fake = FakeLLM(default="ok")
    orch, g = setup(store, make_router, fake)
    srv = store.add_mcp("broken", "/definitely/not/a/command", [])
    store.update_group(g["id"], {"ext": {"mcp": [srv["id"]]}})
    try:
        c = Collector()
        await orch.handle_user_message(g["id"], "@所有人 报到", c)
        notes = [m["content"] for m in store.list_messages(g["id"]) if m["sender_type"] == "system"]
        assert sum("MCP「broken」未连接" in n for n in notes) == 1          # 同一次协作里只提醒一次
        assert len(c.ends()) == 4
    finally:
        await orch.mcp.shutdown()
