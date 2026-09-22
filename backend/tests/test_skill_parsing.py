"""The SKILL.md frontmatter parser.

Worth its own tests because the format is shared with other tools: a skill written for
Claude Code uses the same `---` block, and the two notations for a long description — a
quoted one-liner and a YAML block scalar — are equally common. Reading the second as the
literal string ">" is the kind of failure that looks like it worked until somebody opens
the skill and reads its description.
"""

from __future__ import annotations

from app.tools import parse_skill_text, render_skill


def test_a_quoted_one_liner_is_read_as_before():
    skill = parse_skill_text('---\nname: writer\ndescription: "Formal notices"\n---\n\n步骤', "fallback")
    assert (skill.name, skill.description, skill.body) == ("writer", "Formal notices", "步骤")


def test_a_folded_block_scalar_is_joined_into_one_line():
    skill = parse_skill_text("---\nname: writer\ndescription: >\n  Write formal notices.\n  Use for announcements.\nversion: 2\n---\n\n正文", "fallback")
    assert skill.description == "Write formal notices. Use for announcements."
    assert skill.version == "2", "the key after a block scalar must still be read"
    assert skill.body == "正文"


def test_a_literal_block_scalar_keeps_its_line_breaks():
    skill = parse_skill_text("---\nname: writer\ndescription: |\n  第一行\n  第二行\n---\n\n正文", "fallback")
    assert skill.description == "第一行\n第二行"


def test_indented_keys_are_ignored_rather_than_read_as_values():
    """Claude-style frontmatter often nests a `metadata:` block; a naive line reader would
    turn its children into values of the key above."""
    text = "---\nname: writer\nmetadata:\n  author: someone\n  tags: [a, b]\ndescription: Kept\n---\n\nbody"
    skill = parse_skill_text(text, "fallback")
    assert skill.name == "writer" and skill.description == "Kept"
    assert "someone" not in skill.description


def test_what_this_project_writes_can_be_read_back():
    rendered = render_skill("writer", "Formal notices", "写公告的步骤", "group", "1.2")
    skill = parse_skill_text(rendered, "fallback")
    assert skill.name == "writer" and skill.description == "Formal notices"
    assert skill.scope == "group" and skill.version == "1.2"
    assert "写公告的步骤" in skill.body


def test_no_frontmatter_means_the_whole_file_is_the_body():
    skill = parse_skill_text("just words", "folder-name")
    assert skill.name == "folder-name" and skill.description == "" and skill.body == "just words"


def test_scope_only_accepts_the_two_known_values():
    assert parse_skill_text("---\nname: a\nscope: worldwide\n---\n\nx", "a").scope == "member"
    assert parse_skill_text("---\nname: a\nscope: group\n---\n\nx", "a").scope == "group"
