"""`name` means "where in the workspace", for every tool that writes a file.

Measured 2026-09-26 (group 视频制作): a member asked `make_animation` for `anim/镜2-2.mp4` — the path
its own plan promised — and got `anim/anim-镜2-2.mp4.mp4`, because `name` was treated as a bare leaf
and `figure.workspace_output` sanitises `/` into `-`. The clip was on disk, the plan's name was not,
and the task was stamped "did not deliver" twice in one round while the member correctly reported
what it had asked for. Same disease as `synthesize_speech` had with its audio, one tool over.
"""

from __future__ import annotations

import pytest

from app.toolhub import output_name_parts


def test_the_path_the_plan_promised_survives():
    assert output_name_parts("anim/镜2-2.mp4", "coil_fill", "anim") == ("anim", "镜2-2")


def test_a_bare_name_goes_into_the_tools_own_folder():
    assert output_name_parts("镜2-2", "coil_fill", "anim") == ("anim", "镜2-2")
    assert output_name_parts("", "coil_fill", "anim") == ("anim", "coil_fill")


def test_a_nested_folder_is_kept_whole():
    assert output_name_parts("a/b/c.mp4", "x", "anim") == ("a/b", "c")


def test_a_dot_in_the_name_is_not_eaten_as_an_extension():
    """Only a media suffix is stripped. `图 v2.3` losing `.3` would be the tidy-up that loses a file."""
    assert output_name_parts("交付/图v2.3.png", "x", "figures") == ("交付", "图v2.3")
    assert output_name_parts("图v2.3", "x", "figures") == ("figures", "图v2.3")


def test_a_folder_that_climbs_out_is_refused():
    for escape in ("../evil/x.mp4", "anim/../../x.mp4"):
        with pytest.raises(ValueError):
            output_name_parts(escape, "x", "anim")


def test_an_absolute_path_is_refused_rather_than_relativised():
    """`narration_dest` refuses it, so this must too: silently writing `<workspace>/tmp/x.mp4` for a
    caller that asked for `/tmp/x.mp4` is the same disease as rewriting `../a.wav`."""
    with pytest.raises(ValueError):
        output_name_parts("/tmp/x.mp4", "x", "anim")
    with pytest.raises(ValueError):          # a folder, not a file
        output_name_parts("anim/", "x", "anim")
