"""Members that *are* generators.

A member whose model does not chat — Doubao Seedance, H3, a MetaChat video or image model — used to
be impossible to add: the member roster filters those rows out, and the API refused them outright.
This is the feature that lets one join a group, so what is under test here is the whole road: the
member it creates, the turn it gets, and the case where the model it came from goes away.

No language model is involved in such a turn, and the assertions say so by checking the fake model
was never called — the point of the feature being that the user's own sentence is the prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import attachments as attachments_lib
from app import imagegen, library, media, video
from app.main import create_app
from app.orchestrator import split_ungrounded
from tests.conftest import FakeLLM
from tests.test_ark_video import MODEL, FakeArk
from tests.test_collab import Collector, setup

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# What the prompt-writing model answers in these tests, so "did the generator get the written prompt
# rather than the raw sentence" is a one-line assertion.
WRITTEN = "第一人称手持镜头:手摘下一颗带晨露的红苹果,投入雪克杯用力摇晃;听到摇晃声与轻快鼓点"


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Production polls every few seconds; the loop is what is under test, not the waiting."""
    monkeypatch.setattr(video, "POLL_START", 0.01)
    monkeypatch.setattr(video, "POLL_MAX", 0.02)


@pytest.fixture
def ark_call(monkeypatch):
    """`srv = ark_call(**kwargs)` builds Ark and points the tool at it."""
    real = video.generate

    def make(**kw):
        srv = FakeArk(**kw)

        async def wrapped(prov, payload, **rest):
            rest["client"] = srv.client()
            return await real(prov, payload, **rest)

        monkeypatch.setattr(video, "generate", wrapped)
        return srv

    return make


@pytest.fixture
def studio(store, make_router, ark_call):
    """A group whose only member is the Seedance model itself."""
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 30,
                           "video_max_mb": 8})
    prov = store.add_provider_from_preset("doubao-seedance", api_key="sk-ark-123456")
    model = store.list_provider_models(prov["id"])[0]
    member = store.ensure_model_agent(model["id"])
    group = store.create_group("生成组", member["id"], [member["id"]])
    # The one model call in this path is the prompt writer, so the fake's default answer *is* the
    # prompt the generator should end up with.
    fake = FakeLLM(default=WRITTEN)
    orch, _ = setup(store, make_router, fake)
    return orch, store, group, member, prov, fake


async def speak(orch, group, text, files=None):
    c = Collector()
    await orch.handle_user_message(group["id"], text, c, files=files or [])
    saved = [m for m in orch.store.list_messages(group["id"]) if m["sender_type"] == "agent"]
    return c, (saved[-1] if saved else None), [e["message"] for e in c.events if e["type"] == "message_end"]


# ------------------------------------------------------------------ what kind of member it is
def test_a_generating_model_becomes_a_media_member(store):
    prov = store.add_provider_from_preset("doubao-seedance")
    model = store.list_provider_models(prov["id"])[0]

    member = store.ensure_model_agent(model["id"])

    assert member["origin"] == media.MEDIA_ORIGIN
    assert member["model_id"] == model["id"]
    assert "Media member" in member["role"] and "Video generation" in member["role"]
    assert "generating member" in member["prompt"]
    target = media.member_target(store, member)
    assert target and target["use"] == "video" and target["provider"]["id"] == prov["id"]


def test_a_chat_model_is_still_an_ordinary_member(store):
    """The two are told apart by `origin`, and a chat model must not drift into the other branch."""
    prov = store.add_provider_from_preset("deepseek", api_key="sk-test-123456")
    model = store.list_provider_models(prov["id"])[0]

    member = store.ensure_model_agent(model["id"])

    assert member["origin"] == "model"
    assert media.member_target(store, member) is None


def test_asking_twice_gives_the_same_member_back(store):
    prov = store.add_provider_from_preset("doubao-seedance")
    model = store.list_provider_models(prov["id"])[0]
    assert store.ensure_model_agent(model["id"])["id"] == store.ensure_model_agent(model["id"])["id"]


def test_the_generators_are_listable_for_the_member_picker(store):
    """`list_models` is the chat roster and stays one; the picker needs the other list, and a chat
    gateway's own image models belong on it too."""
    ark = store.add_provider_from_preset("doubao-seedance")
    chat = store.add_provider_from_preset("deepseek", api_key="sk-test-123456")
    store.add_model(chat["id"], "gemini-3-pro-image")      # a chat provider serving an image model

    media_rows = {m["model_name"]: m for m in store.list_media_models()}

    assert MODEL in media_rows and media_rows[MODEL]["use"] == "video"
    assert media_rows["gemini-3-pro-image"]["use"] == "image"
    assert "gemini-3-pro-image" in [m["model_name"] for m in store.list_models()], \
        "the chat roster is unchanged — the split is a second list, not a filter on the first"
    assert all(m["model_name"] != MODEL for m in store.list_models())


def test_the_api_offers_them_and_accepts_one_as_a_member(tmp_path):
    client = TestClient(create_app(tmp_path / "app" / "data", completion_fn=None, background=False),
                        base_url="http://127.0.0.1")
    pid = client.post("/api/providers", json={"preset": "doubao-seedance", "api_key": "sk-x"}).json()["id"]

    row = next(p for p in client.get("/api/providers").json() if p["id"] == pid)
    assert [m["model_name"] for m in row["media_models"]] == [MODEL]
    assert row["models"] == [], "and it is not offered as something a member can chat with"

    gid = client.post("/api/groups", json={"name": "g", "member_ids": []}).json()["id"]
    added = client.post(f"/api/groups/{gid}/members/from-model",
                        json={"model_id": row["media_models"][0]["id"]})
    assert added.status_code == 200, added.text
    members = [a for a in client.get("/api/agents").json() if a["origin"] == media.MEDIA_ORIGIN]
    assert len(members) == 1 and members[0]["name"] in \
        [a["name"] for a in client.get("/api/agents").json()]


# ------------------------------------------------------------------ the turn it gets
async def test_the_prompt_is_written_from_the_conversation_not_lifted_from_the_sentence(studio, ark_call):
    """The whole point of the member being *in* the group: you discuss the clip, then say "make that".

    Handing the generator only the last sentence throws away what the discussion produced, so a chat
    model turns the conversation into a prompt first — and the assertion is that the generator got
    *that*, not the sentence.
    """
    orch, store, group, member, prov, fake = studio
    srv = ark_call()

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 按刚才商量的做")

    assert saved is not None and "Seedance" in saved["content"]
    assert srv.payloads[0]["content"][0]["text"] == WRITTEN
    assert fake.calls, "the conversation was read by a model before the generator ran"
    clips = list((Path(store.data_dir) / "workspaces" / group["id"] / "video").glob("*.mp4"))
    assert len(clips) == 1


async def test_the_writer_sees_what_the_members_concluded(studio, ark_call):
    """Not just the last line: the earlier speakers, what they settled on, and which reference
    material the generator is about to receive."""
    orch, store, group, member, prov, fake = studio
    srv = ark_call()
    store.add_message(group["id"], "agent", "someone", "小助",
                      "结论:走第一人称视角，用「苹苹安安」限定款，尾帧定格在产品图上。")
    before = len(fake.calls)

    _c, _saved, _ends = await speak(orch, group, f"@{member['name']} 就按这个来")

    sent = fake.calls[before][1]
    text = "\n".join(str(m.get("content") or "") for m in sent)
    assert "苹苹安安" in text and "第一人称" in text, "the conversation reached the writer"
    assert "就按这个来" in text, "and so did the instruction"
    assert "Seedance" in text, "and which model it is writing for"
    assert srv.payloads[0]["content"][0]["text"] == WRITTEN


async def test_the_writer_is_told_what_this_provider_wants(studio, ark_call):
    """A prompt for Seedance is not a prompt for H3 — the conventions differ, and they are the one
    thing a generic writer cannot guess."""
    orch, store, group, member, prov, fake = studio
    ark_call()

    await speak(orch, group, f"@{member['name']} 做个广告")

    system = str(fake.calls[0][1][0]["content"])
    assert "Seedance 2.5" in system and "@图片1" in system
    assert "no preamble" in system, "and that only the prompt may come back"


async def test_a_prompt_wrapped_in_packaging_is_cleaned_up(studio, ark_call):
    """A model that answers with a fenced block and a closing offer is the common case, and none of
    that packaging belongs in a video model's text field."""
    orch, store, group, member, prov, fake = studio
    fake.default = "```\n" + WRITTEN + "\n```\n\n需要我调整吗？"
    srv = ark_call()

    await speak(orch, group, f"@{member['name']} 做个广告")

    assert srv.payloads[0]["content"][0]["text"] == WRITTEN


async def test_a_generation_still_happens_when_nothing_can_read_the_conversation(store, make_router, ark_call):
    """The writer is a nicety, not a dependency. With every chat model down, the clip is still made
    from the sentence as written — and the message says so, because "the clip ignored the
    discussion" and "there was no model to read it" look identical from outside.
    """
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 30,
                           "video_max_mb": 8})
    prov = store.add_provider_from_preset("doubao-seedance", api_key="sk-ark-123456")
    member = store.ensure_model_agent(store.list_provider_models(prov["id"])[0]["id"])
    group = store.create_group("生成组", member["id"], [member["id"]])
    orch, _ = setup(store, make_router, FakeLLM(default=RuntimeError("every model is down")))
    srv = ark_call()

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 一杯苹果果茶的广告")

    assert saved is not None
    assert srv.payloads[0]["content"][0]["text"] == "一杯苹果果茶的广告"
    assert "No chat model was available" in saved["content"], saved["content"]


async def test_a_model_that_answers_with_nothing_falls_back_too(studio, ark_call):
    """A whitespace-only answer is not a prompt, and the router refuses to pretend otherwise (it
    raises "the model returned nothing"), so this is the same fallback path as every model being
    down — which is what the assertion pins, rather than a branch of my own that no input reaches.
    """
    orch, store, group, member, prov, fake = studio
    fake.default = "   "
    srv = ark_call()

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 一只猫在键盘上睡着")

    assert srv.payloads[0]["content"][0]["text"] == "一只猫在键盘上睡着"
    assert saved is not None and "No chat model was available" in saved["content"]


async def test_the_clip_is_offered_the_same_way_a_tool_call_offers_one(studio, ark_call):
    """One rendering path, so a member that *is* a generator looks exactly like a member that
    called one — the same pill, the same player, nothing new for the interface to learn."""
    orch, store, group, member, prov, fake = studio
    ark_call()

    _c, saved, ends = await speak(orch, group, f"@{member['name']} 一只猫")

    assert ends and ends[-1]["id"] == saved["id"]
    entries = saved["meta"]["tools"]
    assert len(entries) == 1
    assert entries[0]["name"] == "generate_video" and entries[0]["status"] == "ok"
    assert entries[0]["files"][0]["kind"] == "video"
    assert "prompt" in entries[0]["args"]


async def test_a_message_with_no_content_besides_the_name_says_what_is_wrong(studio):
    orch, store, group, member, prov, fake = studio

    c, saved, _ends = await speak(orch, group, f"@{member['name']}")

    assert saved is None
    notes = [e["message"]["content"] for e in c.events if e["type"] == "message"]
    assert any("nothing to work from" in n for n in notes), notes


async def test_an_attachment_becomes_a_reference_picture(studio, ark_call):
    """The one way a group can hand a generator a picture without hosting it: the file is already in
    its own workspace, and it is inlined from there."""
    orch, store, group, member, prov, fake = studio
    srv = ark_call()
    workspace = Path(store.workspace_dir(group["id"]))
    aid = "att-1"
    rel = attachments_lib.save(workspace, aid, "shot.jpg", ".jpg", JPEG)
    store.add_attachment(group["id"], aid, "shot.jpg", "image/jpeg", len(JPEG), kind="image", rel_path=rel)

    await speak(orch, group, f"@{member['name']} 让这张图动起来",
                files=[{"id": aid, "name": "shot.jpg", "kind": "image", "mime": "image/jpeg",
                        "bytes": len(JPEG)}])

    item = srv.payloads[0]["content"][1]
    assert item["role"] == "reference_image"
    assert item["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_the_master_switch_still_governs_a_member_it_is_merely_an_alias_for(studio, ark_call):
    orch, store, group, member, prov, fake = studio
    srv = ark_call()
    store.update_settings({"video_enabled": False})

    c, saved, _ends = await speak(orch, group, f"@{member['name']} 一只猫")

    assert saved is None and srv.payloads == []
    notes = [e["message"]["content"] for e in c.events if e["type"] == "message"]
    assert any("switched off" in n for n in notes), notes


async def test_a_member_runs_on_the_provider_it_came_from_not_the_groups_choice(store, make_router, monkeypatch):
    """Two video providers, and the member is the Ark one: choosing MetaChat in the settings must
    not silently redirect a member that names its own model."""
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 30,
                           "video_max_mb": 8, "video_provider_id": "metachat-media",
                           "video_model": "grok-imagine-video-1.5-preview"})
    ark_prov = store.add_provider_from_preset("doubao-seedance", api_key="sk-ark-123456")
    store.add_provider_from_preset("metachat-media", api_key="sk-mc-123456")
    model = store.list_provider_models(ark_prov["id"])[0]
    member = store.ensure_model_agent(model["id"])
    group = store.create_group("生成组", member["id"], [member["id"]])
    orch, _ = setup(store, make_router, FakeLLM(default="never called"))

    srv = FakeArk()
    real = video.generate

    async def wrapped(prov, payload, **rest):
        rest["client"] = srv.client()
        return await real(prov, payload, **rest)

    monkeypatch.setattr(video, "generate", wrapped)
    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 一只猫")

    assert saved is not None and srv.payloads, "the member's own provider is the one that ran"
    assert srv.payloads[0]["model"] == MODEL


async def test_a_media_member_does_not_hand_off(studio, ark_call):
    """Its "reply" is written by this app, not by a model that read the group, so an `@` in it would
    not be delegation — there is none here, and this holds the line on that."""
    orch, store, group, member, prov, fake = studio
    ark_call()

    c, saved, _ends = await speak(orch, group, f"@{member['name']} 一只猫")

    assert saved is not None
    assert [m["sender_name"] for m in store.list_messages(group["id"])
            if m["sender_type"] == "agent"] == [member["name"]]


def test_a_media_member_is_never_the_owner(store, make_router):
    """The owner plans and delegates, and a generator has no judgement to add."""
    from app.orchestrator import Orchestrator

    prov = store.add_provider_from_preset("doubao-seedance")
    media_member = store.ensure_model_agent(store.list_provider_models(prov["id"])[0]["id"])
    chat = store.list_agents()[0]
    group = store.create_group("混合组", media_member["id"], [media_member["id"], chat["id"]])

    orch = Orchestrator(store, make_router(FakeLLM(default="x")))
    members = store.group_members(group["id"])

    assert orch._pick_host(group, members)["id"] == chat["id"]


# ------------------------------------------------------------------ when its model goes away
async def test_a_member_that_lost_its_model_says_so_instead_of_staying_silent(studio):
    """Deleting a model deletes the member made from it, so the way this happens is a database that
    arrived from somewhere else — a restored backup, or a hand edit. A row pointing at nothing must
    be reported as such rather than sitting in the group looking like a member that went quiet.
    """
    orch, store, group, member, prov, fake = studio
    store.update_agent(member["id"], {"model_id": None})

    c, saved, _ends = await speak(orch, group, f"@{member['name']} 一只猫")

    assert saved is None
    notes = [e["message"]["content"] for e in c.events if e["type"] == "message"]
    assert any("model it was made from is gone" in n for n in notes), notes


def test_deleting_the_provider_takes_its_media_member_with_it(store):
    """A member *is* the model, so it cannot outlive it — and leaving a row pointing at nothing
    would turn into a member that silently answers nothing."""
    prov = store.add_provider_from_preset("doubao-seedance")
    member = store.ensure_model_agent(store.list_provider_models(prov["id"])[0]["id"])
    assert store.get_agent(member["id"])

    store.delete_provider(prov["id"])

    assert store.get_agent(member["id"]) is None


async def test_a_member_gets_exactly_the_one_tool_it_is(store, make_router):
    """Drawing goes down the same road, and the `use` of the model decides which generator the turn
    runs. One tool, not the group's whole toolbox: a generating member has no judgement to spend on
    choosing, and handing it search or code would make the turn a conversation again.
    """
    from app.orchestrator import Orchestrator

    prov = store.add_provider_from_preset("metachat-media", api_key="sk-mc-123456")
    models = {m["model_name"]: m for m in store.list_provider_models(prov["id"])}
    member = store.ensure_model_agent(models["z-image-turbo"]["id"])
    assert "Image generation" in member["role"] and member["origin"] == media.MEDIA_ORIGIN

    orch = Orchestrator(store, make_router(FakeLLM(default="x")))
    group = store.create_group("画室", member["id"], [member["id"]])
    ctx = await orch.toolhub.context(store.get_group(group["id"]), member, connect=False)

    assert set(ctx.tools) == {"generate_image"}
    assert ctx.media == {"provider_id": prov["id"], "model": "z-image-turbo", "use": "image"}
    assert imagegen.KINDS and not ctx.problems


# ------------------------------------------------- can the picture rest on anything
def test_the_write_line_is_split_off_the_prompt():
    """The writer is asked for one extra line — what it could not establish from the material — and
    that line must not be filmed as text. A writer that ignores the request costs nothing: the whole
    answer is then the prompt."""
    prompt, ungrounded = split_ungrounded("写实镜头:导管进入颈内动脉。\n\n未确证: 病灶尺寸、真实患者影像")
    assert prompt == "写实镜头:导管进入颈内动脉。"
    assert ungrounded == "病灶尺寸、真实患者影像"
    assert split_ungrounded("一只猫") == ("一只猫", "")
    # "nothing to report" in either language is not a finding, and neither is a missing line.
    assert split_ungrounded("一只猫\nUngrounded: none") == ("一只猫", "")
    assert split_ungrounded("一只猫\n未确证:无") == ("一只猫", "")


def write_call(fake) -> str:
    """The user message the writer was given. The writer is always the *last* call: the search-terms
    call (`_media_search_terms`) can come first, and only when a search found nothing."""
    return str(fake.calls[-1][1][1]["content"])


async def test_the_writer_is_given_the_text_of_what_was_attached(studio, ark_call):
    """The material a group actually has reaches the one step that needs it. It did not before, and
    that is the whole of "the picture is invented": a chat says what the clip is about, so every
    structure in it was filled in by the writer."""
    orch, store, group, member, prov, fake = studio
    ark_call()
    workspace = Path(store.workspace_dir(group["id"]))
    body = "颅内动脉瘤介入治疗:弹簧圈填塞瘤腔,载瘤动脉保持通畅;S05 不出现血喷与碎片。".encode()
    rel = attachments_lib.save(workspace, "att-script", "脚本.md", ".md", body)
    store.add_attachment(group["id"], "att-script", "脚本.md", "text/markdown", len(body),
                         kind="document", rel_path=rel)
    store.set_attachment_text("att-script", body.decode())

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 按脚本做第 5 段",
                                   files=[{"id": "att-script", "name": "脚本.md", "kind": "document",
                                           "mime": "text/markdown", "bytes": len(body)}])

    told = write_call(fake)
    assert "弹簧圈填塞瘤腔" in told, told
    # And the clip says what it was built on, so "invented" and "unfounded" can be told apart.
    assert "脚本.md" in saved["content"] and "was built on" in saved["content"], saved["content"]


async def test_the_writer_is_given_what_the_knowledge_base_holds_on_the_subject(studio, ark_call):
    """The library is searched for the generating member, because it has no tools and no turn to
    spare: every other member searches it itself, and this step used to skip it entirely."""
    orch, store, group, member, prov, fake = studio
    ark_call()
    lib = library.Library(store)
    lib.add_file("颅内动脉瘤介入.md",
                 "颈内动脉分为七段;颅内动脉瘤介入治疗用弹簧圈填塞瘤腔,载瘤动脉保持通畅。".encode(),
                 kb_id=lib.shared_kb()["id"])

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 做一段动脉瘤介入的画面")

    told = write_call(fake)
    assert "弹簧圈填塞瘤腔" in told, told
    assert "《颅内动脉瘤介入》" in told and "《颅内动脉瘤介入》" in saved["content"], saved["content"]


async def test_a_group_with_the_knowledge_base_off_searches_nothing(studio, ark_call):
    """One gate decides what a group may read (`scope_kbs`/`scope_ids`), and the generation path is
    not an exception to it — the same rule the reference block and every tool already follow."""
    orch, store, group, member, prov, fake = studio
    ark_call()
    lib = library.Library(store)
    lib.add_file("颅内动脉瘤介入.md", "弹簧圈填塞瘤腔。".encode(), kb_id=lib.shared_kb()["id"])
    store.update_group(group["id"], {"ext": {**group["ext"],
                                             "library": {"mode": "off", "kb_ids": [], "collection_ids": []}}})

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 做一段动脉瘤介入的画面")

    told = write_call(fake)
    assert "弹簧圈填塞瘤腔" not in told, told
    # No material found is said out loud rather than smoothed over: the writer is told it has none,
    # and the group is told the picture rests on nothing.
    assert "knowledge base found nothing" in told, told
    assert "No material was found" in saved["content"], saved["content"]


async def test_a_prompt_written_from_nothing_says_so_under_the_clip(studio, ark_call):
    """The one answer that must never read as if it had sources. The clip is still generated — it is
    a picture, and refusing would not help — but the group is told the picture rests on nothing and
    what to do about it."""
    orch, store, group, member, prov, fake = studio
    ark_call()

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 一只猫在键盘上睡着")

    assert "No material was found" in saved["content"], saved["content"]
    assert "Add the material" in saved["content"], saved["content"]


def test_the_note_under_a_clip_says_what_it_rests_on():
    """All four shapes of the two lines that go under an artefact, including the drawing case, which
    is asked for with nothing to go on far more often than a clip is."""
    from app.orchestrator import Orchestrator

    cited = Orchestrator._grounding_note(["1 attached file(s): 脚本.md"], "", "video")
    assert "脚本.md" in cited and "built on" in cited

    bare_clip = Orchestrator._grounding_note([], "", "video")
    assert "No material was found" in bare_clip and "ask again" in bare_clip

    bare_drawing = Orchestrator._grounding_note([], "", "image")
    assert "No material was found" in bare_drawing and "ask again" not in bare_drawing

    both = Orchestrator._grounding_note(["the 1 knowledge-base excerpt(s): 《颅内动脉瘤介入》"],
                                        "病灶尺寸", "video")
    assert "《颅内动脉瘤介入》" in both and "病灶尺寸" in both


async def test_a_chinese_request_finds_an_english_document(studio, ark_call):
    """Measured on a real library: a knowledge base of surgical atlases is English while the group
    talks Chinese, and BM25 over bigrams shares no tokens between the two — the group's own words
    matched nothing at all, while the same question carrying its English name matched the right
    document immediately. So when the group's words find nothing, one short call names the subject in
    both languages before the search is given up on."""
    orch, store, group, member, prov, fake = studio
    ark_call()
    lib = library.Library(store)
    lib.add_file("Internal Carotid Artery Segments.md",
                 "Internal Carotid Artery (Cervical): the carotid bifurcation, up to the entry of the "
                 "petrous bone. Coiling the aneurysm keeps the parent artery open.".encode(),
                 kb_id=lib.shared_kb()["id"])
    # The terms call is the first one; the writer's is the one after it.
    fake.default = lambda messages: ("internal carotid artery segments aneurysm coiling"
                                     if len(fake.calls) == 1 else WRITTEN)

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 做一段颈内动脉动脉瘤栓塞的画面")

    told = write_call(fake)
    assert "carotid bifurcation" in told, told
    assert "《Internal Carotid Artery Segments》" in saved["content"], saved["content"]


async def test_the_note_says_which_of_the_two_kinds_of_nothing_it_was(studio, ark_call):
    """A library of 300 documents that matched none of the words and a library with nothing in it
    look identical from outside, and they call for different things: reword the request, or add the
    material. The note names which one happened."""
    orch, store, group, member, prov, fake = studio
    ark_call()
    lib = library.Library(store)
    lib.add_file("Internal Carotid Artery Segments.md", "carotid bifurcation".encode(),
                 kb_id=lib.shared_kb()["id"])
    fake.default = lambda messages: ("unrelated terms" if len(fake.calls) == 1 else WRITTEN)

    _c, saved, _ends = await speak(orch, group, f"@{member['name']} 做一段完全无关的东西的画面")

    assert "none of the 1 documents in the knowledge base matched" in saved["content"], saved["content"]


# ------------------------------------------------- a generator works from what summoned it
def test_a_member_summoned_by_another_member_works_from_that_members_line(store, make_router):
    """The expensive half of the prompt problem.

    A generating member that another member @-mentions has no instruction of its own, and the fallback
    used to be the user's whole last message. So a picture service was handed
    「上面是我的发音，其他是需要内容进行配音的…是 clone 这个声音」as a drawing prompt — twice — and
    returned nothing, because the last thing the user said had nothing to do with a picture. The
    sentence that summoned the member is the instruction; the user's own words stay the fallback for
    when *the user* is the one asking.
    """
    st = store
    orch, g = setup(st, make_router, FakeLLM(default="好"))
    members = st.list_agents()
    teller, target = members[0], members[1]

    st.add_message(g["id"], "user", "user", "我", "上面是我的发音，是 clone 这个声音。")
    st.add_message(g["id"], "agent", teller["id"], teller["name"],
                   f"@{target['name']} 出 4 张写实脑血管造影图，9:16。")

    line = orch._summoning_line(st.get_group(g["id"]), members, target)
    assert "脑血管造影" in line and "clone" not in line

    # A user message at the end stops the walk: the user's own words are already the caller's
    # fallback, and rewriting them here would only lose them.
    st.add_message(g["id"], "user", "user", "我", "再画一张")
    assert orch._summoning_line(st.get_group(g["id"]), members, target) == ""


# ------------------------------------------------- what the members panel says about it
def test_a_generating_member_is_not_warned_about_a_chat_model(tmp_path):
    """Its `model_id` is a video model, and the chat roster deliberately has none of those.

    So the "is your pinned model usable right now" check — which walks the *dialogue* chain — found
    nothing, reported "Model not found", and every generating member in the video group wore a
    permanent ⚠ claiming it had fallen back to another model. It never runs on a chat model at all.

    The second half of this test is the load-bearing half: the exemption has to be about `origin`, not
    about "no warning ever", or it would quietly switch off the real warning too.
    """
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"))
    c = TestClient(app, base_url="http://127.0.0.1")
    st = app.state.store
    prov = st.add_provider_from_preset("doubao-seedance", api_key="sk-ark-123456")
    model = st.list_provider_models(prov["id"])[0]
    member = st.ensure_model_agent(model["id"])
    g = c.get("/api/groups").json()[0]

    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": model["id"]})
    row = next(m for m in c.get(f"/api/groups/{g['id']}/capabilities").json()["members"]
               if m["origin"] == "media")
    assert row["agent_id"] == member["id"]
    assert row["manual_model"], "a generating member is pinned to its own model"
    assert row["model_problem"] == ""

    chat = next(m for m in st.list_models() if m["provider_id"] == "deepseek")
    c.post(f"/api/groups/{g['id']}/members/from-model", json={"model_id": chat["id"]})
    other = next(m for m in c.get(f"/api/groups/{g['id']}/capabilities").json()["members"]
                 if m["origin"] == "model")
    assert other["model_problem"], "a chat member whose key is missing is still reported"
