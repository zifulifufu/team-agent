"""Video generation through a ComfyUI on the user's own machine.

The fourth job shape, and the only one that is not a service: ComfyUI runs a *graph*, so there is
no prompt field, no `model` in the request, and no URL in the reply. What is under test is
therefore our half of a contract that is mostly arithmetic plus file bookkeeping:

* the numbers the graph will accept (`4n+1` frames, sides a multiple of 16, a capped long side),
* the envelope (`POST /prompt` -> `prompt_id`, `GET /history/{id}` -> `status`/`outputs`,
  `GET /view?filename=…` -> bytes),
* and every refusal that happens before minutes of GPU time are spent.

The service is faked twice over, on purpose. A `MockTransport` proves the requests we build and the
verdicts we reach; a **real socket** server at the end proves the parts a mocked transport cannot —
the path joining, the real header names, and that the bytes that land in the workspace are the
bytes the instance served. Nothing here talks to a real ComfyUI: a render takes minutes.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import comfyui, discovery, imagegen, media, video

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096
WORKFLOW = comfyui.DEFAULT_WORKFLOW

# The three file names this instance has, and the choices ComfyUI reports for its loaders.
UNET = "wan2.2_ti2v_5B_fp16.safetensors"
CLIP = "umt5_xxl_fp16.safetensors"
VAE = "wan2.2_vae.safetensors"


def object_info(*, unet: str = UNET, clip: str = CLIP, vae: str = VAE,
                without: tuple[str, ...] = (), workflow: str = WORKFLOW) -> dict:
    """An `/object_info` reply, near enough to the real one's shape.

    Each loader declares its file list as the first element of a `[list, {...}]` spec — that is the
    shape `comfyui._choices` reads, and the shape measured against ComfyUI 0.35.0.

    ⚠️ `workflow` decides which node classes are declared, because a picture and a clip do not use
    the same nodes: a fake built from the video graph is missing `ConditioningZeroOut` and
    `EmptySD3LatentImage`, and the probe then reports those as *absent from the instance* — a
    failure about the test's own fake rather than about the code.
    """
    out: dict = {n["class_type"]: {"input": {"required": {}}}
                 for n in comfyui.graph(workflow, prompt="test", width=256, height=256,
                                        frames=25, seed=1, prefix="test").values()
                 if n["class_type"] not in without}
    for cls, field, name in (("UNETLoader", "unet_name", unet),
                             ("CLIPLoader", "clip_name", clip),
                             ("VAELoader", "vae_name", vae)):
        if cls in without:
            continue
        out[cls] = {"input": {"required": {field: [[name], {"tooltip": ""}]}}}
    return out


class FakeComfy:
    """Records what it was asked, replays a script.

    Its habits are reproduced rather than simplified, because our handling exists because of them:
    a finished job leaves `/history` as `{"status": {"completed": true, "status_str": "success"},
    "outputs": {...}}`; a failure arrives as `status_str == "error"` with the reason buried in a
    `messages` list; and the clip is filed under the key **`images`** even though it is a video
    (`SaveVideo` puts `"animated": true` next to it).
    """

    def __init__(self, *, history=None, submit=None, stats=None, info=None, clip: bytes = MP4,
                 filename: str = "clip_00001_.mp4", subfolder: str = "team-agent",
                 extra_output: dict | None = None, missing: bool = False,
                 stats_status: int = 200):
        self.submit_reply = submit
        self.history_reply = history
        # `stats_status` is separate from `stats` on purpose: `stats=None` has to keep meaning "the
        # usual reply" for every other test, so it cannot also mean "answer 500".
        self.stats_status = stats_status
        self.stats = stats if stats is not None else {"system": {"comfyui_version": "0.35.0"}}
        self.info = info if info is not None else object_info()
        self.clip = clip
        self.filename = filename
        self.subfolder = subfolder
        self.extra_output = extra_output or {}
        self.missing = missing
        self.paths: list[str] = []
        self.queries: list[dict] = []
        self.auth: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.queries.append(parse_qs(request.url.query.decode()))
        self.auth.append((request.url.path, request.headers.get("authorization", "")))
        p = request.url.path
        if p == "/prompt":
            self.bodies.append(json.loads(request.content))
            if self.submit_reply is not None:
                return httpx.Response(200, json=self.submit_reply)
            return httpx.Response(200, json={"prompt_id": "pid-1", "number": 0, "node_errors": {}})
        if p.startswith("/history/"):
            if self.missing:
                # A job this instance has never heard of: not proof of failure, and not proof of
                # completion either — which is exactly why the caller keeps waiting.
                return httpx.Response(200, json={})
            if self.history_reply is not None:
                return httpx.Response(200, json={"pid-1": self.history_reply})
            self.polls += 1
            if self.polls < 2:
                return httpx.Response(200, json={"pid-1": {"status": {"status_str": "running"}}})
            return httpx.Response(200, json={"pid-1": completed(self.filename, self.subfolder,
                                                                self.extra_output)})
        if p == "/view":
            return httpx.Response(200, content=self.clip)
        if p == "/system_stats":
            if self.stats_status >= 400:
                return httpx.Response(self.stats_status)
            return httpx.Response(200, json=self.stats)
        if p == "/object_info":
            return httpx.Response(200, json=self.info)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def completed(filename: str, subfolder: str = "", extra: dict | None = None) -> dict:
    outputs = {"58": {"images": [{"filename": filename, "subfolder": subfolder,
                                  "type": "output"}], "animated": [True]}}
    outputs.update(extra or {})
    return {"status": {"completed": True, "status_str": "success", "messages": []},
            "outputs": outputs}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(video, "POLL_START", 0.01)
    monkeypatch.setattr(video, "POLL_MAX", 0.02)


PROV = {"id": "p-comfy", "name": "ComfyUI", "kind": "comfyui", "base_url": "http://127.0.0.1:8188",
        "api_key": "", "is_local": True, "enabled": True, "models": [WORKFLOW]}


# ------------------------------------------------------------------ the arithmetic
def test_the_frame_count_is_one_the_model_will_accept():
    """`4n+1`, always: length 121 is the template's own five seconds at 24 fps, and a count that is
    not `4n+1` is what the model rejects — after the caller has waited for it."""
    assert comfyui.frames_for(5, fps=24) == 121, "the template's own five seconds"
    assert comfyui.frames_for(1, fps=24) == 25
    assert comfyui.frames_for(10, fps=24) == 241
    for sec in range(1, 11):
        n = comfyui.frames_for(sec, fps=24)
        assert (n - 1) % 4 == 0, f"{sec}s produced {n}, which is not 4n+1"
        # Never more than asked for, and never more than one frame short of it either.
        assert sec * 24 < n <= sec * 24 + 1, f"{sec}s produced {n}"


def test_both_sides_are_a_multiple_of_sixteen_and_the_long_side_is_capped():
    w, h = comfyui.size_for("16:9", 480, long_side_max=1280)
    assert (w, h) == (848, 480), "480 * 16/9 rounds to 853, which the VAE's stride takes to 848"
    for ratio in ("16:9", "4:3", "1:1", "3:4", "9:16"):
        w, h = comfyui.size_for(ratio, 704, long_side_max=1280)
        assert w % 16 == 0 and h % 16 == 0, f"{ratio} gave {w}x{h}"
        assert max(w, h) <= 1280
    w, h = comfyui.size_for("9:16", 1280, long_side_max=1280)
    assert (w, h) == (720, 1280)
    # A nonsense setting must not produce a zero-pixel graph.
    w, h = comfyui.size_for("16:9", 8, long_side_max=1280)
    assert w >= 128 and h >= 128
    assert comfyui.size_for("garbage", 480, long_side_max=1280) == comfyui.size_for("16:9", 480, long_side_max=1280)


def test_the_graph_names_the_files_the_workflow_declares():
    """Node ids are ComfyUI's own template's, so this graph can be diffed against one exported from
    the UI. The file names come from `WORKFLOWS`, not from the graph builder — that is what lets a
    different workflow be a table entry instead of a code change."""
    g = comfyui.graph(WORKFLOW, prompt="一只红色的风筝", width=848, height=480, frames=121,
                      seed=7, prefix="team-agent/kite")
    assert g["37"]["inputs"]["unet_name"] == UNET
    assert g["38"]["inputs"]["clip_name"] == CLIP and g["38"]["inputs"]["type"] == "wan"
    assert g["39"]["inputs"]["vae_name"] == VAE
    assert g["55"]["inputs"] == {"vae": ["39", 0], "width": 848, "height": 480, "length": 121,
                                 "batch_size": 1}
    assert g["6"]["inputs"]["text"] == "一只红色的风筝"
    assert g["7"]["inputs"]["text"] == comfyui.NEGATIVE
    assert g["3"]["inputs"]["seed"] == 7 and g["3"]["inputs"]["steps"] == 20
    assert g["57"]["inputs"]["fps"] == 24.0
    # `.mp4` explicitly: `auto` could hand back a WebM that `video.save` would name `.mp4`.
    assert g["58"] == {"class_type": "SaveVideo",
                       "inputs": {"video": ["57", 0], "filename_prefix": "team-agent/kite",
                                  "format": "mp4"}}


def test_the_payload_reports_the_seed_it_actually_sent():
    """The tool tells the user the seed so they can reproduce a clip. Computing it twice — once for
    the report and once for the graph — would report a number the render never used."""
    p = comfyui.payload_for(WORKFLOW, prompt="x", ratio="16:9", seconds=5, short_edge=480, seed=0)
    assert p["seed"] == p["graph"]["3"]["inputs"]["seed"]
    assert p["seed"] > 0, "0 means 'unspecified' in the tool schema; KSampler would take it literally"
    assert p["frames"] == p["graph"]["55"]["inputs"]["length"]
    assert p["seconds"] == 5.04, "121 frames at 24 fps"
    q = comfyui.payload_for(WORKFLOW, prompt="x", ratio="16:9", seconds=5, short_edge=480, seed=7)
    assert q["seed"] == 7 and q["graph"]["3"]["inputs"]["seed"] == 7


def test_the_shipped_workflow_names_and_the_settings_list_are_the_same_thing():
    """Two tables hold the same names — `media.BUILTIN_MEDIA_MODELS` (what the settings dropdown
    offers) and `comfyui.WORKFLOWS` (what can actually render). A name in one and not the other is
    a settings entry that fails on submit, so they are pinned together here.

    ⚠️ Pinned **per use**, not as one set. Both lists used to be the same set of names, so a single
    union comparison held — and it kept passing for the wrong reason once a still-picture workflow
    existed, because "video" would have had to equal the union of both families. Grouping by `use`
    is the invariant that actually matters: the image dropdown must offer the image workflows and
    nothing else, the video one the video workflows and nothing else.
    """
    # Both tables, because both are offered in the dropdown: `WORKFLOWS` are the ones that render,
    # `SETUP_WORKFLOWS` the ones declared before their weights are installed. A name in the settings
    # list and in neither table is a settings entry that fails on submit.
    shipped = {name: row["use"] for name, row in
               {**comfyui.WORKFLOWS, **comfyui.SETUP_WORKFLOWS}.items()}
    offered = media.BUILTIN_MEDIA_MODELS["comfyui"]
    for use in ("video", "image"):
        assert set(offered[use]) == {n for n, u in shipped.items() if u == use}, \
            f"the {use} dropdown and the {use} workflows must be the same names"
    assert set(offered) == {"video", "image"}, "every family the table names is offered"
    assert set(offered["video"]) | set(offered["image"]) == set(shipped), \
        "and every shipped workflow is offered somewhere"
    assert media.purpose_of(WORKFLOW) == "video"
    assert media.purpose_of("z-image-turbo") == "image"
    assert "comfyui" in video.KINDS and "comfyui" in media.MEDIA_KINDS
    assert "comfyui" not in video.ARK_KIND, "sanity: the constant is not the kind"


def test_an_unknown_workflow_falls_back_rather_than_failing():
    assert comfyui.workflow_of("something-else") is comfyui.WORKFLOWS[WORKFLOW]
    g = comfyui.graph("something-else", prompt="x", width=848, height=480, frames=21, seed=1,
                      prefix="p")
    assert g["37"]["inputs"]["unet_name"] == UNET


# ------------------------------------------------- the workflows in the user's own folder
#
# Each test here is about one way the folder can go wrong *without anybody noticing*: a file that is
# not loaded, a name that means two things, a placeholder that silently becomes an empty string, or
# a report that names the workflow that was asked for rather than the one that ran.
@pytest.fixture
def user_workflows(tmp_path):
    """A folder of the user's own workflows, undone afterwards.

    The registry is module-level state deliberately — the generation path and the probe both read it
    and neither has a store to ask — so a test that points it at a `tmp_path` has to point it back.
    Left alone it would send *other* tests through the default workflow for a reason that has
    nothing to do with what they are testing.
    """
    comfyui.use_folder(tmp_path / "workflows")
    try:
        yield tmp_path / "workflows"
    finally:
        comfyui.use_folder(None)


def a_graph(**inputs) -> dict:
    """The least that is a valid graph: the shape ComfyUI's own "Save (API format)" writes."""
    return {"1": {"class_type": "KSampler", "inputs": dict(inputs)}}


def write_workflow(folder, name: str, body) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (folder / f"{name}.json").write_text(text, encoding="utf-8")


def test_a_workflow_the_user_wrote_is_offered_next_to_the_one_we_ship(user_workflows):
    """Why the folder exists at all: a graph built in ComfyUI's own UI becomes a choice in this
    app's settings without anybody editing Python."""
    write_workflow(user_workflows, "my-lipsync", {"use": "video", "graph": a_graph()})
    assert comfyui.names() == ["infinite-talk", "my-lipsync", WORKFLOW, IMAGE]
    assert comfyui.workflow_of("my-lipsync")["graph"] == a_graph()
    assert comfyui.workflow_of("my-lipsync")["user"] is True


def test_a_file_that_cannot_be_used_is_named_and_never_silently_skipped(user_workflows):
    """The expensive failure: a workflow the user wrote, that the app does not load, and which
    therefore renders the *default* graph instead while the result says the render succeeded. So
    every rejection comes back with its file and a reason."""
    write_workflow(user_workflows, "broken", "{ not json")
    write_workflow(user_workflows, "no-graph", {"use": "video"})
    write_workflow(user_workflows, "bad-needs", {"use": "video", "graph": a_graph(),
                                                 "needs": "wan.safetensors"})
    write_workflow(user_workflows, "not-video", {"use": "image", "graph": a_graph()})
    write_workflow(user_workflows, "no-class", {"use": "video", "graph": {"1": {"inputs": {}}}})
    write_workflow(user_workflows, "ok", {"use": "video", "graph": a_graph()})
    got = comfyui.reload_user(force=True)
    assert got["loaded"] == ["ok"], "only the usable one is loaded"
    why = {e["file"]: e["why"] for e in got["errors"]}
    assert set(why) == {"broken.json", "no-graph.json", "bad-needs.json", "not-video.json",
                        "no-class.json"}
    for name, said in why.items():
        assert said.strip(), f"{name} was rejected with no reason"


def test_a_name_that_collides_with_a_shipped_workflow_is_an_error_not_an_override(user_workflows):
    """Two graphs answering to one name is how "it worked yesterday" starts: the dropdown lists one
    name and the render uses whichever of the two won here."""
    write_workflow(user_workflows, WORKFLOW, {"use": "video", "graph": a_graph()})
    got = comfyui.reload_user(force=True)
    assert got["loaded"] == []
    assert WORKFLOW in got["errors"][0]["why"]
    assert comfyui.workflow_of(WORKFLOW) is comfyui.WORKFLOWS[WORKFLOW], "ours is untouched"


def test_the_users_graph_gets_our_numbers_with_the_types_they_need(user_workflows):
    """`{{width}}` has to arrive an integer and `{{cfg}}` a float — a sampler takes one of each, and
    a graph that is nearly right is the thing this file is arranged to avoid."""
    write_workflow(user_workflows, "mine", {
        "use": "video", "fps": 24, "long_side_max": 1280, "steps": 12, "cfg": 6.0,
        "graph": {"1": {"class_type": "KSampler", "inputs": {
            "text": "{{prompt}}", "width": "{{width}}", "length": "{{frames}}", "seed": "{{seed}}",
            "steps": "{{steps}}", "cfg": "{{cfg}}", "prefix": "ta/{{slug}}"}}},
    })
    p = comfyui.payload_for("mine", prompt="a person / talking", ratio="16:9", seconds=3,
                            short_edge=480, seed=7)
    ins = p["graph"]["1"]["inputs"]
    assert ins["text"] == "a person / talking"
    assert ins["width"] == p["width"] and isinstance(ins["width"], int)
    assert ins["length"] == p["frames"] and isinstance(ins["length"], int)
    assert ins["seed"] == 7 and isinstance(ins["seed"], int)
    assert ins["steps"] == 12 and isinstance(ins["steps"], int)
    assert ins["cfg"] == 6.0 and isinstance(ins["cfg"], float)
    assert ins["prefix"] == "ta/a-person-talking", "a filename gets the slug, not the prompt"


def test_a_placeholder_we_cannot_fill_raises_instead_of_rendering_a_blank(user_workflows):
    """An empty string where a prompt should be renders a clip that is not the one the graph's
    author wrote, and nobody can tell from the result. Raising names the placeholder — and what we
    can fill — so the fix is obvious."""
    write_workflow(user_workflows, "greedy",
                   {"use": "video", "graph": a_graph(motion="{{camera_motion}}")})
    with pytest.raises(comfyui.VideoError) as e:
        comfyui.payload_for("greedy", prompt="x", ratio="16:9", seconds=3, short_edge=480, seed=1)
    assert "camera_motion" in str(e.value)
    assert "prompt" in str(e.value)


def test_the_payload_reports_the_workflow_that_will_run_not_the_one_asked_for():
    """`payload_for("something-else")` reported `"workflow": "something-else"` while carrying the
    default graph — a request echoed back as if it were the result, which is the mistake the
    measured-clip code elsewhere in this project exists to prevent."""
    p = comfyui.payload_for("something-else", prompt="x", ratio="16:9", seconds=3,
                            short_edge=480, seed=1)
    assert p["workflow"] == WORKFLOW, "the name that will actually run"
    assert p["asked_for"] == "something-else"
    assert "something-else" in p["note"] and WORKFLOW in p["note"]
    assert p["graph"]["37"]["inputs"]["unet_name"] == UNET, "and the graph agrees with the name"


def test_a_workflow_that_is_there_has_nothing_to_warn_about():
    assert comfyui.resolve(WORKFLOW) == (WORKFLOW, "")
    assert comfyui.resolve("") == (WORKFLOW, ""), "an empty setting is not a mistake"


async def test_the_probe_refuses_to_measure_a_workflow_that_is_not_there(user_workflows):
    """Answering "yes, it can render" about the default workflow while the provider is set to
    something else is worse than not answering: it is a green light for a job that will not run.
    The name the provider holds comes back, plus what *is* available."""
    srv = FakeComfy()
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV | {"models": ["my-digital-human"]}, client=c)
    assert ok is False
    assert "my-digital-human" in said
    assert WORKFLOW in said, "and what it does know, so the fix is one rename away"
    assert srv.paths == ["/system_stats"], "it asked what the instance is, then stopped"


async def test_the_probe_names_the_broken_file_that_explains_a_missing_workflow(user_workflows):
    """"Your workflow is not there" and "your workflow file has a typo" need different fixes, and
    this message is the only place that can tell them apart."""
    write_workflow(user_workflows, "my-digital-human", "{ not json")
    srv = FakeComfy()
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV | {"models": ["my-digital-human"]}, client=c)
    assert ok is False and "my-digital-human.json" in said


async def test_a_workflow_with_no_extra_files_is_not_reported_as_needing_three(user_workflows):
    """The shipped sentence says "the 3 file(s)" because that workflow needs three. A user's graph
    may need none, and telling them three are missing sends them looking for files that do not
    exist."""
    write_workflow(user_workflows, "nothing-to-download",
                   {"use": "video", "needs": [], "graph": a_graph()})
    srv = FakeComfy()
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV | {"models": ["nothing-to-download"]}, client=c)
    assert ok is True and "nothing-to-download" in said
    assert " 3 " not in said and "three" not in said


# ------------------------------------------- a capability declared before its weights exist
def test_a_declared_workflow_is_offered_so_the_user_can_find_out_what_it_needs():
    """The whole point of the declaration: the name is selectable, so `Test` can say which files are
    absent. A capability that exists only in a README is one nobody ever gets running."""
    assert "infinite-talk" in comfyui.names()
    row = comfyui.workflow_of("infinite-talk")
    assert row["setup"] is True and len(row["needs"]) == 6
    assert all(len(n) == 4 for n in row["needs"]), "each file says where to get it"
    assert comfyui.wants(row) == set(), "it has no graph yet, so it asks for no placeholders"


def test_a_declared_workflow_refuses_to_render_instead_of_using_another_one():
    """⛔ The failure this prevents is the expensive one. `workflow_of` falls back to the default for
    an unknown name on purpose; if a *declared* name fell through the same way, choosing the digital
    human would render wan2.2 and the result would say it succeeded."""
    with pytest.raises(comfyui.VideoError) as e:
        comfyui.payload_for("infinite-talk", prompt="x", ratio="16:9", seconds=5, short_edge=480,
                            seed=1)
    said = str(e.value)
    assert "infinite-talk" in said
    assert "wan2.1_infiniteTalk_multi_fp16.safetensors" in said, "it names the files to install"
    assert "huggingface.co" in said, "and where to get them"
    assert "API format" in said, "and how to finish the job"


async def test_the_probe_lists_every_missing_file_with_its_address():
    """Measured against a real instance shape: the nodes are all there (`comfyui-core`), so the only
    thing wrong is the six files — and "you are missing six files" is a diagnosis, not an
    instruction, unless it says where they come from."""
    # The three extra node classes present but declaring **nothing they can load**: that is the
    # shape of the real failure (ComfyUI has the nodes, the weights were never installed), and it is
    # the branch that has to name the files rather than the nodes.
    schemas = object_info()
    for cls, field in (("LoraLoaderModelOnly", "lora_name"), ("ModelPatchLoader", "name"),
                       ("AudioEncoderLoader", "audio_encoder_name")):
        schemas[cls] = {"input": {"required": {field: [[], {"tooltip": ""}]}}}
    srv = FakeComfy(info=schemas)
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV | {"models": ["infinite-talk"]}, client=c)
    assert ok is False
    for _cls, _field, name, url in comfyui.setup_needs("infinite-talk"):
        assert name in said, f"{name} was not named"
        assert url in said, f"{name} came without its address"
    assert "wan2.2-ti2v-5b" not in said, "the wan2.2 hint does not belong on this workflow"


async def test_refreshing_models_offers_the_users_workflows_too(user_workflows):
    """A workflow the user added that the dropdown never offers is one they cannot pick — and the
    tool would then fall back to the default without anybody having asked for that."""
    write_workflow(user_workflows, "my-lipsync", {"use": "video", "graph": a_graph()})
    ids = [m["id"] for m in await discovery.fetch_models(PROV)]
    assert "my-lipsync" in ids and WORKFLOW in ids


# ------------------------------------------------------------------ the envelope
async def test_submit_sends_the_graph_and_returns_the_prompt_id():
    srv = FakeComfy()
    async with srv.client() as c:
        pid = await comfyui.submit(PROV, {"graph": {"1": {"class_type": "X", "inputs": {}}}},
                                   client=c)
    assert pid == "pid-1"
    assert srv.paths == ["/prompt"]
    body = srv.bodies[0]
    assert body["prompt"] == {"1": {"class_type": "X", "inputs": {}}}
    assert body["client_id"], "ComfyUI wants one, and it is what makes a lost job recoverable"


async def test_a_graph_that_is_refused_says_which_node():
    """Refused at submit, not polled: the instance has already said what is wrong, and waiting for a
    job that will never start would turn a clear error into 'gave up after 600s'."""
    srv = FakeComfy(submit={"error": {"type": "prompt_outputs_failed_validation",
                                     "message": "Prompt outputs failed validation"},
                            "node_errors": {"37": {"errors": [
                                {"type": "value_not_in_list",
                                 "message": "Value not in list: unet_name"}]}}})
    async with srv.client() as c:
        with pytest.raises(comfyui.VideoError) as e:
            await comfyui.submit(PROV, {"graph": {}}, client=c)
    assert "unet_name" in str(e.value)


async def test_a_dropped_connection_at_submit_says_comfyui_may_not_be_running():
    def boom(request):
        raise httpx.ConnectError("Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(comfyui.VideoError) as e:
            await comfyui.submit(PROV, {"graph": {}}, client=c)
    assert "Is it running?" in str(e.value)


async def test_status_maps_comfyuis_words_onto_the_loops_own():
    """`completed` / `failed` / anything else — the shape `video._generate`'s loop already reads."""
    srv = FakeComfy()
    async with srv.client() as c:
        raw, first = await comfyui.status_of(PROV, "pid-1", client=c)
        assert raw == "running", "the first poll is not finished yet"
        assert "outputs" not in first, "a job still running has saved nothing yet"
        done, detail = await comfyui.status_of(PROV, "pid-1", client=c)
    assert done == "completed" and detail["outputs"]["58"]["images"][0]["filename"] == \
        "clip_00001_.mp4"


async def test_a_failed_render_reports_the_exception_not_just_the_word():
    """A ComfyUI failure keeps its reason in `messages`, per node. 'The server reported failure'
    would leave the user with nothing to act on."""
    srv = FakeComfy(history={"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "VAELoader", "node_id": 39,
                             "exception_message": "Cannot load wan2.2_vae.safetensors"}]],
    }})
    async with srv.client() as c:
        raw, detail = await comfyui.status_of(PROV, "pid-1", client=c)
    assert raw == "failed"
    assert "Cannot load wan2.2_vae.safetensors" in detail["error"]


async def test_a_job_the_instance_never_heard_of_keeps_waiting():
    """A restart loses the queue. Calling that failure would throw away render time that may well
    still be running; the deadline is what ends the wait."""
    srv = FakeComfy(missing=True)
    async with srv.client() as c:
        assert (await comfyui.status_of(PROV, "pid-1", client=c))[0] == "running"


def test_the_link_is_a_path_on_the_instance_not_a_url_from_it():
    detail = completed("clip_00001_.mp4", subfolder="team-agent")
    link = comfyui.link_of(detail)
    assert link.startswith("view?")
    assert "filename=clip_00001_.mp4" in link and "subfolder=team-agent" in link
    assert "type=output" in link


def test_a_picture_the_workflow_also_saved_is_not_mistaken_for_the_clip():
    """The same node can save a preview beside the clip, and `outputs` nests both. Picking the first
    descriptor found would hand the user a still frame."""
    detail = completed("clip.mp4", extra={"9": {"images": [
        {"filename": "preview_00001_.png", "subfolder": "", "type": "output"}]}})
    assert "clip.mp4" in comfyui.link_of(detail)


def test_nothing_saved_is_an_empty_link_rather_than_a_guess():
    assert comfyui.link_of({"status": {}, "outputs": {}}) == ""


async def test_video_can_drive_the_whole_run_through_a_fake_comfyui(tmp_path):
    """One pass of the real `video.generate`: submit, poll, fetch the link, download."""
    srv = FakeComfy()

    async def run():
        p = comfyui.payload_for(WORKFLOW, prompt="一只红色的风筝", ratio="16:9", seconds=5,
                                short_edge=480, seed=3)
        return await video.generate(PROV, p, workspace=tmp_path, max_bytes=8 << 20,
                                    deadline_s=30, client=srv.client())

    got = await run()
    assert srv.paths[0] == "/prompt"
    assert srv.paths.count("/history/pid-1") >= 2, "it polled until the job was finished"
    assert srv.paths[-1] == "/view"
    assert got["path"].read_bytes() == MP4
    assert got["path"].parent.name == "video", "clips land in the group workspace's video/ folder"
    assert got["id"] == "pid-1" and got["bytes"] == len(MP4)


async def test_the_view_url_is_built_from_our_address_plus_what_the_instance_reported(tmp_path):
    """Nothing the instance returns is followed as a URL — there is no object store here, and the
    key must not leave for one."""
    srv = FakeComfy(filename="clip_00001_.mp4", subfolder="team-agent")

    async def run():
        p = comfyui.payload_for(WORKFLOW, prompt="x", ratio="16:9", seconds=2, short_edge=320, seed=1)
        return await video.generate(PROV | {"api_key": "cs-key"}, p, workspace=tmp_path,
                                    max_bytes=8 << 20, deadline_s=30, client=srv.client())

    await run()
    assert srv.paths[-1] == "/view"
    assert srv.queries[-1] == {"filename": ["clip_00001_.mp4"], "subfolder": ["team-agent"],
                               "type": ["output"]}
    assert srv.auth[-1] == ("/view", "Bearer cs-key"), \
        "the key belongs to this instance, so it is sent — unlike an object-store link"


async def test_a_clip_over_the_cap_is_refused_before_it_is_saved(tmp_path):
    srv = FakeComfy(clip=b"\x00" * 8192)

    async def run():
        p = comfyui.payload_for(WORKFLOW, prompt="x", ratio="16:9", seconds=2, short_edge=320, seed=1)
        return await video.generate(PROV, p, workspace=tmp_path, max_bytes=1024, deadline_s=30,
                                    client=srv.client())

    with pytest.raises(video.VideoError) as e:
        await run()
    assert "MB" in str(e.value)
    saved = list((tmp_path / "video").glob("*")) if (tmp_path / "video").exists() else []
    assert saved == [], f"a refused clip must not leave anything behind, found {saved}"


# ------------------------------------------------------------------ the probe
async def test_the_probe_names_the_file_that_is_missing():
    """The most likely reason a render never starts is a checkpoint the instance does not have.
    'Reachable' would be true and useless."""
    srv = FakeComfy(info=object_info(vae="some_other_vae.safetensors"))
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is False and VAE in said, "the answer has to name the file the user must go and get"
    assert "some_other_vae" not in said, "and not the one it does have"


async def test_the_probe_names_a_missing_node_class():
    srv = FakeComfy(info=object_info(without=("VAELoader",)))
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is False and "VAELoader" in said


async def test_the_probe_says_yes_only_when_the_instance_can_actually_render():
    srv = FakeComfy()
    async with srv.client() as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is True and "0.35.0" in said and WORKFLOW in said


async def test_the_probe_tells_the_three_kinds_of_not_there_apart():
    """Not running, the wrong address, and a proxy refusing the key are three different things to
    fix, and the probe is the only place the user can find that out without waiting for a render."""
    def refuse(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is False and "rejected the key" in said

    not_comfy = FakeComfy(stats_status=500)

    async with not_comfy.client() as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is False and "root address" in said

    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(404, text="<html>nope</html>"))) as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is False and "404" in said

    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"hello": "world"}))) as c:
        ok, said = await comfyui.probe(PROV, client=c)
    assert ok is False and "ComfyUI version" in said


async def test_video_probe_routes_to_the_comfyui_one():
    srv = FakeComfy()
    assert (await video.probe(PROV, client=srv.client()))[0] is True


# ------------------------------------------------------------------ discovery
async def test_refreshing_models_for_comfyui_asks_the_instance_nothing():
    """ComfyUI has no model catalogue in this sense: what the user picks is a workflow we ship. A
    refresh must therefore be answered from the built-in table — pinned with a transport that
    *fails*, so a later change that starts really asking cannot pass this test."""
    def boom(request):       # pragma: no cover - reaching this is the failure
        raise AssertionError(f"discovery asked ComfyUI for models: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        got = await discovery.fetch_models(PROV | {"base_url": "http://127.0.0.1:8188"}, client=c)
    both = [*media.BUILTIN_MEDIA_MODELS["comfyui"]["video"],
            *media.BUILTIN_MEDIA_MODELS["comfyui"]["image"]]
    assert [m["id"] for m in got] == sorted(both)
    assert {m["mode"] for m in got} == {"video", "image"}, \
        "and each is marked for what it is — one instance answers for both families"


# ------------------------------------------------------------------ through the tool
def test_the_shipped_preset_is_local_and_offers_the_workflow(store):
    prov = store.add_provider_from_preset("comfyui")
    assert prov["kind"] == "comfyui" and prov["is_local"] is True
    assert prov["base_url"] == "http://127.0.0.1:8188"
    assert [m["model_name"] for m in store.list_models() if m["provider_id"] == prov["id"]] == []
    listed = [m["model_name"] for m in store.models_of_use(prov["id"], "video")]
    assert WORKFLOW in listed
    assert "infinite-talk" in listed, "a declared capability is offered too, so it can be diagnosed"


def test_a_comfyui_client_needs_no_key_to_be_considered_usable(store):
    """`router.has_credentials` would otherwise skip a provider that legitimately has none."""
    prov = store.add_provider_from_preset("comfyui")
    row = next(p for p in store.list_providers() if p["id"] == prov["id"])
    assert row["is_local"] is True
    assert (row.get("api_key") or "") == ""


async def test_the_tool_refuses_a_keyframe_instead_of_dropping_it(store, make_router):
    """This workflow has no image input at all — the shipped graph fills a latent with noise, so
    there is nowhere to put one. Dropping the frame silently would make a member conclude the model
    ignored it; the refusal names the workflow, what it does take, and what would take it."""
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 20,
                           "video_max_mb": 8, "video_model": WORKFLOW})
    store.add_provider_from_preset("comfyui")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    out = await orch.toolhub.call(
        ctx, "generate_video", {"prompt": "x", "first_frame": "a.png"}, None)
    assert out.ok is False and out.files == []
    assert "nowhere to put a first frame" in out.text
    assert WORKFLOW in out.text, "which workflow is the thing the user has to change"
    assert "{{image}}" in out.text, "and what a graph would need in order to accept one"


async def test_a_users_graph_with_an_image_slot_gets_the_picture_uploaded(store, make_router,
                                                                         user_workflows):
    """The other half of the refusal above: a graph that *does* carry `{{image}}` has the file
    handed to the instance and its name substituted in — which is what makes image-to-video (and so
    a talking head) something the user can have by writing a file, not by waiting for us.

    The upload is asserted on the request, because that is where the failure would be invisible: a
    graph pointing at a filename the instance does not have is accepted and then fails after the
    wait."""
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    write_workflow(user_workflows, "my-i2v", {
        "use": "video", "fps": 24, "needs": [],
        "graph": {"1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
                  "2": {"class_type": "Wan22ImageToVideoLatent",
                        "inputs": {"start_image": ["1", 0], "width": "{{width}}"}}},
    })
    comfyui.reload_user(force=True)
    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 20,
                           "video_max_mb": 8, "video_model": "my-i2v"})
    store.add_provider_from_preset("comfyui")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ws = store.workspace_dir(g["id"])
    (ws / "portrait.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/upload/image"):
            return httpx.Response(200, json={"name": "portrait-abc.png", "subfolder": "",
                                             "type": "input"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"prompt_id": "pid-1", "node_errors": {}})
        if request.url.path.startswith("/history/"):
            return httpx.Response(404)
        return httpx.Response(404)

    import app.net as _net
    real = _net.client
    _net.client = lambda *_a, **_k: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0],
                                         connect=False)
        await orch.toolhub.call(
            ctx, "generate_video", {"prompt": "x", "first_frame": "portrait.png"}, None)
    finally:
        _net.client = real

    uploads = [r for r in seen if r.url.path.endswith("/upload/image")]
    assert uploads, "the picture was never handed to the instance"
    assert b"portrait" in uploads[0].content or b"PNG" in uploads[0].content
    # The header that made the real instance answer 400 while this test was green: the json routes'
    # `Content-Type: application/json` overrides the multipart type httpx would set, so the body is
    # read as JSON and the file in it is never found. A mock transport does not parse multipart, so
    # nothing else here would have noticed.
    assert "json" not in (uploads[0].headers.get("content-type") or ""), \
        "the upload must let httpx set the multipart content type"
    assert "multipart/form-data" in (uploads[0].headers.get("content-type") or "")
    posts = [r for r in seen if r.url.path.endswith("/prompt")]
    assert posts, "the job was queued after the upload"
    body = json.loads(posts[0].content)
    assert body["prompt"]["1"]["inputs"]["image"] == "portrait-abc.png", \
        "the graph points at the name the instance gave back, not the one we sent"
    assert body["prompt"]["2"]["inputs"]["width"], "and our numbers are still substituted"


async def test_the_tool_refuses_extra_reference_material(store, make_router):
    from tests.conftest import FakeLLM
    from tests.test_collab import setup

    store.update_settings({"video_enabled": True, "video_timeout": 30, "video_max_seconds": 20,
                           "video_max_mb": 8, "video_model": WORKFLOW})
    store.add_provider_from_preset("comfyui")
    orch, g = setup(store, make_router, FakeLLM(default="好"))
    ctx = await orch.toolhub.context(store.get_group(g["id"]), store.list_agents()[0], connect=False)
    out = await orch.toolhub.call(
        ctx, "generate_video", {"prompt": "x", "reference_images": ["https://a/b.png"]}, None)
    assert out.ok is False and "does not take reference pictures" in out.text


def test_a_comfyui_model_becomes_a_generating_member(store):
    """The same path every other generator takes: adding the model is what creates the member."""
    prov = store.add_provider_from_preset("comfyui")
    model = next(m for m in store.models_of_use(prov["id"], "video"))
    agent = store.ensure_model_agent(model["id"])
    assert agent is not None and agent["origin"] == media.MEDIA_ORIGIN
    target = media.member_target(store, agent)
    assert target and target["use"] == "video" and target["provider"]["kind"] == "comfyui"


# ------------------------------------------------------------------ a real socket
class _Stub(BaseHTTPRequestHandler):
    """A ComfyUI-shaped server on a real port.

    A mocked transport proves our request objects; it cannot prove the path joining, the real header
    names, or that the bytes on the wire are the bytes that land in the workspace. This can.
    """

    clip = MP4
    seen: list[tuple[str, str]] = []

    def log_message(self, *a):       # keep the test output clean
        pass

    def _send(self, body: bytes, ctype: str = "application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        type(self).seen.append(("GET", self.path))
        if path == "/system_stats":
            self._send(json.dumps({"system": {"comfyui_version": "0.35.0"}}).encode())
        elif path == "/object_info":
            self._send(json.dumps(object_info()).encode())
        elif path == "/history/pid-socket":
            self._send(json.dumps({"pid-socket": completed("socket.mp4", "sub")}).encode())
        elif path == "/view":
            self._send(self.clip, "video/mp4")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        # The parsed body, not a truncated rendering of it: the point of this server is that the
        # graph which actually went over the socket can be inspected afterwards.
        type(self).seen.append(("POST", body))
        self._send(json.dumps({"prompt_id": "pid-socket"}).encode())


async def test_a_real_socket_run_saves_the_bytes_the_instance_served(tmp_path):
    _Stub.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        prov = PROV | {"base_url": f"http://127.0.0.1:{port}", "api_key": ""}
        p = comfyui.payload_for(WORKFLOW, prompt="socket", ratio="16:9", seconds=2, short_edge=320,
                                seed=11)

        async def run():
            return await video.generate(prov, p, workspace=tmp_path, max_bytes=8 << 20,
                                        deadline_s=30)

        got = await run()
    finally:
        srv.shutdown()
    assert got["path"].read_bytes() == MP4
    assert got["bytes"] == len(MP4)
    methods = [m for m, _ in _Stub.seen]
    assert methods[0] == "POST", "the graph goes first"
    assert "GET" in methods[1:], "then it polls"
    assert any(m == "GET" and "/view" in str(d) for m, d in _Stub.seen), \
        "and fetches the file it was told about"
    graph = _Stub.seen[0][1]
    assert "prompt" in graph and graph["prompt"]["37"]["inputs"]["unet_name"] == UNET
    assert graph["prompt"]["3"]["inputs"]["seed"] == 11, "the caller's seed reached the graph"


async def test_the_real_socket_probe_answers_over_http(tmp_path):
    _Stub.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ok, said = await video.probe(PROV | {"base_url": f"http://127.0.0.1:{srv.server_address[1]}"})
    finally:
        srv.shutdown()
    assert ok is True and "0.35.0" in said
    assert [m for m, _ in _Stub.seen] == ["GET", "GET"], "system_stats then object_info, nothing else"


# ------------------------------------------------------------------ the still-picture family
IMAGE = "z-image-turbo"
IMAGE_UNET = "z_image_turbo_bf16.safetensors"
IMAGE_CLIP = "qwen_3_4b.safetensors"
IMAGE_VAE = "ae.safetensors"

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 512


def test_the_image_graph_is_the_blueprint_s_own_arrangement():
    """Every value here is read off ComfyUI's `blueprints/Text to Image (Z-Image-Turbo).json`, so a
    later edit that "tidies" one of them has to argue with the blueprint rather than with a memory.
    """
    g = comfyui.image_graph(comfyui.WORKFLOWS[IMAGE], prompt="a red cube", width=1024, height=1024,
                            seed=5, prefix="p")
    assert g["28"]["inputs"] == {"unet_name": IMAGE_UNET, "weight_dtype": "default"}
    # ⚠️ The encoder is `lumina2`. The wrong string is refused by name, and the candidate list the
    # refusal prints looks like it contains the right thing — which is exactly how this gets "fixed"
    # into a broken graph.
    assert g["30"]["inputs"] == {"clip_name": IMAGE_CLIP, "type": "lumina2", "device": "default"}
    assert comfyui.WORKFLOWS[IMAGE]["clip_type"] == "lumina2"
    assert g["29"]["inputs"]["vae_name"] == IMAGE_VAE
    # Z-Image's latent is SD3-shaped; `EmptyLatentImage` would be the 4-channel one.
    assert g["13"]["class_type"] == "EmptySD3LatentImage"
    assert g["11"]["inputs"]["shift"] == 3.0
    ks = g["3"]["inputs"]
    assert (ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"], ks["denoise"]) == \
        (8, 1.0, "res_multistep", "simple", 1.0), "the turbo checkpoint's own numbers"
    assert ks["negative"] == ["33", 0] and g["33"]["class_type"] == "ConditioningZeroOut"
    assert g["33"]["inputs"]["conditioning"] == ["27", 0], \
        "the negative is the positive conditioning zeroed out — there is no negative prompt here"
    assert g["9"]["class_type"] == "SaveImage", \
        "the blueprint hands its image back to the canvas; the file is the part we add"
    assert "a red cube" in json.dumps(g)


def test_a_size_is_rounded_the_way_the_model_needs_and_refused_when_it_is_readable():
    p = comfyui.image_payload_for(IMAGE, prompt="x", size="1024x1536", seed=0)
    assert (p["width"], p["height"]) == (1024, 1536) and p["size"] == "1024x1536"
    # 0 means "unspecified" in the tool schema, and KSampler would take it literally.
    assert p["seed"] > 0 and p["graph"]["3"]["inputs"]["seed"] == p["seed"]
    assert comfyui.image_payload_for(IMAGE, prompt="x", size="1024x1024", seed=9)["seed"] == 9
    # Both sides a multiple of 16, and the long side capped at what the checkpoint was built for.
    w, h = comfyui.image_size("1000x1000", long_side_max=1536)
    assert w % 16 == 0 and h % 16 == 0
    w, h = comfyui.image_size("4096x2048", long_side_max=1536)
    assert max(w, h) <= 1536 and w > h, "the aspect is kept, the long side is what gives"
    for bad in ("", "big", "1024", "1024x"):
        with pytest.raises(comfyui.VideoError):
            comfyui.image_size(bad, long_side_max=1536)


def test_each_family_refuses_the_other_s_workflow_by_name():
    """The two tools share one instance and one dropdown mechanism, so the only thing keeping a
    drawing workflow out of the video tool is an explicit refusal — and a KeyError on `frames`
    would read as a broken build instead of as a wrong pick."""
    with pytest.raises(comfyui.VideoError) as e:
        comfyui.payload_for(IMAGE, prompt="x", ratio="16:9", seconds=5, short_edge=480, seed=1)
    assert "Image generation" in str(e.value)
    with pytest.raises(comfyui.VideoError) as e:
        comfyui.image_payload_for(WORKFLOW, prompt="x", size="1024x1024", seed=1)
    assert "video tool" in str(e.value)
    # A name nobody ships is named as such rather than being described as the wrong family: an
    # unknown name resolves to the *default* workflow first, and "wan2.2-ti2v-5b renders a clip"
    # is a confusing thing to be told when what was typed does not exist.
    with pytest.raises(comfyui.VideoError) as e:
        comfyui.image_payload_for("does-not-exist", prompt="x", size="1024x1024", seed=1)
    assert "does-not-exist" in str(e.value)
    # An unset name never reaches the graph builder: `comfy_payload` refuses it with the sentence
    # the tool shows, before anything is submitted.
    with pytest.raises(imagegen.ImageError) as e:
        imagegen.comfy_payload("x", model="", size="1024x1024")
    assert "z-image-turbo" in str(e.value)


async def test_a_still_goes_through_the_same_submit_poll_fetch_as_a_clip(monkeypatch):
    """`imagegen` owns no transport of its own for this kind: it drives `comfyui.submit` /
    `status_of` / `link_of`, the same three calls the clip path makes, then fetches the file."""
    monkeypatch.setattr(imagegen, "POLL_SECONDS", 0.01)
    srv = FakeComfy(clip=PNG, filename="still_00001_.png")
    payload = imagegen.comfy_payload("a red cube", model=IMAGE, size="1024x1024")
    got = await imagegen.generate(PROV, payload, max_bytes=4 * 1024 * 1024, deadline_s=30,
                                  client=srv.client())
    assert got["data"] == PNG and got["size"] == len(PNG)
    assert srv.paths[0] == "/prompt" and "/history/" in "".join(srv.paths)
    assert srv.paths[-1] == "/view", "and it reads the file the instance reported"
    assert srv.queries[-1]["filename"] == ["still_00001_.png"]
    submitted = srv.bodies[-1]["prompt"]
    assert submitted["28"]["inputs"]["unet_name"] == IMAGE_UNET
    assert "a red cube" in json.dumps(submitted)


async def test_a_still_that_fails_is_reported_with_comfyui_s_own_node_error(monkeypatch):
    monkeypatch.setattr(imagegen, "POLL_SECONDS", 0.01)
    srv = FakeComfy(history={
        "status": {"status_str": "error",
                   "messages": [["execution_error", {"node_id": 3, "node_type": "KSampler",
                                                     "exception_message": "out of memory"}]]}})
    payload = imagegen.comfy_payload("x", model=IMAGE, size="1024x1024")
    with pytest.raises(imagegen.ImageError) as e:
        await imagegen.generate(PROV, payload, max_bytes=1024, deadline_s=30, client=srv.client())
    assert "KSampler" in str(e.value) and "out of memory" in str(e.value)


async def test_testing_a_drawing_provider_asks_about_the_workflow_being_tested():
    """One ComfyUI answers for both families and the probe keys off the provider's own `models`
    list — so a test that did not name the workflow would be answered about whichever name happens
    to come first, and would say "ready" about a workflow that was never checked."""
    srv = FakeComfy(info=object_info(unet=IMAGE_UNET, clip=IMAGE_CLIP, vae=IMAGE_VAE,
                                     workflow=IMAGE))
    ok, said = await imagegen.probe(PROV, IMAGE, client=srv.client())
    assert ok is True and IMAGE in said
    assert WORKFLOW not in said, "the clip workflow is not what was asked about"
    assert "clip" not in said, "and a drawing workflow is not reported as one"

    # A node that is absent and a *file* that is absent are two different diagnoses, and the second
    # is the one the user can act on — so the second fake keeps the node and leaves its file list
    # without the VAE the workflow names.
    missing = FakeComfy(info=object_info(unet=IMAGE_UNET, clip=IMAGE_CLIP,
                                         vae="some_other_vae.safetensors", workflow=IMAGE),
                        stats={"system": {"comfyui_version": "0.35.0"}})
    ok, said = await imagegen.probe(PROV, IMAGE, client=missing.client())
    assert ok is False and IMAGE_VAE in said, "and it names the file that is actually missing"

    old = FakeComfy(info=object_info(workflow=IMAGE, without=("ConditioningZeroOut",)))
    ok, said = await imagegen.probe(PROV, IMAGE, client=old.client())
    assert ok is False and "ConditioningZeroOut" in said, \
        "a node this install does not have is named as a node, not as a missing file"
