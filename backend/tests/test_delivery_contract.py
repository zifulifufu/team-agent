"""The delivery contract: a task is done when something was delivered.

Until this rule existed, `_execute_plan` marked a task `done` the moment its owner replied — so a
turn whose every tool call failed, or one that was supposed to write a file and wrote none, was handed
downstream and reported to the user as finished. These tests pin the three shapes:

1. every tool call of the turn failed → the task failed, with the calls named;
2. the deliverable names a file and the turn made no successful tool call at all → the file cannot
   exist, so the task failed (a member that used *some* tool successfully is trusted about the name:
   `报告-v2.docx` for a plan that said `报告.docx` must not be a failure);
3. a member that cannot take a turn at all (an external agent with no usable configuration) is taken
   out of the round *before* it is handed one, and the round says why once.
"""

from __future__ import annotations

import pathlib

from app import planner
from app.orchestrator import Orchestrator


def task(deliverable: str = "") -> planner.PlanTask:
    return planner.PlanTask(id="t1", owner="Copywriter", owner_id="a1", title="写稿",
                            instruction="写", deliverable=deliverable)


def msg(tools: list[dict]) -> dict:
    return {"meta": {"tools": tools}}


# ------------------------------------------------------------------ _task_shortfall
def test_every_tool_call_failing_is_not_a_delivery(tmp_path):
    orch = Orchestrator.__new__(Orchestrator)          # the method touches no instance state
    calls = [{"name": "generate_image", "status": "failed"}, {"name": "generate_image", "status": "failed"}]

    why = orch._task_shortfall(task(), msg(calls), tmp_path)

    assert "generate_image(failed)" in why
    assert "全都失败" in why or "every tool call failed" in why


def test_a_named_deliverable_with_no_successful_call_is_a_failure(tmp_path):
    orch = Orchestrator.__new__(Orchestrator)
    calls = [{"name": "write_document", "status": "denied"}]

    why = orch._task_shortfall(task("交付/报告.docx"), msg(calls), tmp_path)

    assert "报告.docx" in why
    assert "落盘" in why or "never written" in why


def test_a_reply_with_no_tools_is_still_a_normal_writing_task(tmp_path):
    """Most tasks are prose: a member that wrote a review, a decision or a script has no tool to call,
    and calling that a failure would break every honest text task."""
    orch = Orchestrator.__new__(Orchestrator)

    assert orch._task_shortfall(task("一段评审意见"), msg([]), tmp_path) == ""
    assert orch._task_shortfall(task(""), msg([]), tmp_path) == ""


def test_some_successful_call_means_the_name_is_not_second_guessed(tmp_path):
    """A member may deliver `报告-v2.docx` while the plan said `报告.docx`. Failing that would be worse
    than the thing this check is for."""
    orch = Orchestrator.__new__(Orchestrator)
    calls = [{"name": "write_document", "status": "ok", "files": [{"name": "交付/报告-v2.docx"}]}]
    (tmp_path / "交付").mkdir()
    (tmp_path / "交付/报告-v2.docx").write_bytes(b"real output")

    assert orch._task_shortfall(task("交付/报告.docx"), msg(calls), tmp_path) == ""


def test_a_skipped_call_alone_is_not_a_delivery_either(tmp_path):
    orch = Orchestrator.__new__(Orchestrator)

    assert orch._task_shortfall(task("报告.docx"), msg([{"name": "write_document", "status": "denied"}]), tmp_path)


def test_delivery_retry_keeps_existing_audio_and_failed_review_evidence(tmp_path):
    import wave
    from app.orchestrator import _delivery_tools

    with wave.open(str(tmp_path / "narration.wav"), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x01" * 24000)
    (tmp_path / "status.md").write_text("recorded")
    previous = [{"name": "synthesize_speech", "status": "ok", "files": [{"name": "narration.wav"}]},
                {"name": "review_audio", "status": "failed", "args": {"path": "narration.wav"}}]
    current = [{"name": "write_document", "status": "ok", "files": [{"name": "status.md"}]}]
    message = {"meta": {"delivery_previous_tools": previous, "tools": current}}
    orch = Orchestrator.__new__(Orchestrator)
    t = task("narration.wav + status.md")
    t.tools = ["review_audio"]
    assert orch._task_shortfall(t, message, tmp_path)  # writing status cannot erase the failed review
    current.append({"name": "review_audio", "status": "ok", "args": {"path": "narration.wav"}})
    assert orch._task_shortfall(t, message, tmp_path) == ""
    assert [f["name"] for c in _delivery_tools(message) for f in c.get("files", [])] == ["narration.wav", "status.md"]


# ------------------------------------------------------------------ who may be given a turn
def member(**kw) -> dict:
    return {"id": "m1", "name": "WorkBuddy", "role": "External agent", **kw}


def test_an_external_member_with_the_switch_off_is_taken_out_of_the_round():
    orch = Orchestrator.__new__(Orchestrator)
    cfg = {"external_agents_enabled": False}

    blocked = orch._blocked_members([member(engine="workbuddy"), {"id": "m2", "name": "Aide"}], cfg)

    assert list(blocked) == ["m1"]
    assert "master switch" in blocked["m1"]


def test_a_member_whose_config_does_not_comply_is_taken_out_with_the_reason():
    orch = Orchestrator.__new__(Orchestrator)
    cfg = {"external_agents_enabled": True}
    # Full permissions without the acknowledgement: the shape a hand-edited database or a restored
    # backup can bring in. `clean_cfg` rejects it, so the member would fail identically every round.
    bad = member(engine="workbuddy", engine_cfg={"level": "full", "risk_ack": False})

    blocked = orch._blocked_members([bad], cfg)

    assert "not valid" in blocked["m1"]
    assert "不合规" in blocked["m1"] or "not valid" in blocked["m1"]


def test_a_plain_member_is_never_blocked_by_this_check():
    """Only *configuration* problems are caught here — a model that times out still gets its turn."""
    orch = Orchestrator.__new__(Orchestrator)

    assert orch._blocked_members([{"id": "m1", "name": "Aide"}, {"id": "m2", "name": "文案"}],
                                 {"external_agents_enabled": False}) == {}


# ------------------------------------------------------------------ the board and the host's answer
def test_the_task_board_carries_the_files_a_task_produced():
    t = task("交付/报告.docx")
    t.status = "done"
    t.files = ["交付/报告.docx"]

    board = t.to_dict()

    assert board["files"] == ["交付/报告.docx"]
    assert board["deliverable"] == "交付/报告.docx"


def test_the_host_is_told_to_list_the_files_and_not_to_pass_off_a_failed_task():
    plan = planner.Plan(goal="出一份报告", conventions="", tasks=[task("交付/报告.docx")])
    plan.tasks[0].status = "failed"
    plan.tasks[0].files = []

    text = planner.integration_prompt(plan, {})

    # The wording follows the request language, so the assertions are on what the instruction has to
    # contain rather than on one spelling of it.
    assert ("List the files" in text) or ("把这一轮真正产出的文件列出来" in text)
    assert ("not among them" in text) or ("那就是没交付" in text)
    assert "报告.docx" not in text or "没有完成" in text


def test_the_task_prompt_says_the_file_is_the_deliverable():
    plan = planner.Plan(goal="出一份报告", conventions="", tasks=[task("交付/报告.docx")])

    text = planner.task_prompt(plan, plan.tasks[0], {}, 1)

    assert "write_document" in text
    assert "交付/报告.docx" in text


# ---------------------------------------------------------------- 声明了但没落盘：核验结论要盖在声明上
async def test_a_claim_of_delivery_is_stamped_when_no_tool_ever_ran(store, make_router):
    """「已落盘」写在聊天里，而程序核验说没有 —— 这两句必须待在**同一条消息**里。

    2026-09-26（群 介入术式科）：Project manager 连两轮**零工具调用**，却在正文里写
    「## t1 真实交付：视觉规范 v2.3 已落盘」。文件从不存在；任务板上写着 `t1 [failed]`，
    而群主在**下一轮规划**时读到的是那段自信、详细的正文，于是把「视觉规范已落盘」当成既成事实
    往下排。声明与结论在不同消息里，看起来像答案的只有声明那一条。

    所以结论要盖在声明上：前置（历史截断也丢不掉）、存库（下一轮规划读得到）、发出去（用户也看得到）。
    """
    from app.orchestrator import RunState
    from tests.conftest import FakeLLM
    from tests.test_collab import Collector, setup

    claim = "## t1 真实交付：视觉规范 v2.3 已落盘\n**交付物**：`tasks/x/视觉规范-v2.3.md`"
    orch, g = setup(store, make_router, FakeLLM(default=claim))
    group = store.get_group(g["id"])
    host = store.get_agent(group["host_agent_id"])
    plan = planner.build_plan({"tasks": [{"id": "t1", "owner": host["name"], "instruction": "落盘视觉规范",
                                          "tools": ["write_document"],
                                          "deliverable": "tasks/x/视觉规范-v2.3.md"}]},
                              [host], known_tools={"write_document"},
                              member_tools={host["id"]: {"write_document"}})

    events: list[dict] = []

    class Collector2(Collector):
        """A Collector that also remembers what was emitted, so the rewritten message can be seen."""

        async def __call__(self, event):
            events.append(event)
            return await super().__call__(event)

    real_turn = orch._agent_turn
    prompts: list[tuple[str, str]] = []

    async def spy(group_, agent, members, emit, run, **kw):
        prompts.append(((kw.get("extra_meta") or {}).get("task_id", ""), kw.get("extra_user") or ""))
        return await real_turn(group_, agent, members, emit, run, **kw)

    orch._agent_turn = spy                              # type: ignore[assignment]
    await orch._execute_plan(group, [host], host, plan, RunState(group["id"], "synthetic"), Collector2())

    t = plan.tasks[0]
    assert t.status == "failed" and "视觉规范-v2.3.md" in t.error, t.error
    stored = store.get_message(t.message_id)
    assert stored["meta"]["delivery_verdict"] == t.error, "核验结论要存进 meta,幂等靠它而不是靠文案"
    body = stored["content"]
    assert body.startswith("⚠️"), body[:80]
    assert "程序核验" in body or "Verified by the program" in body, body[:200]
    assert "工具调用 0 次" in body or "Tool calls this turn: 0" in body, body[:200]
    assert claim in body, "原话必须保留 —— 要让人看到声明与结论摆在一起"
    assert body.index("⚠️") < body.index("已落盘"), "结论要在前,历史截断才丢不掉"
    # 界面上也要换掉那条消息,否则用户看到的还是那句谎话
    assert any(e.get("type") == "message_end" and e["message"]["id"] == t.message_id for e in events)
    # ⚠️ 关键的一半：整合回合（群主）拿到的必须是**盖过章**的文本,否则同一个谎话会从"下游"再进一次
    final = [p for tid, p in prompts if tid == "final"]
    assert final and ("程序核验" in final[0] or "Verified by the program" in final[0]), final[:1]
    # ⚠️ 重判那条已盖章的消息必须得到同一个结论 —— 盖章的正文里含那个文件名,而缺口检查有一个分支
    # 会从正文里抠文件名,不挡住的话结论会反过来替声明作证。
    assert orch._task_shortfall(t, stored, store.workspace_dir(group["id"])) == t.error


async def test_a_silent_member_is_told_it_called_nothing(store, make_router):
    """重试回合不能说「沿用上一轮已整理的内容」—— 那一轮什么都没做，那是在鼓励它保留谎话。

    实测同一轮：重试把成员自己那段话原样喂回去、标签写着「沿用，不要重新检索」，而工具证据是
    一个空列表（`[]`）。**空列表不是一句话**，模型从里面读不出「你一次工具都没调用」。
    """
    from app.orchestrator import RunState, TurnOut
    from tests.conftest import FakeLLM
    from tests.test_collab import Collector, setup

    orch, g = setup(store, make_router, FakeLLM(default="已落盘。"))
    group = store.get_group(g["id"])
    host = store.get_agent(group["host_agent_id"])
    plan = planner.Plan(goal="g", conventions="", tasks=[
        planner.PlanTask("t1", host["name"], host["id"], "落盘", "写文件", tools=["write_document"],
                         deliverable="tasks/x/视觉规范.md")])
    prompts: list[str] = []

    async def turn(group_, agent, members, emit, run, **kw):
        prompts.append(kw.get("extra_user") or "")
        msg = store.add_message(group_["id"], "agent", agent["id"], agent["name"], "已落盘。")
        return TurnOut("已落盘。", "已落盘。", msg)

    orch._agent_turn = turn                              # type: ignore[assignment]
    await orch._execute_plan(group, [host], host, plan, RunState(group["id"], "synthetic"), Collector())

    retry = [p for p in prompts if "delivery" not in p and "Previous" in p] or prompts[1:]
    assert len(prompts) >= 2, "交付缺口应当触发一次重试"
    again = prompts[1]
    assert ("一次工具调用都没有发生" in again or "made no tool calls at all" in again), again[:400]
    assert "沿用" not in again and "do not search again" not in again, "不能把空产出说成已整理内容"
    assert "未落盘" in again or "nothing was written to disk" in again, again[:400]
    _ = retry


# ---------------------------------------------------------------- one judgement, or none
def test_one_file_name_judgement_for_the_validator_the_executor_and_the_ledger():
    """⚠️ Four copies of "is this a file name" had grown apart, and they disagreed both ways.

    Measured 2026-09-26, three silent consequences:

    * `out/animation.json`, `notes.tex`, `video/字幕轨.html` matched nothing in the executor's own
      27-extension list — and that list did not merely word an error, it decided **whether delivery
      was verified at all**. Those tasks were left `done` with nothing on disk.
    * `voice/旁白.wav` and `art/clip.mkv` matched nothing in the ledger's 12-extension list, so the
      process engineer's "promised and not delivered" count could never see an audio or video
      handover go missing — the work this group actually does.
    * `video/x.srt` matched nothing in the validator's 16-extension list, so a **production** task
      whose deliverable plainly named a file was refused with "you must name your output file".
      `.srt` is one of the two files `assemble_video` itself emits, beside the film.
    """
    from app import proclog

    # One function answers it, and the other two ask that function rather than restating it.
    assert planner.named_files("交付/综述.md") == ["综述.md"]          # the name, not the path
    for text, want in (("out/animation.json", "animation.json"),
                       ("notes.tex", "notes.tex"),
                       ("video/颅内动脉瘤介入治疗科普-300s-字幕轨.html", "颅内动脉瘤介入治疗科普-300s-字幕轨.html"),
                       ("voice/旁白.wav", "旁白.wav"),
                       ("art/clip.mkv", "clip.mkv"),
                       ("video/x.srt", "x.srt")):
        assert planner.named_files(text) == [want], (text, planner.named_files(text))
    # …and prose is still prose: a version number is not a suffix, and neither is an integer
    for prose in ("分镜表 v2.3", "一份 3000 字综述", "共 74 镜", "第 3.5 节"):
        assert planner.named_files(prose) == [], (prose, planner.named_files(prose))
    # Two deliverables written with a separator between them are **two** files. Read as one blob the
    # ledger reported the pair as a single missing file — a false "promised and not delivered" entry
    # for a task that had delivered both.
    assert planner.named_files("交付/综述.md、交付/综述.docx") == ["综述.md", "综述.docx"]
    assert planner.suffixes("交付/综述.md、交付/综述.docx") == {"md", "docx"}
    # ⚠️ A name may keep its spaces and CJK, because the ledger compares names whole
    assert planner.named_files("Final Report.docx") == ["Final Report.docx"]
    assert planner.named_files("中秋散文（配图）.docx") == ["中秋散文（配图）.docx"]
    # ⚠️ Known limit, written down rather than pretended away: a run of CJK with no punctuation
    # stays glued to the name, because deciding that 与 / 先 is not part of a file name needs a word
    # list. Two consequences worth knowing: such a name will not match a delivered base name
    # (`proclog` compares whole names), and the same file mentioned twice reads as two entries.
    # Punctuation is what separates them — the case that actually occurs in a `deliverable` field.
    assert planner.named_files("见 a.md 与 b.png") == ["见 a.md 与 b.png"]
    assert planner.named_files("先 a.md，又 a.md") == ["先 a.md", "又 a.md"]
    # …and the same name twice is one entry, in the order it was first mentioned
    assert planner.named_files("a.md、b.png、a.md") == ["a.md", "b.png"]
    assert planner.suffixes("out/x.py;y.JSON") == {"py", "json"}

    from app.orchestrator import _FILE_IN_PLAN
    assert _FILE_IN_PLAN is planner.named_files, "执行侧必须问同一个函数,不能再有一份名单"
    assert proclog.FILE_IN_TEXT is planner.named_files, "账本也必须问同一个函数"


def test_a_deliverable_outside_the_old_list_is_still_verified(store, make_router):
    """The fail-open half: a task delivering `.json` used to skip verification **entirely**.

    `_execute_plan` only ran the shortfall check when the deliverable matched its own list of 27
    extensions. Anything else — `.json`, `.tex`, `.html`, `.zip` — took the branch that marks the
    task `done` without looking at the workspace. That is this project's most expensive failure
    shape: the board says finished and the file does not exist. `-字幕轨.html` was exactly the
    deliverable this group planned for the HyperFrames chain.
    """
    import asyncio

    from app.orchestrator import RunState, TurnOut
    from tests.conftest import FakeLLM
    from tests.test_collab import Collector, setup

    orch, g = setup(store, make_router, FakeLLM(default="已写好 animation.json。"))
    group = store.get_group(g["id"])
    host = store.get_agent(group["host_agent_id"])
    task = planner.PlanTask("t1", host["name"], host["id"], "做动画", "把动画参数写成文件",
                            tools=["library_read"], deliverable="out/animation.json")
    plan = planner.Plan(goal="g", conventions="", tasks=[task])

    async def turn(group_, agent, members, emit, run, **kw):
        msg = store.add_message(group_["id"], "agent", agent["id"], agent["name"], "已写好 animation.json。")
        return TurnOut("已写好 animation.json。", "已写好 animation.json。", msg)

    orch._agent_turn = turn                              # type: ignore[assignment]
    asyncio.run(orch._execute_plan(group, [host], host, plan, RunState(group["id"], "synthetic"),
                                   Collector()))
    assert task.status == "failed", "盘上没有这个文件,任务不能算完成"
    assert "animation.json" in task.error, task.error
    stamped = store.get_message(task.message_id)
    assert stamped["meta"]["delivery_verdict"], "核验结论要存进 meta"


def test_a_production_task_may_promise_the_subtitle_file_its_tool_emits(store, make_router):
    """The fail-closed half: `video/x.srt` used to be refused as "you have not named a file".

    The old test was "does it carry one of 16 known extensions" and `.srt` was not among them — so
    the host, planning the subtitle track that `assemble_video` emits next to every film, was told
    its deliverable did not name a file and could only re-send the same plan.
    """
    members = [{"id": "pm", "name": "Project manager"}]
    for deliverable in ("video/x.srt", "video/x.mp4", "out/fig.png", "交付/综述.md"):
        planner.build_plan(
            {"tasks": [{"id": "t1", "owner": "Project manager", "instruction": "做",
                        "tools": ["assemble_video"], "deliverable": deliverable}]},
            members, member_tools={"pm": {"assemble_video"}})
    # a name with no extension at all is still refused, and so is a description of a result
    import pytest
    for prose in ("一份字幕轨", "完成字幕"):
        with pytest.raises(planner.PlanError, match="deliverable"):
            planner.build_plan(
                {"tasks": [{"id": "t1", "owner": "Project manager", "instruction": "做",
                            "tools": ["assemble_video"], "deliverable": prose}]},
                members, member_tools={"pm": {"assemble_video"}})


def test_the_ledger_counts_a_missing_audio_deliverable(store, make_router):
    """`voice/旁白.wav` was invisible to the ledger: its own list had no wav — or mp3, m4a, mkv.

    The ledger is what the process engineer reads, so this was the number that says "this keeps
    happening" under-counting the group's own kind of work.
    """
    from app import proclog

    entries = proclog.auto_task_defects(
        {"id": "t3", "title": "配音", "owner": "资料员", "status": "done",
         "deliverable": "voice/旁白.wav", "error": ""},
        [], set())
    keys = [e.get("key") for e in entries]
    assert any("旁白.wav" in k for k in keys), keys


def test_no_module_grows_its_own_list_of_file_extensions_again():
    """A scan, not a unit test — because the defect is **duplication**, and four copies is what it took.

    Every one of them was reasonable on its own, and the pair that disagreed silently decided whether
    a delivery was verified at all. A guard that only checks today's three aliases would not stop the
    fifth copy appearing somewhere else, so this reads the source: no module but `planner` may carry a
    regex alternation of file extensions. (The set literals in `_pending_reviews` are a *different*
    judgement — which media a review tool accepts — and are deliberately not matched.)
    """
    import pathlib
    import re

    ext = (r"(?:png|jpe?g|webp|avif|tiff?|svg|gif|mp4|mov|webm|mkv|m4v|wav|mp3|m4a|aac|flac|aiff|ogg|opus"
           r"|docx|doc|pptx|xlsx|csv|pdf|md|txt|srt|ass|vtt|html|json|xml|ya?ml|tex|bib|zip|py|js|ts)")
    # ⚠️ `\.` then `(` then three or more extensions — the shape every one of the four copies had
    # (`r"\.(png|jpg|…)"`, `r"…\.(?:docx|…)"`). Requiring the escaped dot is what keeps this off
    # prose: a schema comment reading `kind … 'note | txt | md | pdf'` is a list of values, not a
    # suffix pattern, and flagging it would make the guard noise instead of a guard.
    alternation = re.compile(r"\\\.\s*\((?:\?:)?" + ext + r"(?:\s*\|\s*" + ext + r"){2,}", re.I)

    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for p in sorted(app.rglob("*.py")):
        if p.name == "planner.py":          # the one place allowed to hold it
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if alternation.search(line):
                offenders.append(f"{p.name}:{i}: {line.strip()[:120]}")
    assert not offenders, ("扩展名词表只能有一份 (`planner.named_files`),别处又长出来一份:\n"
                           + "\n".join(offenders))

    # The guard has to actually catch the shapes that were there — a scan that matches nothing is a
    # scan that proves nothing. These three are the exact lines that used to exist.
    for old in (r'_FILE_IN_PLAN = re.compile(r"[\w\u4e00-\u9fff][\w .()（）\-]*\.'
                r'(?:docx|pptx|xlsx|csv|pdf|md|txt|srt|png|jpe?g|mp4|mov|webm)\b", re.I)',
                r'_DELIVERABLE_EXT = (r"\.(png|jpg|jpeg|webp|mp4|mov|webm|wav|mp3|m4a|docx|pdf|md)\b")',
                r'FILE_IN_TEXT = re.compile(r"[\w]*\.(?:docx|pptx|xlsx|csv|pdf|md|txt|srt)\b")'):
        assert alternation.search(old), f"这条守卫认不出当年的写法,等于没守:{old[:60]}"


def test_every_file_writing_tool_is_declared_a_producer():
    """`planner.FILE_TOOLS` is what makes "a production task must name its output file" mean anything.

    ⚠️ Miss one and there are **two** silent escapes, not one: the task is allowed to promise no file,
    and then `_execute_plan` has no file name to verify against and skips verification entirely — so
    a `make_figure` task could end `done` with no figure. The list was six names and had missed every
    drawing/sound/print tool added since.

    The check is by name, which is mechanical and therefore free of judgement: anything that looks
    like a producer (the naming the tool table already uses) must be declared, and the only names
    allowed to be missing are the ones that write a file *incidentally* while answering.
    """
    from app import planner, toolhub

    specs = toolhub.BUILTIN_SPECS
    looks_like_a_producer = tuple(p for p in specs if p.startswith(
        ("make_", "generate_", "render_", "assemble_", "write_", "synthesize_", "study_")))

    undeclared = [n for n in looks_like_a_producer
                  if n not in planner.FILE_TOOLS and n not in planner.INCIDENTAL_WRITERS]
    assert not undeclared, (f"这些工具看着就会产出文件,却没写进 planner.FILE_TOOLS:{undeclared}\n"
                            "漏掉它们的后果不是一句错话,而是**整段核验被跳过** —— 任务可以什么都没有还判 done。")

    # …and nothing may be declared that is not a real tool, or `produces` would never match it.
    unknown = [n for n in planner.FILE_TOOLS if n not in specs]
    assert not unknown, f"planner.FILE_TOOLS 里有不存在的工具名:{unknown}"
    # The two "incidental" names exist too, so the exception list cannot rot into a fiction.
    assert all(n in specs for n in planner.INCIDENTAL_WRITERS), planner.INCIDENTAL_WRITERS
    # A producer that was actually missing, pinned by name: this exact task shape used to escape.
    assert "make_figure" in planner.FILE_TOOLS and "render_document" in planner.FILE_TOOLS
