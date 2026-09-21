from app.orchestrator import Orchestrator, find_mentions
from tests.conftest import FakeLLM


def setup(store, make_router, fake):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    orch = Orchestrator(store, make_router(fake))
    g = store.list_groups()[0]
    return orch, g, {a["name"]: a for a in store.list_agents()}


class Collector:
    def __init__(self):
        self.events = []

    async def __call__(self, ev):
        self.events.append(ev)

    def ends(self):
        return [e["message"] for e in self.events if e["type"] == "message_end"]


def by_sender(fake_reply_map):
    """按 system prompt 里的「名字」返回不同回复。"""

    def pick(messages):
        sys = messages[0]["content"]
        for name, reply in fake_reply_map.items():
            if sys.startswith(f"你是「{name}」"):
                return reply
        return "好的"

    return pick


def test_find_mentions_longest_first_and_order():
    members = [{"id": "1", "name": "文案"}, {"id": "2", "name": "文案组"}, {"id": "3", "name": "校对"}]
    got = find_mentions("请 @校对 和 @文案组 看看", members)
    assert [m["name"] for m in got] == ["校对", "文案组"]
    assert find_mentions("@文案组", members)[0]["name"] == "文案组"
    assert len(find_mentions("@文案组", members)) == 1


async def test_no_mention_goes_to_host(store, make_router):
    fake = FakeLLM(default="收到")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "帮我写个发布会通知", c)
    assert [m["sender_name"] for m in c.ends()] == ["小助"]


async def test_handoff_chain_via_at_mentions(store, make_router):
    fake = FakeLLM(default=by_sender({
        "小助": "拆解完毕。@文案 请写初稿。",
        "文案": "初稿如下……@校对 请审校。",
        "校对": "已审校,无问题。",
    }))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "写一篇通知", c)
    assert [m["sender_name"] for m in c.ends()] == ["小助", "文案", "校对"]
    # 消息与模型信息已落库
    saved = store.list_messages(g["id"])
    assert [m["sender_name"] for m in saved][-3:] == ["小助", "文案", "校对"]
    assert saved[-1]["model_id"] == "deepseek/deepseek-flash"


async def test_explicit_mention_skips_host(store, make_router):
    fake = FakeLLM(default="行")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@分镜 给我一个 15 秒的分镜", c)
    assert [m["sender_name"] for m in c.ends()] == ["分镜"]


async def test_at_all_fans_out_to_everyone(store, make_router):
    fake = FakeLLM(default="到")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@所有人 自我介绍", c)
    assert [m["sender_name"] for m in c.ends()] == ["小助", "文案", "分镜", "校对"]


async def test_ping_pong_is_capped_by_max_hops(store, make_router):
    store.update_settings({"max_hops": 5})
    fake = FakeLLM(default=by_sender({"文案": "@校对 你看看", "校对": "@文案 你再改改"}))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 开始", c)
    assert len(c.ends()) == 5
    last = store.list_messages(g["id"])[-1]
    assert last["sender_type"] == "system" and "最大发言轮数" in last["content"]


async def test_self_mention_ignored(store, make_router):
    fake = FakeLLM(default=by_sender({"文案": "我 @文案 自己搞定"}))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@文案 写", c)
    assert len(c.ends()) == 1


async def test_context_labels_speakers_and_merges_roles(store, make_router):
    fake = FakeLLM(default=by_sender({"小助": "@文案 写", "文案": "写好了"}))
    orch, g, ag = setup(store, make_router, fake)
    await orch.handle_user_message(g["id"], "来个通知", Collector())
    msgs = next(m for _, m in fake.calls if m[0]["content"].startswith("你是「文案」"))
    roles = [m["role"] for m in msgs]
    assert roles[0] == "system" and roles[1] == "user"
    assert all(a != b for a, b in zip(roles[1:], roles[2:]))  # 严格交替
    assert "[我] 来个通知" in msgs[1]["content"] and "[小助]" in msgs[1]["content"]
    assert msgs[-1]["content"].rstrip().endswith("(现在轮到你「文案」发言)")


async def test_skill_injected_into_system_prompt(store, make_router):
    from app.tools import ensure_example_skills

    ensure_example_skills(store.data_dir / "skills")
    fake = FakeLLM(default="ok")
    orch, g, ag = setup(store, make_router, fake)
    await orch.handle_user_message(g["id"], "@文案 写", Collector())
    sys = fake.calls[0][1][0]["content"]
    assert "【技能:公文写作规范】" in sys and "开头一句话交代目的和结论" in sys


async def test_all_models_down_emits_system_notice_not_crash(store, make_router):
    fake = FakeLLM(default=RuntimeError("down"))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "hi", c)
    types = [e["type"] for e in c.events]
    assert "message_discard" in types
    last = store.list_messages(g["id"])[-1]
    assert last["sender_type"] == "system" and "暂时无法回复" in last["content"]


async def test_fallback_is_recorded_on_message(store, make_router):
    fake = FakeLLM({"deepseek/": RuntimeError("timeout")}, default="本地回复")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "hi", c)
    m = c.ends()[0]
    assert m["model_id"] == "ollama/qwen2.5:7b" and m["fallback_from"] == "deepseek/deepseek-flash"


async def test_cancel_discards_streaming_message_and_saves_nothing(store, make_router):
    import asyncio

    from tests.conftest import chunk

    started = asyncio.Event()

    async def slow(**kw):
        async def gen():
            yield chunk("开头")
            started.set()
            await asyncio.sleep(30)
            yield chunk("永远到不了")

        return gen()

    orch, g, ag = setup(store, make_router, slow)
    c = Collector()
    task = asyncio.create_task(orch.handle_user_message(g["id"], "@文案 写", c))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(e["type"] == "message_discard" for e in c.events)
    assert not any(m["sender_type"] == "agent" for m in store.list_messages(g["id"]))
