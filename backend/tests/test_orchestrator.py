from app.orchestrator import Orchestrator, find_mentions
from tests.conftest import TURN_NOW, FakeLLM, has


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
    """按 system prompt 里的名字返回不同回复(中英两种内置提示词都认)。"""

    def pick(messages):
        head = messages[0]["content"][:90]
        for name, reply in fake_reply_map.items():
            if f"「{name}」" in head or f'"{name}"' in head:
                return reply
        return "好的"

    return pick


def test_find_mentions_longest_first_and_order():
    members = [{"id": "1", "name": "Copywriter"}, {"id": "2", "name": "文案组"}, {"id": "3", "name": "Proofreader"}]
    got = find_mentions("请 @Proofreader 和 @文案组 看看", members)
    assert [m["name"] for m in got] == ["Proofreader", "文案组"]
    assert find_mentions("@文案组", members)[0]["name"] == "文案组"
    assert len(find_mentions("@文案组", members)) == 1


async def test_no_mention_goes_to_host(store, make_router):
    fake = FakeLLM(default="收到")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "帮我写个发布会通知", c)
    assert [m["sender_name"] for m in c.ends()] == ["Aide"]


async def test_handoff_chain_via_at_mentions(store, make_router):
    fake = FakeLLM(default=by_sender({
        "Aide": "拆解完毕。@Copywriter 请写初稿。",
        "Copywriter": "初稿如下……@Proofreader 请审校。",
        "Proofreader": "已审校,无问题。",
    }))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "写一篇通知", c)
    assert [m["sender_name"] for m in c.ends()] == ["Aide", "Copywriter", "Proofreader"]
    # 消息与模型信息已落库
    saved = store.list_messages(g["id"])
    assert [m["sender_name"] for m in saved][-3:] == ["Aide", "Copywriter", "Proofreader"]
    assert saved[-1]["model_id"] == "deepseek/deepseek-flash"


async def test_explicit_mention_skips_host(store, make_router):
    fake = FakeLLM(default="行")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@Storyboard 给我一个 15 秒的分镜", c)
    assert [m["sender_name"] for m in c.ends()] == ["Storyboard"]


async def test_at_all_fans_out_to_everyone(store, make_router):
    fake = FakeLLM(default="到")
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@所有人 自我介绍", c)
    assert [m["sender_name"] for m in c.ends()] == ["Aide", "Copywriter", "Storyboard", "Proofreader"]


async def test_ping_pong_is_capped_by_max_hops(store, make_router):
    store.update_settings({"max_hops": 5})
    fake = FakeLLM(default=by_sender({"Copywriter": "@Proofreader 你看看", "Proofreader": "@Copywriter 你再改改"}))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@Copywriter 开始", c)
    assert len(c.ends()) == 5
    last = store.list_messages(g["id"])[-1]
    assert last["sender_type"] == "system" and "Reached the limit of" in last["content"]


async def test_self_mention_ignored(store, make_router):
    fake = FakeLLM(default=by_sender({"Copywriter": "我 @Copywriter 自己搞定"}))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "@Copywriter 写", c)
    assert len(c.ends()) == 1


async def test_context_labels_speakers_and_merges_roles(store, make_router):
    fake = FakeLLM(default=by_sender({"Aide": "@Copywriter 写", "Copywriter": "写好了"}))
    orch, g, ag = setup(store, make_router, fake)
    await orch.handle_user_message(g["id"], "来个通知", Collector())
    msgs = next(m for _, m in fake.calls if "Copywriter" in m[0]["content"][:90])
    roles = [m["role"] for m in msgs]
    assert roles[0] == "system" and roles[1] == "user"
    assert all(a != b for a, b in zip(roles[1:], roles[2:]))  # 严格交替
    assert "[me] 来个通知" in msgs[1]["content"] and "[Aide]" in msgs[1]["content"]
    assert has(msgs[-1]["content"].rstrip(), TURN_NOW) and "Copywriter" in msgs[-1]["content"]


async def test_skill_injected_into_system_prompt(store, make_router):
    from app.tools import ensure_example_skills

    ensure_example_skills(store.data_dir / "skills")
    fake = FakeLLM(default="ok")
    orch, g, ag = setup(store, make_router, fake)
    await orch.handle_user_message(g["id"], "@Copywriter 写", Collector())
    sys = fake.calls[0][1][0]["content"]
    assert "[Skill: Office writing conventions]" in sys
    assert "Open with one sentence giving the purpose and the conclusion" in sys

    # 中文界面下,同一个技能以中文名与中文正文注入
    from app import i18n
    from app.tools import skills_prompt
    i18n.set_current("zh")
    try:
        zh = skills_prompt(store.data_dir / "skills", ["Office writing conventions"])
    finally:
        i18n.set_current("en")
    assert "【技能:公文写作规范】" in zh and "开头一句话交代目的和结论" in zh


async def test_all_models_down_emits_system_notice_not_crash(store, make_router):
    fake = FakeLLM(default=RuntimeError("down"))
    orch, g, ag = setup(store, make_router, fake)
    c = Collector()
    await orch.handle_user_message(g["id"], "hi", c)
    types = [e["type"] for e in c.events]
    assert "message_discard" in types
    last = store.list_messages(g["id"])[-1]
    assert last["sender_type"] == "system" and "cannot reply right now" in last["content"]


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
    task = asyncio.create_task(orch.handle_user_message(g["id"], "@Copywriter 写", c))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(e["type"] == "message_discard" for e in c.events)
    assert not any(m["sender_type"] == "agent" for m in store.list_messages(g["id"]))
