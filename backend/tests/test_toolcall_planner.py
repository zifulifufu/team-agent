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
