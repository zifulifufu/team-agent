import pytest

from app import planner
from app.toolcall import TagFilter, format_result, parse_tool_calls, strip_hidden, tools_prompt
from tests.conftest import ASSIGNMENT, INTEGRATE, UPSTREAM, has


# ---------------------------------------------------------------- toolcall
def test_parse_tool_call_basic_and_visible_text():
    text = '我先查一下。<tool_call>{"name": "library_search", "arguments": {"query": "报价"}}</tool_call>'
    visible, calls = parse_tool_calls(text)
    assert visible == "我先查一下。"
    assert len(calls) == 1 and calls[0].name == "library_search" and calls[0].arguments == {"query": "报价"}


def test_parse_tool_call_lenient_fences_alias_and_string_args():
    text = '<tool_call>\n```json\n{"tool": "x", "args": "{\\"a\\": 1}"}\n```\n</tool_call>'
    _, calls = parse_tool_calls(text)
    assert calls[0].name == "x" and calls[0].arguments == {"a": 1} and not calls[0].error


def test_parse_tool_call_unterminated_and_invalid():
    _, calls = parse_tool_calls('<tool_call>{"name": "a", "arguments": {}}')  # model forgot the closing tag
    assert calls[0].name == "a"
    _, bad = parse_tool_calls("<tool_call>这不是 JSON</tool_call>")
    assert bad[0].error and not bad[0].name
    _, bad2 = parse_tool_calls('<tool_call>{"name": "a", "arguments": [1]}</tool_call>')
    assert "JSON object" in bad2[0].error


def test_parse_limits_number_of_calls():
    one = '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
    _, calls = parse_tool_calls(one * 5, max_calls=3)
    assert len(calls) == 3


def test_tag_filter_hides_calls_even_when_split_across_chunks():
    f = TagFilter()
    src = '前言<tool_call>{"name":"a"}</tool_call>后语<plan>{"x":1}</plan>结尾'
    out = "".join(f.feed(src[i:i + 2]) for i in range(0, len(src), 2)) + f.flush()
    assert out == "前言后语结尾"


def test_tag_filter_passes_lookalikes_and_drops_unterminated_tail():
    f = TagFilter()
    out = f.feed("a < b 且 <tool 不是标签") + f.flush()
    assert out == "a < b 且 <tool 不是标签"
    g = TagFilter()
    assert g.feed("可见<plan>没写完的计划") + g.flush() == "可见"


def test_strip_hidden_and_format_result_truncates():
    assert strip_hidden('a<plan>{}</plan>b<tool_call>{}') == "ab"
    assert "truncated" in format_result("t", True, "x" * 7000)
    assert 'ok="false"' in format_result("t", False, "err")


# ------------------------------------------------- the four shapes real models produce
# Every string below is the shape found in a stored transcript on this machine, not a shape chosen
# to make the parser look good. Each one had cost a round its work: the calls were dropped, or the
# markers were printed into the chat, or the model's actual answer was swallowed with them.
def test_several_unterminated_calls_are_all_kept():
    """Shape ②: four calls, no closing tag anywhere. Reading the first to the end of the reply glued
    all four into one malformed call and lost the lot."""
    text = ('先看目录。'
            '<tool_call>{"name": "process_log", "arguments": {"action": "scan"}} '
            '<tool_call>{"name": "library_search", "arguments": {"query": "动脉瘤"}} '
            '<tool_call>{"name": "read_file", "arguments": {"path": "a.md"}} '
            '<tool_call>{"name": "list_files", "arguments": {}}')
    visible, calls = parse_tool_calls(text, max_calls=8)
    assert [c.name for c in calls] == ["process_log", "library_search", "read_file", "list_files"]
    assert [c.arguments for c in calls][1] == {"query": "动脉瘤"}
    assert not any(c.error for c in calls)
    assert visible == "先看目录。"


def test_a_bare_marker_never_eats_the_answer():
    """Shape ③, and the most expensive variant: 120 repetitions of the **plural** marker in front of
    a correct diagnosis. The plural was not recognised at all, so the markers were shown as text."""
    text = "<tool_calls>\n" * 120 + "出图这一步被服务端拦下了,默认模型配置错了。"
    visible, calls = parse_tool_calls(text)
    assert calls == []                       # noise, not 120 failed calls
    assert visible == "出图这一步被服务端拦下了,默认模型配置错了。"
    assert "<tool_calls>" not in visible


def test_plural_container_and_several_objects_in_one_block():
    """Shape ④: the plural as a container, and two calls sharing one block."""
    visible, calls = parse_tool_calls(
        '<tool_calls>\n<tool_call>{"name": "a", "arguments": {}}</tool_call>\n'
        '<tool_call>{"name": "b", "arguments": {"x": 1}}</tool_call>\n</tool_calls>', max_calls=8)
    assert [(c.name, c.arguments) for c in calls] == [("a", {}), ("b", {"x": 1})]
    assert visible == ""
    _, both = parse_tool_calls(
        '<tool_call>{"name": "a", "arguments": {}}{"name": "b", "arguments": {"x": 1}}</tool_call>',
        max_calls=8)
    assert [c.name for c in both] == ["a", "b"]


def test_braces_inside_a_prompt_do_not_truncate_the_call():
    """A picture prompt is prose, and prose contains braces. The scanner tracks string literals so
    the object ends where the JSON says it does, not at the first `}` in the prompt."""
    _, calls = parse_tool_calls('<tool_call>{"name": "generate_image", "arguments": '
                               '{"prompt": "一座{古镇}的石桥", "size": "1024x1536"}}</tool_call>')
    assert calls[0].arguments == {"prompt": "一座{古镇}的石桥", "size": "1024x1536"}


def test_tag_filter_releases_an_unterminated_block_instead_of_swallowing():
    """The streaming half of the same failure. A block that never closes must not take the rest of
    the answer with it — on screen the user would watch the model go silent mid-sentence."""
    f = TagFilter()
    out = f.feed("<tool_calls>\n" * 120 + "诊断结论:模型名不对。") + f.flush()
    assert out.strip() == "诊断结论:模型名不对。"
    g = TagFilter()
    src = '前言<tool_call>{"name": "a"}<tool_call>{"name": "b"}后语'
    mid = "".join(g.feed(src[i:i + 3]) for i in range(0, len(src), 3)) + g.flush()
    assert mid.startswith("前言") and mid.endswith("后语")
    # …but half a plan is still dropped, because a truncated plan reads as noise rather than as an
    # unfinished answer. This is the distinction `_DROP_TAIL` exists for.
    h = TagFilter()
    assert h.feed("可见<plan>没写完的计划") + h.flush() == "可见"


def test_tools_prompt_lists_signature_and_required():
    p = tools_prompt([{"name": "s", "description": "搜", "parameters": {
        "type": "object", "properties": {"q": {"type": "string", "description": "词"}, "k": {"type": "integer"}},
        "required": ["q"]}}])
    assert "s(q: string — 词; k: integer (optional))" in p and "<tool_call>" in p
    assert tools_prompt([]) == ""


# ----------------------------------------------------------------- planner
MEMBERS = [{"id": "1", "name": "Aide"}, {"id": "2", "name": "Copywriter"}, {"id": "3", "name": "Proofreader"}]


def _obj(tasks):
    return {"goal": "写通知", "conventions": "称呼统一为「各位同事」", "tasks": tasks}


def test_extract_plan_json_variants():
    assert planner.extract_plan_json("没有计划") is None
    obj = planner.extract_plan_json('思路。<plan>{"tasks": [{"id": "t1"}]}</plan>')
    assert obj and obj["tasks"][0]["id"] == "t1"
    obj = planner.extract_plan_json('```json\n{"goal": "g", "tasks": []}\n```')
    assert obj and obj["goal"] == "g"
    with pytest.raises(planner.PlanError):
        planner.extract_plan_json("<plan>{坏掉的</plan>")


def test_build_plan_orders_by_dependency_and_remaps_ids():
    plan = planner.build_plan(_obj([
        {"id": "a", "owner": "@Proofreader", "title": "审", "instruction": "审校", "needs": ["b"]},
        {"id": "b", "owner": "Copywriter", "title": "写", "instruction": "起草", "strengths": ["写作"], "tools": ["library_search", "nope"]},
    ]), MEMBERS, 8, {"library_search"})
    assert [t.id for t in plan.tasks] == ["b", "a"]            # the dependency comes first
    assert plan.tasks[1].needs == ["b"] and plan.tasks[1].owner_id == "3"
    assert plan.tasks[0].tools == ["library_search"]           # unknown tools are dropped
    assert plan.conventions.startswith("称呼")


def test_build_plan_rejects_unknown_owner_cycle_and_empty():
    with pytest.raises(planner.PlanError, match="not a member of this group"):
        planner.build_plan(_obj([{"owner": "路人", "instruction": "x"}]), MEMBERS)
    with pytest.raises(planner.PlanError, match="depend on each other"):
        planner.build_plan(_obj([
            {"id": "a", "owner": "Copywriter", "instruction": "x", "needs": ["b"]},
            {"id": "b", "owner": "Proofreader", "instruction": "y", "needs": ["a"]}]), MEMBERS)
    with pytest.raises(planner.PlanError):
        planner.build_plan(_obj([]), MEMBERS)
    with pytest.raises(planner.PlanError, match="instruction"):
        planner.build_plan(_obj([{"owner": "Copywriter"}]), MEMBERS)


# ------------------------------------------------- one bad task must not kill the plan
def test_a_task_without_an_instruction_is_saved_by_its_deliverable():
    """Measured: a one-task plan whose task had an owner and a deliverable but no `instruction` was
    refused whole, the round fell back to turn-taking, one member replied and it stopped. The
    delegation the host had already decided on was thrown away over a missing field, and the only
    thing the user saw was "the plan was not valid".

    A task that names its deliverable still says what to produce, so it is usable; the synthesized
    instruction is thin on purpose — the member reads the plan's goal and the user's request too.
    """
    plan = planner.build_plan(_obj([
        {"id": "t1", "owner": "Copywriter", "deliverable": "科普短文.md"}]),
        MEMBERS, 8, {"write_document"})
    assert len(plan.tasks) == 1
    assert "科普短文.md" in plan.tasks[0].instruction
    assert plan.tasks[0].deliverable == "科普短文.md"
    assert plan.dropped == []


def test_a_synonym_for_instruction_is_accepted():
    """The field is asked for as `instruction`; models also write `description`/`brief`/`detail`.
    Refusing a plan over which synonym was used is refusing it over spelling."""
    for key in ("description", "brief", "detail", "action", "what", "task"):
        plan = planner.build_plan(_obj([{"id": "t1", "owner": "Copywriter", key: "起草三段文案"}]),
                                  MEMBERS)
        assert plan.tasks[0].instruction == "起草三段文案", key


def test_an_unusable_task_is_dropped_and_the_rest_still_runs():
    """The whole point: the plan survives, minus the task nobody could be given — and the board says
    which one went, because a board that quietly shows fewer rows is how a dropped requirement goes
    unnoticed."""
    plan = planner.build_plan(_obj([
        {"id": "t1", "owner": "Copywriter", "instruction": "写"},
        {"id": "t2", "owner": "查无此人", "instruction": "不知道派给谁"},
        {"id": "t3", "owner": "Proofreader", "instruction": "审"},
    ]), MEMBERS)
    assert [t.id for t in plan.tasks] == ["t1", "t3"]
    assert len(plan.dropped) == 1 and plan.dropped[0].startswith("t2")
    assert "已丢弃" in planner.summarize(plan) or "dropped" in planner.summarize(plan)
    assert plan.to_meta()["dropped"] == plan.dropped


def test_a_plan_with_nothing_usable_still_raises_so_the_repair_runs():
    """When every task is unusable there is no plan to execute, so the caller's one-shot repair has
    to be the thing that happens — silence would be worse than an error."""
    with pytest.raises(planner.PlanError):
        planner.build_plan(_obj([
            {"id": "t1", "owner": "路人甲"},
            {"id": "t2", "owner": "路人乙"},
        ]), MEMBERS)


def test_an_owner_may_be_named_the_way_the_group_talks_about_them():
    """The failure this fixes, in full: a group whose members are stored under English names but
    addressed in Chinese had **twelve consecutive plans thrown away** because the owner was written
    「文案」 and only the stored name was accepted — so the run silently fell back to round-robin and
    the whole division of labour never happened. Every spelling the rest of the app understands has to
    be understood here too."""
    members = [{"id": "1", "name": "Aide", "role": "Coordinator"},
               {"id": "2", "name": "Copywriter", "role": "Copywriter"},
               {"id": "3", "name": "Storyboard", "role": "Video storyboard artist"}]
    plan = planner.build_plan(_obj([
        {"id": "t1", "owner": "文案", "instruction": "写脚本"},
        {"id": "t2", "owner": "Storyboard", "instruction": "做分镜", "needs": ["t1"]},
        {"id": "t3", "owner": "视频分镜", "instruction": "补分镜", "needs": ["t2"]},
    ]), members)
    # 「文案」 is Copywriter's `name_zh` in the built-in presets — the same alias the @mention resolver
    # reads, which is why the two must not have separate rules.
    assert [t.owner for t in plan.tasks] == ["Copywriter", "Storyboard", "Storyboard"]
    assert planner._match_member("小助", members)["id"] == "1"        # a built-in member's other name
    assert planner._match_member("copywriter", members)["id"] == "2"  # spelling, not case
    assert planner._match_member("路人", members) is None


def test_a_rejected_owner_is_told_who_the_owners_are():
    """Being told "no" with nothing to correct towards is how a model repeats itself — which is
    exactly what the twelve rejections looked like. The message names the ways to write an owner."""
    with pytest.raises(planner.PlanError) as e:
        planner.build_plan(_obj([{"owner": "张三", "instruction": "x"}]), MEMBERS)
    text = str(e.value)
    assert "Copywriter" in text and "Aide" in text


def test_the_planning_prompt_lists_the_writeable_owner_spellings():
    """Prevention rather than recovery: the model is shown the accepted spellings before it decides,
    so the commonest mistake (writing the role instead of the name) never happens."""
    text = planner.planning_instruction(8, "auto", "", planner.owner_list(MEMBERS))
    assert "Copywriter" in text and "文案" in text


def test_build_plan_rejects_excess_tasks_instead_of_silently_dropping_work():
    tasks = [{"id": "t1", "owner": "Copywriter", "instruction": str(i), "needs": ["t1", "ghost"]} for i in range(6)]
    with pytest.raises(planner.PlanError, match="limit"):
        planner.build_plan(_obj(tasks), MEMBERS, max_tasks=3)


def test_task_prompt_carries_conventions_upstream_and_declaration():
    plan = planner.build_plan(_obj([
        {"id": "t1", "owner": "Copywriter", "title": "初稿", "instruction": "写初稿", "deliverable": "Markdown"},
        {"id": "t2", "owner": "Proofreader", "title": "审校", "instruction": "审校初稿", "needs": ["t1"], "strengths": ["中文"]}]), MEMBERS)
    p = planner.task_prompt(plan, plan.tasks[1], {"t1": "这是文案的初稿"}, 2)
    assert "各位同事" in p and "这是文案的初稿" in p and has(p, ASSIGNMENT) and has(p, UPSTREAM) and "中文" in p
    # when upstream produced nothing, tell the downstream step explicitly
    p2 = planner.task_prompt(plan, plan.tasks[1], {}, 2)
    assert "produced nothing" in p2
    integ = planner.integration_prompt(plan, {"t1": "稿"})
    assert has(integ, INTEGRATE) and "did not finish" in integ   # t2 is still pending, i.e. unfinished


def test_a_call_whose_arguments_quote_the_protocol_is_read_correctly():
    """⚠️ Found by an independent review pass, and it is not exotic on this machine: the app's own
    prompts and every skill file contain the call format, so a member documenting the tool — a
    `write_document` body, a task instruction — writes the marker *as text inside a JSON string*.

    The scan for markers sees text, not structure, so it found two markers inside that string. The
    measured result: one perfectly good call became **two malformed ones**, and the tail of the body
    (`这样"}}`) was printed into the chat as if it were prose.
    """
    text = ('<tool_call>{"name": "write_document", "arguments": {"path": "说明.md", "format": "md", '
            '"body": "工具格式是 <tool_call>{\\"name\\": \\"x\\"}</tool_call> 这样"}}</tool_call>')
    visible, calls = parse_tool_calls(text, max_calls=8)
    assert [c.name for c in calls] == ["write_document"]
    assert not any(c.error for c in calls)
    # The quoted tag is *content*: deleting it as if it were a marker corrupts the argument.
    assert calls[0].arguments["body"] == '工具格式是 <tool_call>{"name": "x"}</tool_call> 这样'
    assert visible == ""


def test_a_bare_marker_is_not_a_call_and_keeps_only_itself():
    """The fallback that stops a bare marker from eating the answer.

    Two claims. First, a marker with **nothing in it** is not a failed call: reporting it as one feeds
    the model an error message per marker, and a stored reply had 120 of them in a row. Second, a
    marker with no JSON must not swallow what follows it — reading "no closing tag" as "everything
    after this is my arguments" is exactly how a correct diagnosis that came *after* the markers would
    have vanished from the user's screen.

    ⚠️ A marker whose body is **prose** is a different thing, and is still reported as a failed call —
    that feedback is what lets the model correct itself, and `<tool_call>这不是 JSON</tool_call>`
    depends on it.
    """
    visible, calls = parse_tool_calls('<tool_call>   <tool_calls>  \n <tool_call>')
    assert calls == [] and visible == ""
    visible, calls = parse_tool_calls('<tool_calls>\n' * 3 + '正文在标记之后。')
    assert calls == [] and visible == '正文在标记之后。'
    _v, kept = parse_tool_calls('<tool_call> 这段没有 JSON </tool_call>')
    assert len(kept) == 1 and kept[0].error


def test_a_call_sent_with_no_tags_is_still_a_call():
    """2026-09-25, and the most expensive shape this parser has met.

    The Storyboard member wrote a sentence and then the JSON, with no `<tool_call>` around it. Two
    things broke at once: the call never ran (the storyboard file it believed it had written is not
    on disk, and the next task failed for want of it), and the JSON — a whole markdown document with
    literal `\\n` escapes — was printed into the chat bubble, which is what the user reported as
    「成员发表的内容没有格式,看起来很乱」.

    The sentence in front is kept on purpose: it is prose the member wrote, and it reads as the
    lead-in to a tool result that follows.
    """
    text = ('我先把分镜表从对话上下文里整段落地到工作目录,再开始生成动画。'
            '{"name": "write_document", "arguments": {"path": "tasks/x/分镜表.md", "format": "md", '
            '"body": "# 标题\\n\\n| 镜号 | 起止秒 |\\n|---|---|\\n| 1 | 0-6 |"}}')
    visible, calls = parse_tool_calls(text, known={"write_document", "library_read"})
    assert [c.name for c in calls] == ["write_document"]
    assert calls[0].arguments["path"] == "tasks/x/分镜表.md"
    assert calls[0].arguments["body"].startswith("# 标题")
    assert visible == "我先把分镜表从对话上下文里整段落地到工作目录,再开始生成动画。"
    assert '{"name"' not in visible and "\\n" not in visible
    # …and the display-side cleanup reaches the same verdict, because the rows already stored in
    # someone's database were written before this shape was understood. No tool list out here.
    assert strip_hidden(text) == visible


def test_the_known_tool_list_is_what_separates_a_call_from_quoted_json():
    """A name we could not have run means it is not our protocol — leave the text alone.

    Shape alone (an object with `name` and an `arguments` object) is not enough: a member explaining
    a JSON API, or quoting a payload, produces the same shape. The round's own tool list settles it.
    """
    text = '示例配置是 {"name": "temperature", "arguments": {"value": 0.7}} 这样。'
    _v, calls = parse_tool_calls(text, known={"library_search"})
    assert calls == []
    visible, calls = parse_tool_calls(text, known={"temperature", "library_search"})
    assert [c.name for c in calls] == ["temperature"] and "temperature" not in visible


def test_json_in_a_code_block_is_left_alone():
    """Prose that quotes JSON in a fence stays prose, even when the shape matches a real call."""
    text = '用法:\n\n```json\n{"name": "library_search", "arguments": {"query": "x"}}\n```\n'
    visible, calls = parse_tool_calls(text, known={"library_search"})
    assert calls == [] and '"library_search"' in visible


def test_tagged_and_tagless_calls_keep_their_order():
    text = ('<tool_call>{"name": "a", "arguments": {}}</tool_call>\n'
            '中间一句话\n'
            '{"name": "b", "arguments": {}}')
    visible, calls = parse_tool_calls(text, known={"a", "b"})
    assert [c.name for c in calls] == ["a", "b"]
    assert visible == "中间一句话"


# A textual condition used to be ignored, so both paid generators ran.
@pytest.mark.parametrize("condition", [
    {"title": "备用出图（仅在 t1 失败时启用）"},
    {"instruction": "Generate only if t1 fails"},
    {"fallback_for": "t1"},
    {"when": {"task": "t1", "status": "failed"}},
])
def test_unsupported_standby_conditions_are_not_silently_executed(condition):
    from app import planner
    with pytest.raises(planner.PlanError, match="standby|备用"):
        planner.build_plan({"tasks": [
            {"id": "t1", "owner": "Writer", "instruction": "Synthetic primary"},
            {"id": "t2", "owner": "Writer", "instruction": "Synthetic backup", **condition},
        ]}, [{"id": "w", "name": "Writer"}])


def test_a_call_written_in_two_dialects_at_once_now_runs():
    """2026-09-26, group 冗长扩张动: `{"name": "library_read"> <parameter name="doc">…`.

    A JSON head with a DSML parameter tag after it. Nothing can balance that as JSON, and the
    earlier tag-less rule demanded `, "arguments": {` immediately after the name — so the call
    matched nothing: the file was never read and the call text was printed into the chat bubble.

    ⚠️ This shape used to be **display-only**: `strip_hidden` hid the text and the call was still
    lost, which is the worst of both worlds — the bubble looked clean and the work silently did not
    happen. It now parses into a real call, together with the minimax spelling of the same idea
    (`<query>` instead of `<parameter name="query">`).

    ⚠️ The router's "the model emitted private XML" check survives this, and does so by construction
    rather than by luck: it reads `parse_tool_calls`'s **visible half**, which holds exactly what
    could not be turned into a call. Pure private XML with no parseable head — the shape
    `test_private_tool_markup_triggers_fallback_instead_of_fake_success` uses — is still there.
    """
    text = ('根据流程反馈,我需要先读取 MD 参考文献部分。'
            '{"name": "library_read"> <parameter name="doc">交付/冗长扩张动脉瘤综述.md</parameter>'
            '</invoke>')
    visible, calls = parse_tool_calls(text, known={"library_read"})
    assert [(c.name, c.arguments) for c in calls] == [("library_read", {"doc": "交付/冗长扩张动脉瘤综述.md"})]
    assert visible == "根据流程反馈,我需要先读取 MD 参考文献部分。", visible
    shown = strip_hidden(text, known={"library_read"})
    assert '{"name"' not in shown and "parameter" not in shown, shown
    assert shown.startswith("根据流程反馈"), shown
    # A name we could not have run leaves the text alone, dialect or not — the router needs to see it
    _v, calls = parse_tool_calls(text, known={"library_search"})
    assert calls == []
    # …and pure private XML, which has no JSON head to read at all, is still nobody's call
    _v, calls = parse_tool_calls('<minimax:tool_call><invoke name="library_read">'
                                 '<parameter name="doc">x.md</parameter></invoke></minimax:tool_call>',
                                 known={"library_read"})
    assert calls == []


# ---------------------------------------------------------------- rejection notice
def test_a_rejected_plan_shows_the_task_the_complaint_is_about():
    """The notice used to show the first 500 characters of the plan, whatever the complaint was.

    A real plan is far longer than that: the 8-task plan the video group sent on 2026-09-26 was cut
    off inside `conventions`, so a rejection about `t6` was published next to the JSON for nothing —
    neither the user nor the repairing model could see what `t6` said. The excerpt now follows the
    task id the rejection names.
    """
    from app.orchestrator import _bad_task_id, _plan_excerpt

    raw = ('<plan>{"goal": "做出 300 秒科普片", "conventions": "' + "统一口径。" * 120 + '", '
           '"tasks": ['
           '{"id": "t1", "owner": "A", "instruction": "先读材料", "tools": [], "deliverable": "x.md"}, '
           '{"id": "t6", "owner": "B", "instruction": "烧字幕", "tools": ["write_document"], '
           '"deliverable": "video/字幕轨.html"}]}</plan>')
    why = "任务 t6:制作任务必须在 deliverable 中写出输出文件名及扩展名。"

    assert _bad_task_id(why) == "t6"
    old = _plan_excerpt(raw)                      # the head of the plan: no t6 anywhere in it
    assert '"id": "t6"' not in old and "字幕轨.html" not in old
    new = _plan_excerpt(raw, focus=_bad_task_id(why))
    assert '"id":"t6"' in new and "字幕轨.html" in new and "write_document" in new, new
    assert "做出300秒科普片" in new, "目标要一起给出,否则看不出这条任务为什么存在"
    assert "t1" not in new.split("[t6]")[1], "只给这一条任务,不要把别的任务也带上"
    # 认不出任务 id 时行为不变
    assert _bad_task_id("计划里没有 tasks 数组") == ""


def test_a_gateways_own_token_separator_does_not_cost_the_call():
    """minimax-m3 wraps every token as `]<]minimax[>[`, and the wrapper arrives as plain text.

    2026-09-26, group 介入术式科, member Storyboard, two calls in one turn:
        {"name": "library_search">]<]minimax[>[<query>起伏参数表 起伏版 WAV narration]<]minimax[>[</query>]…
    Both were answered "(malformed) — wrong shape", twice, and the member went on without the
    material it had asked for. Nothing was wrong with the call: the separator sits between the name
    and the arguments, so no JSON can close and no dialect matcher sees a marker it knows.
    """
    from app.toolcall import _denoise

    sep = "]<]minimax[>["
    known = {"library_search", "library_read"}
    body = '{"name": "library_search">' + sep + "<query>起伏参数表 起伏版 WAV narration</query>" + sep + "</invoke>"
    assert sep not in _denoise(body)

    wrapped = f"<tool_call>{body}</tool_call>"
    visible, calls = parse_tool_calls(wrapped, known=known)
    assert [(c.name, c.arguments) for c in calls] == [("library_search", {"query": "起伏参数表 起伏版 WAV narration"})]
    assert not calls[0].error and visible == ""

    # Untagged: the call still runs, and the prose in front of it survives.
    visible, calls = parse_tool_calls("我来查一下。" + body, known=known)
    assert [c.name for c in calls] == ["library_search"] and visible == "我来查一下。"
    assert strip_hidden("我来查一下。" + body, known=known) == "我来查一下。"

    # The `<parameter name="doc">` spelling works through the same separator, and now **runs**
    # instead of only being hidden: this is the shape that used to be display-only.
    dsml = f'<tool_call>{{"name": "library_read">{sep}<parameter name="doc">video/x.md</parameter>{sep}</invoke></tool_call>'
    _v, calls = parse_tool_calls(dsml, known=known)
    assert [(c.name, c.arguments) for c in calls] == [("library_read", {"doc": "video/x.md"})]

    # A name this round could not have called stays put, separator or not. (The `known` filter only
    # applies to the **tag-less** half: an explicit `<tool_call>` block is honoured whatever `known`
    # says, which is how `_dsml_calls` and the JSON branch have always behaved.)
    _v, calls = parse_tool_calls("我来查一下。" + body, known={"library_search_elsewhere"})
    assert calls == []
    # And plain prose that merely contains a JSON head is still prose.
    _v, calls = parse_tool_calls('他写着 {"name": "library_search"} 但没给参数。', known=known)
    assert calls == []


def test_plain_xml_invocations_are_each_their_own_call():
    """`<invoke name="library_read"><doc>x.md</doc></invoke>` — no JSON head anywhere.

    2026-09-26, group 介入术式科, member Storyboard, **four of them side by side** inside a
    `<tool_call>`. All four were answered "(malformed) — wrong shape", three times over, and `t5`
    then failed for lack of the file it had asked for. Two readers in this file had a reason to miss
    it: `_dsml_calls` insists on the `||DSML||` prefix in front of `<invoke`, and the JSON-head rule
    insists on JSON. There is none — the argument tags are the whole message.
    """
    body = ('<invoke name="library_read"><doc>tasks/x/视觉规范.md</doc><limit>3000</limit></invoke>'
            '<invoke name="library_read"><doc>tasks/x/分镜表.md</doc><limit>3000</limit></invoke>'
            '<invoke name="make_figure"><prompt>段1 开场</prompt><size>1080x1920</size></invoke>')
    known = {"library_read", "make_figure"}
    visible, calls = parse_tool_calls(f"<tool_call>{body}</tool_call>", known=known)
    assert [(c.name, c.arguments) for c in calls] == [
        ("library_read", {"doc": "tasks/x/视觉规范.md", "limit": "3000"}),
        ("library_read", {"doc": "tasks/x/分镜表.md", "limit": "3000"}),
        ("make_figure", {"prompt": "段1 开场", "size": "1080x1920"}),
    ], calls                                   # ⚠️ 每条各成一个调用,绝不能合并成一条
    assert not any(c.error for c in calls) and visible == ""

    # `<parameter name="doc">` is the same dialect with the other spelling of an argument
    _v, calls = parse_tool_calls('<tool_call><invoke name="library_read">'
                                 '<parameter name="doc">video/x.md</parameter></invoke></tool_call>',
                                 known=known)
    assert [(c.name, c.arguments) for c in calls] == [("library_read", {"doc": "video/x.md"})]

    # ⚠️ Truncated — no `</invoke>` — must stay in the visible half: this is what keeps the router's
    # "the model emitted private XML" check alive now that the readable shapes run.
    visible, calls = parse_tool_calls('<minimax:tool_call><invoke name="library_read">'
                                      '<parameter name="doc">x.md', known=known)
    assert calls == [] and "<invoke" in visible, visible
