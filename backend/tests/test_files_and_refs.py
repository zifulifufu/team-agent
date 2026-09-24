"""Files in a group chat: where they live, what the model is told about them, and how a user
points at one.

The point of this file: "the model can read it" has to be true for every kind of file the user
might drop in — a spreadsheet without any vision model at all, a screenshot by being described
once, a video by its frames — and never by quietly leaving the model to guess.
"""

from __future__ import annotations

import asyncio
import io
import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

from app import attachments, coderun, library, media, strengths, vision
from app.main import create_app
from app.orchestrator import Orchestrator
from tests.conftest import FakeLLM
from tests.test_collab import Collector, setup

OCTET = {"Content-Type": "application/octet-stream"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def xlsx_bytes(rows: list[list[object]] = [[ "指标", "值"], ["DNT", 45]]) -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def docx_bytes(text: str = "合同正文第一条") -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ classification (the bytes decide)
@pytest.mark.parametrize(("name", "data", "kind"), [
    ("截图.png", PNG, attachments.IMAGE),
    ("照片.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32, attachments.IMAGE),
    ("报表.pdf", b"%PDF-1.7\nhello", attachments.DOCUMENT),
    ("指标表.xlsx", xlsx_bytes(), attachments.DOCUMENT),
    ("合同.docx", docx_bytes(), attachments.DOCUMENT),
    ("笔记.md", "# 标题".encode(), attachments.DOCUMENT),
    ("打包.zip", b"PK\x03\x04" + b"\x00" * 8, attachments.OTHER),
    ("录像.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8, attachments.VIDEO),
    ("录音.mp3", b"ID3\x03\x00" + b"\x00" * 8, attachments.AUDIO),
    ("录音.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", attachments.AUDIO),
    ("一坨二进制.bin", b"\x00\x01\x02\x03", attachments.OTHER),
])
def test_the_kind_comes_from_the_content_not_the_name(name, data, kind):
    got, mime, ext = attachments.classify(data, name)
    assert got == kind, (name, got, mime)
    assert mime and ext


def test_a_zip_called_xlsx_is_still_a_spreadsheet_because_that_is_the_only_way_to_tell():
    """OOXML is a zip; here the name is the only signal there is, and it is accepted on purpose —
    the file is then handed to openpyxl, which rejects it if the contents do not match."""
    kind, mime, _ext = attachments.classify(xlsx_bytes(), "随便起名.bin")
    assert kind == attachments.OTHER and mime == "application/zip"


def test_only_that_one_case_trusts_the_name():
    """A PDF called .png is a PDF: the magic bytes win everywhere else."""
    kind, mime, _ext = attachments.classify(b"%PDF-1.7\nsome text here", "假的.png")
    assert kind == attachments.DOCUMENT and mime == "application/pdf"


# ------------------------------------------------------------------ extraction, no model needed
def test_a_spreadsheet_and_a_presentation_can_be_read_locally():
    kind, text = library.extract_text("指标表.xlsx", xlsx_bytes())
    assert kind == "xlsx" and "DNT | 45" in text

    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "卒中绿色通道"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
    box.text_frame.text = "目标:DNT 不超过 60 分钟"
    slide.notes_slide.notes_text_frame.text = "讲稿要点"
    buf = io.BytesIO()
    deck.save(buf)
    kind, text = library.extract_text("汇报.pptx", buf.getvalue())
    assert kind == "pptx" and "卒中绿色通道" in text and "讲稿要点" in text


def test_the_old_binary_formats_say_what_to_do_instead_of_failing_obscurely():
    with pytest.raises(library.LibraryError) as e:
        library.extract_text("旧表.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert ".xlsx" in str(e.value)


# ------------------------------------------------------------------ the workspace is always there
def test_a_group_gets_its_workspace_when_it_is_made(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.post("/api/groups", json={"name": "新群", "member_ids": []}).json()["id"]
        folder = tmp_path / "data" / "workspaces" / gid
        assert folder.is_dir(), "a group without a workspace is a group that cannot hold files"
        assert (c.get(f"/api/groups/{gid}/workspace").json()["path"]) == str(folder)


def test_groups_that_existed_before_this_get_one_on_the_next_start(tmp_path):
    """The startup pass, because 'the group's workspace' used to be created lazily by the first
    code run — so older groups never had one at all."""
    store_dir = tmp_path / "data"
    app = create_app(store_dir, completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.post("/api/groups", json={"name": "老群", "member_ids": []}).json()["id"]
    gone = coderun.workspace_dir(store_dir, {"code_workdir": ""}, gid)
    for child in sorted(gone.iterdir(), reverse=True):
        child.rmdir()
    gone.rmdir()
    assert not gone.exists()

    create_app(store_dir, completion_fn=FakeLLM(default="好"))         # the next start
    assert coderun.workspace_dir(store_dir, {"code_workdir": ""}, gid).is_dir()


def test_the_workspace_file_route_refuses_to_leave_the_workspace(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.post("/api/groups", json={"name": "群", "member_ids": []}).json()["id"]
        for bad in ("../../../etc/passwd", "/etc/passwd", "~/x", "uploads/../../x"):
            assert c.get(f"/api/groups/{gid}/workspace/file", params={"path": bad}).status_code == 404


# ------------------------------------------------------------------ uploads land in the workspace
def test_an_uploaded_file_lands_in_the_group_workspace_and_can_be_served_back(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.post("/api/groups", json={"name": "群", "member_ids": []}).json()["id"]
        r = c.post(f"/api/groups/{gid}/attachments", params={"filename": "指标表.xlsx"},
                   content=xlsx_bytes(), headers=OCTET)
        assert r.status_code == 200
        row = r.json()
        assert row["kind"] == "document" and row["rel_path"].startswith("uploads/")
        assert "DNT" in (row["text"] or ""), "text is extracted once, at upload"
        on_disk = tmp_path / "data" / "workspaces" / gid / row["rel_path"]
        assert on_disk.is_file(), "a member has to be able to open it with its own tools"

        listed = c.get(f"/api/groups/{gid}/workspace").json()["files"]
        assert [f["path"] for f in listed] == [row["rel_path"]]

        got = c.get(row["url"])
        assert got.status_code == 200 and got.content == xlsx_bytes()
        assert "attachment" in got.headers["content-disposition"]

        assert c.get("/api/groups/nowhere/attachments").status_code == 404


def test_the_size_cap_is_checkable_and_changeable(tmp_path):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        gid = c.post("/api/groups", json={"name": "群", "member_ids": []}).json()["id"]
        assert c.put("/api/settings", json={"upload_max_mb": 1}).status_code == 200
        big = c.post(f"/api/groups/{gid}/attachments", params={"filename": "大.bin"},
                     content=b"\x00" * (2 * 1024 * 1024), headers=OCTET)
        assert big.status_code in (400, 413)
        assert list((tmp_path / "data" / "workspaces" / gid / "uploads").glob("*")) == []


# ------------------------------------------------------------------ what the model is told
def _file_in_workspace(store, gid, name: str, data: bytes) -> dict:
    workspace = coderun.workspace_dir(store.data_dir, store.get_settings(), gid)
    aid = store.new_id()
    kind, mime, ext = attachments.classify(data, name)
    rel = attachments.save(workspace, aid, name, ext, data)
    text = attachments.extract_text(name, data) or None
    return store.add_attachment(gid, aid, name, mime, len(data), kind=kind, rel_path=rel, text=text)


def test_a_document_attachment_reaches_the_model_as_text(store, make_router):
    """No vision model involved: a spreadsheet is text, and it was read when it was uploaded."""
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    row = _file_in_workspace(store, group["id"], "指标表.xlsx", xlsx_bytes())

    asyncio.run(orch.handle_user_message(group["id"], "看下附件", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "document"}]))
    sent = member_call(fake)
    assert "指标表.xlsx" in sent and "DNT | 45" in sent and "uploads/" in sent


def test_a_document_with_no_text_layer_says_so_instead_of_arriving_empty(store, make_router):
    """A scanned PDF used to reach the member as a bare name-and-size header, which reads like a
    document whose contents were simply not worth quoting. The reader then discusses a report
    nobody has seen — the same failure as a picture nobody could look at, one kind over."""
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    scan = b"%PDF-1.7\n" + b"\x00" * 200          # a PDF with no text layer at all
    row = _file_in_workspace(store, group["id"], "报告扫描件.pdf", scan)
    assert not row.get("text"), "nothing was extracted, which is the case under test"

    asyncio.run(orch.handle_user_message(group["id"], "看下附件", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "document"}]))
    sent = member_call(fake)
    assert "报告扫描件.pdf" in sent and "no text could be pulled out" in sent


def test_a_picture_is_described_once_when_no_member_can_see_it(store, make_router):
    """The member's model is text-only, so a vision model looks instead — and the description is
    what the member reads. Described once: the second round re-uses it."""
    # The key is a prefix of what litellm actually calls: a local chat model is
    # spelled `ollama_chat/...`, so "ollama" is the part both spellings share.
    fake = FakeLLM(script={"ollama": "图上是一张化验单,肌钙蛋白 0.02。"}, default="好")
    orch, group = setup(store, make_router, fake)
    store.update_model("ollama/qwen2.5:7b", {"strengths": ["multimodal"]})
    store.update_settings({"vision_model_id": "ollama/qwen2.5:7b"})
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)

    files = [{"id": row["id"], "name": row["name"], "kind": "image"}]
    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(), files=files))
    assert len(vision_calls(fake)) == 1, "one look, one call"
    assert isinstance(member_call(fake), str) and "肌钙蛋白 0.02" in member_call(fake)
    assert store.get_attachment(row["id"])["vision_text"].startswith("图上是一张化验单")

    fake.calls.clear()
    asyncio.run(orch.handle_user_message(group["id"], "再看一次", Collector(), files=files))
    assert not vision_calls(fake), "the picture was described twice"
    assert "肌钙蛋白 0.02" in member_call(fake)


def test_a_local_model_that_can_see_is_handed_the_picture_itself(store, make_router):
    """A local multimodal model needs no permission: the picture never leaves the machine."""
    fake = FakeLLM(default="看到了")
    orch, group = setup(store, make_router, fake)
    store.update_model("ollama/qwen2.5:7b", {"strengths": ["multimodal", "local"]})
    store.update_agent(store.list_agents()[0]["id"], {"model_id": "ollama/qwen2.5:7b"})
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)

    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "image"}]))
    content = fake.calls[-1][1][-1]["content"]
    assert isinstance(content, list), "a model that can see gets parts, not a description"
    assert content[0]["type"] == "text" and content[1]["image_url"]["url"].startswith("data:image/png")


def test_a_cloud_model_that_can_see_still_needs_the_outbound_switch(store, make_router):
    """Sending text off the machine and sending a picture the user attached are separate
    decisions — the second one has its own switch, and until it is on the picture is described
    by whatever local model can, rather than shipped out."""
    fake = FakeLLM(script={"ollama": "描述:一张化验单"}, default="好")
    orch, group = setup(store, make_router, fake)
    store.update_settings({"vision_cloud": False})
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)
    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "image"}]))
    assert not any("data:image" in str(m.get("content"))
                   for model, msgs in fake.calls if "ollama" not in model for m in msgs
                   ), "a picture left the machine for a cloud model"


def test_nothing_is_made_up_when_nobody_can_look_at_the_picture(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    # Nothing usable is multimodal: the usual state on a machine with only text models.
    for m in store.list_models():
        store.update_model(m["id"], {"strengths": ["chinese"]})
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)

    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "image"}]))
    sent = member_call(fake)
    assert "no model here can look at images" in sent
    assert "ollama pull" in sent, "the reader is told what to do about it, not just that it failed"


# ------------------- a model that makes pictures is not a model that looks at pictures
def test_a_generator_is_never_mistaken_for_a_model_that_can_see():
    """`multimodal` reached every `gemini-*` from family reputation alone — including the ones that
    only generate pictures. A gateway lists both kinds together, so "can see" has to be judged by
    what the model is *for*, not by the family name."""
    assert media.purpose_of("gemini-3.1-pro-preview") == "chat"
    for name in ("gemini-3-pro-image", "gpt-image-2.5-sunburst", "flux-2-pro", "doubao-seedance-2-5"):
        assert media.purpose_of(name) != "chat"
        assert "multimodal" not in strengths.infer(name), f"{name} was tagged as able to see"
    assert "multimodal" in strengths.infer("gemini-3.1-pro-preview")
    # And the same judgement holds for a model that was *told* it is multimodal — a tag set by hand,
    # or by an older build of this app.
    assert not vision._can_see({"model_name": "gpt-image-2.5-sunburst", "strengths": ["multimodal"]})
    assert not vision._can_see({"model_name": "whatever", "use": "image", "strengths": ["multimodal"]})
    assert vision._can_see({"model_name": "qwen2.5vl:3b", "strengths": ["multimodal"], "is_local": True})


def test_a_picture_is_still_read_when_the_picked_model_cannot_look(store, make_router):
    """The pick used to be honoured to the letter: naming a model that cannot see meant *no* picture
    in *any* group was read, with nothing on screen connecting the two. It now falls through to a
    model that can — still local-first, so the picture does not leave the machine by accident."""
    fake = FakeLLM(script={"ollama": "描述:一张化验单"}, default="好")
    orch, group = setup(store, make_router, fake)
    store.update_model("ollama/qwen2.5:7b", {"strengths": ["multimodal"]})
    store.update_settings({"vision_model_id": "deepseek/deepseek-v4-pro"})   # a text model, not eyes
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)

    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "image"}]))
    assert len(vision_calls(fake)) == 1, "the picture was not looked at at all"
    assert "描述:一张化验单" in member_call(fake)
    assert store.get_attachment(row["id"])["vision_text"].startswith("描述:一张化验单")


def test_a_cloud_model_picked_by_hand_still_needs_the_cloud_vision_switch(store, make_router):
    """The switch says images never leave the machine while it is off. An explicitly named cloud
    model used to slip past it — pick one model once and every picture in every group went to that
    provider, with the switch off and nothing on screen saying so."""
    fake = FakeLLM(script={"ollama": "描述:一张化验单"}, default="好")
    orch, group = setup(store, make_router, fake)
    store.update_settings({"vision_cloud": False})
    store.update_model("ollama/qwen2.5:7b", {"strengths": ["multimodal"]})
    store.update_model("deepseek/deepseek-flash", {"strengths": ["multimodal"]})   # a cloud one, by hand
    store.update_settings({"vision_model_id": "deepseek/deepseek-flash"})
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)

    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "image"}]))
    assert not pictures_sent_to(fake, "deepseek"), "the picture went to the cloud anyway"
    assert "描述:一张化验单" in member_call(fake), "the local model that can see was not used instead"

    # ...and with the switch on, the model the user named is the one used.
    fake.calls.clear()
    store.update_settings({"vision_cloud": True})
    row2 = _file_in_workspace(store, group["id"], "化验单2.png", PNG)
    asyncio.run(orch.handle_user_message(group["id"], "再看这张", Collector(),
                                         files=[{"id": row2["id"], "name": row2["name"], "kind": "image"}]))
    assert pictures_sent_to(fake, "deepseek"), "the model the user named was not used"


def test_the_picked_model_is_named_when_it_cannot_look(store, make_router):
    """Told "no model here can look at images" while a model is selected for exactly that, the
    reader has no way to find the setting that is wrong. So the sentence carries the name."""
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    for m in store.list_models():
        store.update_model(m["id"], {"strengths": ["chinese"]})
    store.update_settings({"vision_model_id": "deepseek/deepseek-flash"})
    row = _file_in_workspace(store, group["id"], "化验单.png", PNG)

    asyncio.run(orch.handle_user_message(group["id"], "看这张", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "image"}]))
    sent = member_call(fake)
    assert "deepseek-flash" in sent, "the model that was picked has to be named"
    assert "Which model looks at pictures" in sent


def test_a_model_the_provider_dropped_is_never_recommended(store, make_router):
    """Enabled + keyed + multimodal is not enough to be a good recommendation: a model its provider
    has stopped serving fails on the first call. The live listing is what says so, and it is the
    same judgement the model chooser uses to badge one as gone."""
    router = make_router(FakeLLM())
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.update_settings({"vision_cloud": True})
    store.update_model("deepseek/deepseek-flash", {"strengths": ["multimodal"]})
    store.update_model("deepseek/deepseek-v4-pro", {"strengths": ["multimodal"]})
    # v4-pro comes first in the roster, so it is the one "automatic" would pick...
    assert vision.status(store, router)["recommended_id"] == "deepseek/deepseek-v4-pro"

    # ...until the provider's own listing drops it — the case that prompted this was `kimi-k3`,
    # still enabled on an account whose provider had stopped serving it.
    store.set_model_live("deepseek", ["deepseek-flash"])
    st = vision.status(store, router)
    assert st["recommended_id"] == "deepseek/deepseek-flash"
    assert "deepseek/deepseek-v4-pro" not in [c["id"] for c in st["candidates"]]
    assert vision.pick(store, router.usable_models())["id"] == "deepseek/deepseek-flash"

    # A listing we do not have says nothing: nothing may be hidden on the strength of a missing one.
    store.set_model_live("deepseek", [])
    assert "deepseek/deepseek-v4-pro" in [c["id"] for c in vision.status(store, router)["candidates"]]


def test_the_recommendation_is_the_model_that_would_really_run(store, make_router):
    """The settings page stars a model and says "automatic uses this one", so the star has to be the
    model that would actually run — one rule, not two. Local first, and the page is told whether
    that means a picture leaves the machine."""
    router = make_router(FakeLLM())
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})   # usable, so it can be recommended
    store.update_settings({"vision_cloud": False})
    store.update_model("ollama/qwen2.5:7b", {"strengths": ["multimodal"]})
    st = vision.status(store, router)
    assert st["recommended_id"] == "ollama/qwen2.5:7b" and st["recommended_local"] is True
    assert st["recommended_name"] == "qwen2.5:7b"

    # A cloud model that can see does not displace a local one...
    store.update_model("deepseek/deepseek-flash", {"strengths": ["multimodal"]})
    assert vision.status(store, router)["recommended_id"] == "ollama/qwen2.5:7b"

    # ...and with none local, the recommendation names a cloud model and says so.
    store.update_model("ollama/qwen2.5:7b", {"strengths": ["chinese"]})
    st = vision.status(store, router)
    assert st["recommended_id"] == "deepseek/deepseek-flash" and st["recommended_local"] is False

    # Nothing can look at all: no star to give, which is what makes the page print the "install one"
    # sentence instead of a name.
    store.update_model("deepseek/deepseek-flash", {"strengths": ["chinese"]})
    st = vision.status(store, router)
    assert st["recommended_id"] == "" and st["candidates"] == []


def test_the_transcriber_install_hint_fits_this_machine(monkeypatch):
    """"nothing found" is a dead end on its own — the command that would work *here* is the useful
    half of it. Which package is named depends on the hardware (MLX is Apple silicon only), and on
    which installer the machine actually has, because the entry point has to end up in a directory
    this app searches."""
    monkeypatch.setattr(attachments, "apple_silicon", lambda: True)
    monkeypatch.setattr(attachments, "tool", lambda name: f"/usr/local/bin/{name}" if name in ("uv", "pip3") else None)
    assert attachments.suggested_transcriber_install() == "uv tool install mlx-whisper"
    monkeypatch.setattr(attachments, "tool", lambda name: "/usr/local/bin/pipx" if name == "pipx" else None)
    assert attachments.suggested_transcriber_install() == "pipx install mlx-whisper"
    monkeypatch.setattr(attachments, "apple_silicon", lambda: False)
    assert attachments.suggested_transcriber_install() == "pipx install openai-whisper"
    monkeypatch.setattr(attachments, "tool", lambda name: None)
    assert attachments.suggested_transcriber_install() == "pip3 install --user openai-whisper"


def test_apple_silicon_asks_the_hardware_not_the_interpreter(monkeypatch):
    """This backend commonly runs as an x86_64 process under Rosetta on an M-series Mac, where
    `platform.machine()` says x86_64 — believing it would recommend the slow transcriber to someone
    whose machine has the fast one built in."""
    monkeypatch.setattr(attachments.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(attachments.platform, "machine", lambda: "x86_64")

    class Ran:
        stdout = "1\n"

    monkeypatch.setattr(attachments.subprocess, "run", lambda *a, **k: Ran())
    assert attachments.apple_silicon() is True

    Ran.stdout = "0\n"
    assert attachments.apple_silicon() is False

    monkeypatch.setattr(attachments.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no sysctl")))
    assert attachments.apple_silicon() is False, "an unanswerable question is not a 'yes'"


def test_the_capabilities_endpoint_carries_the_install_hint(tmp_path, monkeypatch):
    """The row in Settings reads this endpoint, so the hint has to travel with the 'no'."""
    monkeypatch.setattr(attachments, "transcriber", lambda settings=None: None)
    monkeypatch.setattr(attachments, "suggested_transcriber_install", lambda: "uv tool install mlx-whisper")
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        caps = c.get("/api/capabilities").json()
        assert caps["audio_transcribe"] is False
        assert caps["transcriber_install"] == "uv tool install mlx-whisper"

        monkeypatch.setattr(attachments, "transcriber", lambda settings=None: ("/usr/local/bin/whisper", []))
        caps = c.get("/api/capabilities").json()
        assert caps["audio_transcribe"] is True and caps["transcriber_install"] == ""


def test_a_model_that_cannot_look_is_reported_with_the_reason(store, make_router):
    """`status` is what the settings page reads, so the mis-pick has to be visible there too — and
    an image generator must never be offered as something that looks at pictures."""
    router = make_router(FakeLLM())
    store.update_settings({"vision_cloud": False})
    # A name is all `purpose_of` needs, and this is the shape of the mistake: a gateway lists its
    # chat models and its image models together, so `gpt-image-…` sits in the same list looking like
    # a model that *handles* images.
    store.add_model("ollama", "gpt-image-2")
    store.update_settings({"vision_model_id": "ollama/gpt-image-2"})
    st = vision.status(store, router)
    assert st["configured_sees"] is False and st["configured_found"] is True
    assert st["configured_name"] == "gpt-image-2"
    assert "ollama/gpt-image-2" not in [c["id"] for c in st["candidates"]]
    assert "makes pictures, not one that reads them" in vision.reason_missing(store, router)

    # A name that is not a model here at all is a third case, with its own sentence.
    store.update_settings({"vision_model_id": "deepseek/some-model-that-is-gone"})
    st = vision.status(store, router)
    assert st["configured_sees"] is False and st["configured_found"] is False
    assert "is not here any more" in vision.reason_missing(store, router)

    # Nothing picked = automatic, and that is *not* a misconfiguration.
    store.update_settings({"vision_model_id": ""})
    assert vision.status(store, router)["configured_sees"] is None


def test_an_audio_file_is_named_and_not_pretended_to_have_been_read(store, make_router, monkeypatch):
    """Speech needs a program, not a model, and this app bundles none. With nothing installed the
    honest sentence is used — a member must never write a summary of audio nobody listened to."""
    monkeypatch.setattr(attachments, "tool", lambda name: None)
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    row = _file_in_workspace(store, group["id"], "录音.mp3", b"ID3\x03\x00" + b"\x00" * 64)

    asyncio.run(orch.handle_user_message(group["id"], "听一下", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "audio"}]))
    sent = member_call(fake)
    assert "录音.mp3" in sent and "could transcribe it" in sent
    assert store.get_attachment(row["id"])["text"] in (None, ""), "nothing was stored as its content"


FAKE_STT = """import pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / "spoken.txt").write_text("转写:血压 128/80,心率 72。", encoding="utf-8")
"""


def test_an_audio_file_is_read_when_a_transcriber_is_named(store, make_router, tmp_path):
    """The escape hatch: any command can be named, so a transcriber this app has never heard of —
    including one that runs a model locally — can be wired in without a code change."""
    fake_stt = tmp_path / "fake_stt.py"
    fake_stt.write_text(FAKE_STT, encoding="utf-8")
    store.update_settings({"transcribe_cmd": f"{sys.executable} {fake_stt} --out {{out}}"})

    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    row = _file_in_workspace(store, group["id"], "录音.mp3", b"ID3\x03\x00" + b"\x00" * 64)

    asyncio.run(orch.handle_user_message(group["id"], "听一下", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "audio"}]))
    sent = member_call(fake)
    assert "血压 128/80" in sent
    assert "this machine" in sent, "and it is clear where the words came from"
    assert store.get_attachment(row["id"])["text"].startswith("转写"), "transcribed once, then remembered"

    # Second time round it is a text attachment: no transcriber is started again.
    (tmp_path / "fake_stt.py").unlink()
    fake.calls.clear()
    asyncio.run(orch.handle_user_message(group["id"], "再听一次", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "audio"}]))
    assert "血压 128/80" in member_call(fake)


def test_a_transcriber_that_fails_leaves_the_audio_unread_rather_than_empty(store, make_router, tmp_path):
    """`None` and "it produced nothing" are different answers: the first sends the member to the
    file, the second would look like a recording with no words in it."""
    broken = tmp_path / "broken.py"
    broken.write_text("import sys; sys.exit(2)\n", encoding="utf-8")
    store.update_settings({"transcribe_cmd": f"{sys.executable} {broken}"})
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    row = _file_in_workspace(store, group["id"], "录音.m4a", b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 32)

    asyncio.run(orch.handle_user_message(group["id"], "听", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "audio"}]))
    assert "could transcribe it" in member_call(fake)


def test_the_transcriber_is_reported_only_when_one_is_really_there(store, monkeypatch):
    """The settings page says whether transcription works, so it has to ask the same question the
    transcription path does — a hard-coded False here would be a lie in the other direction."""
    monkeypatch.setattr(attachments, "tool", lambda name: None)
    assert attachments.transcriber(store.get_settings()) is None
    monkeypatch.setattr(attachments, "tool", lambda name: "/usr/local/bin/whisper" if name == "whisper" else None)
    exe, args = attachments.transcriber(store.get_settings())
    assert exe.endswith("whisper") and "--output_dir" in args
    store.update_settings({"transcribe_cmd": "/opt/mine/stt --write {out}"})
    exe, args = attachments.transcriber(store.get_settings())
    assert exe == "/opt/mine/stt" and args == ["--write", "{out}"]


# ------------------------------------------------------------------ @ references
def told(fake, index: int = 0) -> str:
    """Everything the model was given on one call: system prompt plus the conversation."""
    return "\n\n".join(str(m.get("content")) for m in fake.calls[index][1])


def vision_calls(fake) -> list:
    """The calls made to the vision model. litellm spells a local Ollama chat call
    `ollama_chat/...`, so match loosely rather than pinning the provider spelling."""
    return [m for m in fake.calls if "ollama" in m[0]]


def pictures_sent_to(fake, needle: str) -> list:
    """Calls to one model that carried a picture as content parts.

    Looking at the *parts* rather than at the model name is what makes "the picture did not leave
    the machine" checkable: a member's own prompt mentions the file by name either way.
    """
    return [m for m, messages in fake.calls
            if needle in m and "data:image" in str(messages)]


def member_call(fake) -> str:
    """What the member was given — i.e. the first call that is not the vision model looking."""
    for model, messages in fake.calls:
        if "ollama" not in model:
            return "\n\n".join(str(m.get("content")) for m in messages)
    raise AssertionError("no member call happened")


def test_a_referenced_file_and_folder_are_inlined_with_their_content(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    workspace = coderun.workspace_dir(store.data_dir, store.get_settings(), group["id"])
    (workspace / "任务说明.md").write_text("# 任务\n把结论写在最后。", encoding="utf-8")
    (workspace / "素材").mkdir(exist_ok=True)
    (workspace / "素材" / "一.txt").write_text("素材内容甲", encoding="utf-8")
    (workspace / "素材" / "二.txt").write_text("素材内容乙", encoding="utf-8")

    asyncio.run(orch.handle_user_message(group["id"], "按 @file:任务说明.md 做,参考 @dir:素材",
                                         Collector()))
    sent = told(fake)
    assert "任务说明.md" in sent and "把结论写在最后" in sent
    assert "素材/一.txt" in sent and "素材内容乙" in sent, "a folder is listed with a taste of each file"


def test_a_reference_to_a_path_outside_the_workspace_is_ignored(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    asyncio.run(orch.handle_user_message(group["id"], "读 @file:../../../etc/passwd 和 @file:/etc/hosts",
                                         Collector()))
    sent = told(fake)
    assert "root:" not in sent and "localhost" not in sent


def test_an_earlier_message_can_be_referenced(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    first = store.add_message(group["id"], "user", "user", "我", "会议定在周四下午两点。")

    asyncio.run(orch.handle_user_message(group["id"], f"再确认一下 @msg:{first['id']}", Collector()))
    sent = told(fake)
    assert "earlier message" in sent and "周四下午两点" in sent, "the quoted message is marked as a quote"


def test_the_reference_budget_stops_a_file_from_swallowing_the_prompt(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    store.update_settings({"refs_budget": 1000})
    workspace = coderun.workspace_dir(store.data_dir, store.get_settings(), group["id"])
    (workspace / "长文.md").write_text("甲乙丙丁" * 3000, encoding="utf-8")

    asyncio.run(orch.handle_user_message(group["id"], "看看 @file:长文.md", Collector()))
    sent = told(fake)
    assert sent.count("甲乙丙丁") * 4 < 1600, f"the file was not clipped into the budget ({sent.count('甲乙丙丁')} copies)"


def test_the_old_hashtag_shorthand_for_a_document_still_works(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    lib = library.Library(store)
    lib.add_file("差旅制度.md", "住宿不超过 600 元".encode(), kb_id=lib.shared_kb()["id"])

    asyncio.run(orch.handle_user_message(group["id"], "按 #差旅制度 总结", Collector()))
    assert "不超过 600 元" in told(fake)


# ------------------------------------------------------------------ tasks get their own folder
def test_a_task_gets_its_own_folder_inside_the_group_workspace(store, make_router):
    from tests.test_collab import plan_script

    fake = FakeLLM(default=plan_script())
    orch, group = setup(store, make_router, fake)
    asyncio.run(orch.handle_user_message(group["id"], "帮我出个发布会方案", Collector()))
    asyncio.run(orch.drain())

    workspace = coderun.workspace_dir(store.data_dir, store.get_settings(), group["id"])
    board = [m for m in store.list_messages(group["id"]) if m["sender_type"] == "plan"][0]
    made = sorted(p.name for p in (workspace / "tasks" / board["id"]).iterdir())
    assert len(made) == 2, made
    dirs = [t["dir"] for t in board["meta"]["tasks"]]
    assert all(d.startswith("tasks/") for d in dirs), dirs

    prompts = [told_for(m) for _, m in fake.calls]
    worker = next((t for t in prompts if "[Your folder]" in t or "【你的交付目录】" in t), "")
    assert worker, "the member is told where its own files go"
    assert f"tasks/" in worker and (board["meta"]["tasks"][0]["dir"] in worker)


def told_for(messages: list[dict]) -> str:
    return "\n\n".join(str(m.get("content")) for m in messages)


def last_user(messages: list[dict]) -> str:
    rows = [m for m in messages if m["role"] == "user"]
    content = rows[-1]["content"]
    return content if isinstance(content, str) else content[0]["text"]


def test_a_media_tool_is_found_even_when_the_app_was_started_from_the_finder(monkeypatch):
    """A GUI-launched app gets launchd's minimal PATH, which has no Homebrew prefix in it — so
    "ffmpeg is installed" and "the app can find ffmpeg" are different facts, and video frames
    would otherwise never be taken without any error to explain why."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    assert attachments.tool("sh") is not None, "a tool on that PATH is still found"
    import shutil as _shutil

    real = _shutil.which
    monkeypatch.setattr(_shutil, "which", lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else real(name))
    assert attachments.tool("ffmpeg") == "/opt/homebrew/bin/ffmpeg"

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    missing = attachments.tool("definitely-not-a-real-binary")
    assert missing is None, "and a tool that really is absent still reports as absent"

    # A transcriber is the tool a user installs by hand, and pipx / `pip install --user` put it in
    # ~/.local/bin — which no GUI-launched app has on its PATH. Without this, "I installed it" and
    # "the app found it" were different facts with nothing on screen to explain the gap.
    assert str(pathlib.Path.home() / ".local/bin") in attachments.TOOL_DIRS


def test_the_readers_of_the_new_file_kinds_are_declared_as_dependencies():
    """They are imported lazily, so a missing one does not break the app — it just makes a
    spreadsheet or a presentation "not supported yet", silently, on a fresh install. Declaring
    them is the difference between working out of the box and looking broken."""
    declared = (pathlib.Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    for package in ("openpyxl", "python-pptx", "pillow"):
        assert package in declared, f"{package} is used but not declared in requirements.txt"


# ------------------------------------------------------------------ running the transcriber
def test_the_transcriber_is_given_the_directories_it_needs(store, monkeypatch, tmp_path):
    """A transcriber needs ffmpeg to decode the audio, and the app is commonly launched from Finder —
    whose PATH has no /usr/local/bin. So an *installed* transcriber failed with
    `FileNotFoundError: 'ffmpeg'` and read to everyone as "this machine cannot listen". `bindirs`
    exists so that cannot happen; this was the consumer nobody had added to it.
    """
    seen: dict = {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        (tmp_path / "out.txt").write_text("你好")          # what a working transcriber writes
        return Done()

    monkeypatch.setattr(attachments, "transcriber", lambda s=None: ("/x/mlx_whisper", ["{audio}"]))
    monkeypatch.setattr(attachments.subprocess, "run", fake_run)
    monkeypatch.setattr(attachments, "tempfile", type("T", (), {"mkdtemp": staticmethod(lambda **k: str(tmp_path))}))
    text, why = attachments.transcribe_with_reason(tmp_path / "a.m4a", store.get_settings())

    assert text == "你好" and why == ""
    path = (seen["env"] or {}).get("PATH") or ""
    from app.bindirs import TOOL_DIRS
    # Whatever is on the app's own PATH is kept, and the tool directories are appended to it — the
    # order matters, because a version the user put first deliberately must still win.
    assert any(d in path.split(":") for d in TOOL_DIRS if pathlib.Path(d).is_dir())


def test_a_transcriber_that_exits_zero_without_writing_is_not_heard_as_silence(store, monkeypatch,
                                                                              tmp_path):
    """The failure that actually happens here, and it exits **0**.

    With the model fetch blocked, `mlx_whisper` logs `Skipping <file> due to ProxyError: 502 Bad
    Gateway` and returns success. Trusting the exit code would report "this recording is empty" — the
    one answer that is worse than an error, because a member would then write the narration out of its
    own head. So the output is what is checked, and a mirror is tried once.
    """
    calls: list = []

    class Done:
        def __init__(self, code=0):
            self.returncode = code
            self.stdout = ""
            self.stderr = "Skipping a.m4a due to ProxyError: 502 Bad Gateway"

    def fake_run(cmd, **kw):
        calls.append((kw.get("env") or {}).get("HF_ENDPOINT"))
        return Done()                                    # exits 0 and writes nothing, both times

    monkeypatch.setattr(attachments, "transcriber", lambda s=None: ("/x/mlx_whisper", ["{audio}"]))
    monkeypatch.setattr(attachments.subprocess, "run", fake_run)
    monkeypatch.setattr(attachments, "tempfile", type("T", (), {"mkdtemp": staticmethod(lambda **k: str(tmp_path))}))
    text, why = attachments.transcribe_with_reason(tmp_path / "a.m4a", store.get_settings())

    assert text is None and "wrote no text" in why
    assert calls == [None, attachments.HF_MIRROR], "the mirror was not tried exactly once"

    # A user who set HF_ENDPOINT themselves is left alone: their endpoint is what the child gets,
    # and no mirror is tried behind their back.
    calls.clear()
    monkeypatch.setenv("HF_ENDPOINT", "https://my-own-mirror.example")
    attachments.transcribe_with_reason(tmp_path / "a.m4a", store.get_settings())
    assert calls == ["https://my-own-mirror.example"]
