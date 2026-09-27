"""The budget on the skills block in a system prompt.

One group's skills are assembled into ONE block with a character ceiling, and a single skill may
take at most half of it. Everything here is about what happens when they do not all fit — which is
the normal case on this machine, where a video group's skills add up to more than the ceiling.

Untested until 2026-09-27, and the two things that were wrong in it were both about a prompt telling
a member to do something it cannot do:

* the note left on a cut-short skill used to say the rest was in `<path>/SKILL.md` and to read it
  first. No member tool can open that path — every file read in `toolhub` goes through
  `relative_to(workspace)` / `is_relative_to(root)`, and nothing there touches the skills folder at
  all — so the only thing a member could do with that sentence was guess at the missing steps or
  claim to have read them.
* the same was true of the note on a skill that ships files of its own ("read what it points you at
  from there, and run its scripts from that directory").
"""

from __future__ import annotations

import pathlib

from app import i18n, tools

# The two sentences that told a member to open something it cannot open. They are asserted *absent*
# rather than the new wording being asserted present: a test that pins today's translation goes red
# the first time somebody improves it, and what must not come back is the dead instruction.
DEAD_INSTRUCTIONS = (
    "read that before working",
    "照着这几步做之前先读它",
    "read what it points you at from there",
    "它让你读的东西都在那里",
)


def _dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _big_body(lines: int = 900) -> str:
    return "".join(f"第 {i} 步：先量再改。\n" for i in range(lines))


def test_an_oversized_skill_keeps_a_readable_head_and_says_where_the_rest_is(tmp_path):
    """A skill nobody has heard of cannot be looked up, so a cut skill keeps its opening and the
    location of its full text — the location being something the *user* can be asked for."""
    d = _dir(tmp_path)
    tools.write_skill(d, "Enormous", "巨大", _big_body(), "member")

    block = tools.skills_prompt(d, ["Enormous"], group=True)
    assert "第 0 步" in block, "开头要留着,否则成员连这条技能叫什么都不知道"
    assert str(d / "Enormous" / "SKILL.md") in block, "要指出全文在哪,哪怕成员自己打不开"
    assert len(block) < 4000, "一个技能最多只能吃掉预算的一半"


def test_an_oversized_skill_does_not_eat_the_ones_behind_it(tmp_path):
    """The failure this half of the ceiling exists to stop: one long manual used to swallow the
    whole block — every skill written after it was dropped *silently*, while the settings page went
    on showing all of them as attached."""
    d = _dir(tmp_path)
    tools.write_skill(d, "Enormous", "巨大", _big_body(), "member")
    tools.write_skill(d, "Small", "小", "后面这一条必须到得了。", "member")

    block = tools.skills_prompt(d, ["Enormous", "Small"], group=True)
    assert "后面这一条必须到得了" in block


def test_a_cut_skill_never_tells_a_member_to_open_what_it_cannot_open(tmp_path):
    for lang in ("en", "zh"):
        was = i18n.current()
        i18n.set_current(lang)
        try:
            d = _dir(tmp_path / lang)
            tools.write_skill(d, "Enormous", "巨大", _big_body(), "member")
            block = tools.skills_prompt(d, ["Enormous"], group=True)
            for dead in DEAD_INSTRUCTIONS:
                assert dead not in block, f"[{lang}] 这句会让成员去读一个它打不开的文件:{dead}"
        finally:
            i18n.set_current(was)


def test_a_skill_with_files_of_its_own_does_not_send_the_member_after_them(tmp_path):
    """A skill that ships `references/` or `scripts/` is a manual, not a paragraph. The path is worth
    naming — the user can be asked for a file — but not as something the member goes and opens."""
    for lang in ("en", "zh"):
        was = i18n.current()
        i18n.set_current(lang)
        try:
            d = _dir(tmp_path / lang)
            tools.write_skill(d, "Manual", "手册", "先读 `references/how.md`。", "member")
            (d / "Manual" / "references").mkdir()
            (d / "Manual" / "references" / "how.md").write_text("细则", encoding="utf-8")

            block = tools.skills_prompt(d, ["Manual"], group=True)
            assert tools.skill_extra_files(tools.list_skills(d)[0]) == 1, "这条技能确实带着文件"
            assert str(d / "Manual") in block, "要说出那些文件在哪"
            for dead in DEAD_INSTRUCTIONS:
                assert dead not in block, f"[{lang}] 这句让成员去读它读不到的文件:{dead}"
        finally:
            i18n.set_current(was)


def test_every_skill_is_accounted_for_however_many_are_attached(tmp_path):
    """Nine oversized skills into one ceiling. The later ones cannot fit — that is arithmetic, not a
    bug — but none of them may vanish without trace: a block that silently omits a skill leaves a
    group whose prompt and whose settings page disagree about what it was told to do, and the model
    has no way to know it is missing anything. So each one is either a section of the block or named
    in the line that says what was left out."""
    d = _dir(tmp_path)
    names = [f"Long {i}" for i in range(9)]
    for n in names:
        tools.write_skill(d, n, "长", _big_body(300), "member")

    block = tools.skills_prompt(d, names, group=True)
    missing = [n for n in names if n not in block]
    assert not missing, f"这些技能既没进正文,也没被点名说明为什么不在:{missing}"

    # And the ones that did make it are the first ones, in the order they were asked for — a group's
    # rules are read top-down, so the order is a decision, not an accident.
    assert "Long 0" in block.split("Long 2")[0]
