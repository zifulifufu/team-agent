import asyncio

from app import templates
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
    """Answer differently depending on the name in the system prompt (recognises both
    the Chinese and the English built-in phrasing).
"""

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
    # messages and their model info are persisted
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
    assert all(a != b for a, b in zip(roles[1:], roles[2:]))  # strictly alternating
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

    # under a Chinese interface the same skill is injected with its Chinese name and body
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


async def test_the_notice_names_the_real_reason_when_the_chain_is_the_problem(store, make_router):
    """The exact situation that produces a mute group with a working key.

    A model has to be named in the priority chain to be used at all, so a chain full of ids that no
    longer exist leaves nothing to call **while every credential is fine**. The old notice said
    "no model is available" and listed only the dead ids, which sends the reader off to re-check
    keys that already work. Naming what is usable-but-unchained is the actionable half.

    The host is unpinned and untagged first, because both of those are *other* ways in: a member
    with a model of its own goes to the front of the chain, and one with tags is ranked against
    every usable model — neither consults the chain.
    """
    orch, g, _agents = setup(store, make_router, FakeLLM(default="unused"))
    store.update_agent(g["host_agent_id"], {"model_id": "", "tags": []})
    store.update_settings({"route_chain": ["deepseek/deepseek-chat"]})   # renamed since; not a model any more
    store.update_provider("ollama", {"enabled": False})                   # no local safety net
    c = Collector()

    await orch.handle_user_message(g["id"], "hi", c)

    last = store.list_messages(g["id"])[-1]
    assert last["sender_type"] == "system"
    assert "cannot reply right now" in last["content"]
    assert "deepseek/deepseek-chat" in last["content"], "the dead chain entry is still reported"
    assert "none of them is in the priority chain" in last["content"]
    assert "deepseek-flash" in last["content"], "the models that *could* answer are named"
    assert "Routing & fallback" in last["content"], "and where to fix it"


async def test_the_notice_stays_plain_when_nothing_is_usable(store, make_router):
    """No hint when there is nothing to hint about: the plain sentence is already the right advice."""
    orch, g, _agents = setup(store, make_router, FakeLLM(default="unused"))
    store.update_agent(g["host_agent_id"], {"model_id": "", "tags": []})
    store.update_settings({"route_chain": []})
    store.update_provider("deepseek", {"api_key": ""})                    # setup() adds one
    store.update_provider("ollama", {"enabled": False})
    c = Collector()

    await orch.handle_user_message(g["id"], "hi", c)

    last = store.list_messages(g["id"])[-1]
    assert "cannot reply right now" in last["content"]
    assert "priority chain" not in last["content"]


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


# ------------------------------------------------- a member that keeps failing
def test_a_member_that_keeps_failing_is_told_how_many_times_and_that_it_can_be_taken_out(
        store, make_router):
    """One failure is noise; twelve is a workstream that never happened.

    In one session a member failed twelve times — with the audio and assembly work assigned to it — and
    the app said the same "could not reply" every time, as if it were the first. A member that keeps
    failing is occupying a slot in every round, and the two real fixes are to repair it or to take it
    out of the group; the second one lives on the member's card, so the failure note says so.
    """
    orch, g, agents = setup(store, make_router, FakeLLM(default="好"))
    victim = agents["Aide"]
    other = agents["Librarian"] if "Librarian" in agents else list(agents.values())[1]

    def note_saying(name: str, times: int) -> None:
        for _ in range(times):
            store.add_message(g["id"], "system", None, "系统",
                              f"「{name}」没能回复:命令行引擎报告了错误。")

    # Nothing yet, and one failure, are both just noise.
    assert orch._repeat_failure_note(g["id"], victim) == ""
    note_saying(victim["name"], 1)
    assert orch._repeat_failure_note(g["id"], victim) == ""

    # Failures of *other* members do not inflate this one's count.
    note_saying(other["name"], 5)
    assert orch._repeat_failure_note(g["id"], victim) == ""

    # Two prior failures → the next one is the third, and that is a pattern worth saying out loud.
    note_saying(victim["name"], 1)
    note = orch._repeat_failure_note(g["id"], victim)
    assert "3" in note
    assert "移出本群" in note or "take it out of this group" in note


# ------------------------------------------------------------- seeing how it got there
async def test_a_reasoning_models_working_is_streamed_and_kept(store, make_router):
    """The reasoning stream is the model's working, and it is most of what lets a reader judge the
    answer: it belongs on screen while it arrives, above the reply, and with the message afterwards.
    It used to be read only to tell "reasoned and never answered" apart from "returned nothing".
    """
    working = "先看用户要什么,再决定怎么答。" * 6
    answer = "答案是 42。"
    orch, g, _ = setup(store, make_router, FakeLLM(default=("reasoning", working, answer)))
    c = Collector()
    await orch.handle_user_message(g["id"], "算一下", c)
    await orch.drain()

    live = [e for e in c.events if e["type"] == "thinking"]
    assert live, "the working never reached the chat"
    assert "".join(e["text"] for e in live).startswith("先看用户要什么")
    # …and it is not part of the answer: the two are separate things on screen.
    streamed = "".join(e["text"] for e in c.events if e["type"] == "delta")
    assert answer in streamed and "先看用户要什么" not in streamed

    end = c.ends()[-1]
    assert end["content"].endswith(answer)
    assert "先看用户要什么" in end["meta"]["thinking"]


async def test_a_fallback_clears_the_working_of_the_model_that_failed(store, make_router):
    """The working on screen belongs to the model that produced it. When a stream fails and another
    model takes over, leaving the first one's reasoning up would attribute it to the answer that
    actually arrives.
    """
    from tests.conftest import chunk

    class Halfway:
        """A reasoning model that dies after thinking, before answering."""

        def __init__(self):
            self.calls = 0

        async def __call__(self, **kw):
            self.calls += 1
            if self.calls == 1:
                async def dying():
                    yield chunk("", "我是第一个模型的思考")
                    raise ConnectionError("stream broke")
                return dying()

            async def second():
                yield chunk("换了个模型,答案在这里。")
            return second()

    orch = Orchestrator(store, make_router(Halfway()))
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.update_settings({"route_chain": ["deepseek/deepseek-flash", "ollama/qwen2.5:7b"]})
    g = store.list_groups()[0]
    c = Collector()
    await orch.handle_user_message(g["id"], "写点什么", c)
    await orch.drain()

    think = "".join(e["text"] for e in c.events if e["type"] == "thinking")
    assert "我是第一个模型的思考" not in think, "the failed attempt's working was left on screen"
    assert any(e["type"] == "reset" for e in c.events), "the failed attempt should have been cleared"
    assert "换了个模型" in c.ends()[-1]["content"]


async def test_a_model_that_does_not_reason_gets_no_thinking_block(store, make_router):
    """The block is for a model that has working to show, not a decoration every reply grows."""
    orch, g, _ = setup(store, make_router, FakeLLM(default="就这么答。"))
    c = Collector()
    await orch.handle_user_message(g["id"], "你好", c)
    await orch.drain()

    assert not [e for e in c.events if e["type"] == "thinking"]
    assert "thinking" not in c.ends()[-1]["meta"]


# ------------------------------------------------- one message must fit one round of the group
def _paused(collector) -> list[str]:
    """The "reached the limit" notes the round posted.

    Read from both event shapes on purpose — a system note is emitted as a `message`, while a member's
    turns end as `message_end`, and a test that looks in only one of them sees "no note" when the
    note is right there.
    """
    return [str((e.get("message") or {}).get("content") or "") for e in collector.events
            if e.get("type") in ("message", "message_end")
            and "paused" in str((e.get("message") or {}).get("content") or "")]


def _ring(store, g, names=("Analyst", "Librarian", "Fact-checker")):
    """A group whose members keep @-ing each other, so the queue never empties on its own."""
    for name in names:
        made = templates.ensure_agent(store, name)
        assert made, name
        store.add_member(g["id"], made["id"])
    roster = [m["name"] for m in store.group_members(g["id"])]
    turns = {"i": 0}

    def reply(messages):                      # every speaker hands over to the next name
        turns["i"] += 1
        nxt = roster[(turns["i"]) % len(roster)]
        return f"说完了,@{nxt} 接着来。"

    return roster, FakeLLM(default=reply)


def test_a_turn_limit_smaller_than_the_group_cannot_silence_the_end_of_the_list(store, make_router):
    """A real fourteen-member group at the default limit of 8 paused members *every* round —
    "reached the limit of 8 turns, so WorkBuddy, 小助 was paused" — and the steps they owned could
    never be reached, however many messages were sent. A limit that cannot fit one round is not a
    limit, it is a truncation: the members at the end of the list are never heard from at all.

    The limit here is 2 on a four-member group, and the members talk to each other in a ring, so the
    round cannot end by itself: exactly one full round runs (`max(2, 4 + 1) = 5` turns) and then the
    cap stops it. That number — 5, not 2 and not Infinity — is the whole of the fix.
    """
    st = store
    orch, g, _ = setup(st, make_router, FakeLLM(default="好。"))
    roster, fake = _ring(st, g)
    orch = Orchestrator(st, make_router(fake))
    st.update_settings({"max_hops": 2, "plan_mode": "off"})

    c = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "大家依次说说。", c))
    asyncio.run(orch.drain())

    said = [m["sender_name"] for m in c.ends() if m.get("sender_type") == "agent"]
    notes = _paused(c)
    cap = max(2, len(roster) + 1)
    # One full round runs and then the cap stops it — `cap`, not 2 and not Infinity.
    assert len(said) == cap, (len(said), cap, roster)
    assert set(roster) <= set(said), "every member of the group spoke, including the end of the list"
    assert notes and f"{cap} turns" in notes[0], notes     # and the limit that was actually applied


def test_a_limit_bigger_than_the_group_is_still_the_limit(store, make_router):
    """The floor is a floor, not a ceiling: a user who asks for twelve turns gets twelve, so the fix
    for "the end of the list is never heard from" cannot quietly remove the guard against an endless
    @-loop."""
    st = store
    orch, g, _ = setup(st, make_router, FakeLLM(default="好。"))
    roster, fake = _ring(st, g)
    orch = Orchestrator(st, make_router(fake))
    st.update_settings({"max_hops": 12, "plan_mode": "off"})

    c = Collector()
    asyncio.run(orch.handle_user_message(g["id"], "大家依次说说。", c))
    asyncio.run(orch.drain())

    said = [m["sender_name"] for m in c.ends() if m.get("sender_type") == "agent"]
    assert len(said) == 12, len(said)
    assert _paused(c), "the ring is still talking, so the cap has to stop it"


async def test_a_local_tool_member_listens_but_never_speaks_unasked(store, make_router):
    """本机工具成员在**旁听席**上:不主动发言,但叫得动。

    用户 2026-09-25:「工具可以不发言,作为旁听,但是群员可以随时调用这些工具的」。
    一个渲染器没有话要说,它只产出文件 —— 让它进对话队列等于每轮请一个只会交文件的成员讲一段话,
    而它十条里有九条在报「我跑不起来」(实测 Remotion/HyperFrames/video-shotcraft 每轮必败,
    其中一次跑了 306 秒)。

    ⚠️ 反过来的那一半同样重要(所以下面的断言成对写):
      * 没点名 → **不给它回合**;
      * 点了名 → 它有回合(引擎没装好会失败,但「轮到过它」必须验到);
      * 它**不是 blocked** —— 被排进任务板照跑,否则「随时可用」就成了「永远用不上」。
    """
    fake = FakeLLM(default="好的")
    # ⚠️ 本机工具成员也是「外部程序」,受同一个总开关管:开关关着时它们**连点名都跑不了**
    # (这条也是实测出来的 —— 第一版测试没开这个开关,于是 `_blocked_members` 把它整个挡掉了)。
    store.update_settings({"external_agents_enabled": True})
    orch, g, _ag = setup(store, make_router, fake)
    tool = store.create_agent("Remotion", engine="remotion", engine_cfg={})
    store.add_member(g["id"], tool["id"], 9)

    members = store.group_members(g["id"])
    assert tool["id"] in orch._quiet_members(members), "本机工具成员应当坐在旁听席上"
    assert tool["id"] not in orch._blocked_members(members, store.get_settings()), \
        "旁听 != 被封:它被排进任务板时要能跑"

    c = Collector()
    await orch.handle_user_message(g["id"], "大家好,聊两句", c)
    assert "Remotion" not in [m["sender_name"] for m in c.ends()], \
        "没点名时它不该发言:" + str([m["sender_name"] for m in c.ends()])

    # 点名 → 出列。引擎没装好,所以这一轮会失败 —— 但**失败也是回合**,系统消息里会出现它的名字。
    c2 = Collector()
    await orch.handle_user_message(g["id"], "@Remotion 渲染一个 15 秒的片子", c2)
    said = " ".join(str(e.get("text") or "") + str((e.get("message") or {}).get("content") or "")
                    for e in c2.events)
    assert "Remotion" in said, "点了名就该轮到它(哪怕结果是报错):" + said[:400]
