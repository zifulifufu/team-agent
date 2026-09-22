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

import pytest
from fastapi.testclient import TestClient

from app import attachments, coderun, library
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


def test_an_audio_file_is_named_and_not_pretended_to_have_been_read(store, make_router):
    fake = FakeLLM(default="好")
    orch, group = setup(store, make_router, fake)
    row = _file_in_workspace(store, group["id"], "录音.mp3", b"ID3\x03\x00" + b"\x00" * 64)

    asyncio.run(orch.handle_user_message(group["id"], "听一下", Collector(),
                                         files=[{"id": row["id"], "name": row["name"], "kind": "audio"}]))
    sent = member_call(fake)
    assert "录音.mp3" in sent and "not transcribed" in sent


# ------------------------------------------------------------------ @ references
def told(fake, index: int = 0) -> str:
    """Everything the model was given on one call: system prompt plus the conversation."""
    return "\n\n".join(str(m.get("content")) for m in fake.calls[index][1])


def vision_calls(fake) -> list:
    """The calls made to the vision model. litellm spells a local Ollama chat call
    `ollama_chat/...`, so match loosely rather than pinning the provider spelling."""
    return [m for m in fake.calls if "ollama" in m[0]]


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
    made = sorted(p.name for p in (workspace / "tasks").iterdir())
    assert len(made) == 2, made
    board = [m for m in store.list_messages(group["id"]) if m["sender_type"] == "plan"][0]
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
