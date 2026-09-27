"""Video generation through a ComfyUI running on your own machine (kind `comfyui`).

The other three video providers are services that take a prompt. This one is a **graph runner**,
and that difference is the whole shape of this file:

    POST {base}/prompt                     {"prompt": <api graph>, "client_id": …} -> {"prompt_id": …}
    GET  {base}/history/{prompt_id}         -> {prompt_id: {"status": …, "outputs": …}}
    GET  {base}/view?filename=…&subfolder=…&type=output  -> the bytes
    GET  {base}/system_stats, {base}/object_info         -> what it is and what it has

So there is no `model` in the request — there is a **workflow**, and the models are named inside
it. The thing the user picks in our settings is therefore a workflow — one we ship (`WORKFLOWS`) or
one of their own (`<data dir>/workflows/*.json`) — and the checkpoint/encoder/VAE file names live in
that workflow's `needs`. That is also why the probe can
be more useful than "the server answered": it can name the file that is missing.

Why this exists at all: every other video provider here bills per clip. ComfyUI bills nothing —
the cost is your own electricity — which is the point for a user who does not want to pay per
render. What it cannot do is invent anything: a workflow only runs if the instance has the files it
names and the nodes it uses. Saying "it works" without those would be the usual lie, so `probe`
answers all three questions and every failure names what is missing.

`wan2.2-ti2v-5b` is the workflow we ship. It is the text-to-video graph of ComfyUI's own
`video_wan2_2_5B_ti2v` template — 24 fps, soundless, and **expensive in minutes**: this docstring
used to claim "roughly a minute of Mac GPU time for five seconds of footage", which is wrong by more
than an order of magnitude. Measured on this machine 2026-09-26, from the tool records' own `ms`
field: 5 s → **1647 s**, 6 s → **827 s**, 6 s → **807 s**, and 2 s → 127 s. The shipped graph is also
16:9-only — it takes no `aspect_ratio` — so a 9:16 film gets letterboxed footage out of it.

That is why `animate.make_animation` exists and why the planning prompt routes schematic work to it:
it draws the same mechanisms here in **1.9 s** for 6 s of 1080×1920. Reach for diffusion when the
shot needs photographic motion, not when it needs to *explain* something. Anything else is a
**file** rather than a code change: one JSON per workflow in
`<data dir>/workflows/`, carrying that graph in API format with `{{prompt}}`, `{{width}}` and the
rest where our numbers go. That is what makes a lip-sync, digital-human or image-to-video graph
something the user *has* instead of something they are owed — see "the user's own workflows" below
for the three rules it follows and why each of them is about not rendering the wrong thing.

⚠️ The frame count is not a free parameter. Wan's latent wants `4n+1` frames and its VAE wants
both sides a multiple of 16, so `frames_for` and `size_for` do that arithmetic here instead of
sending a number the instance will reject after the user has waited for it.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx

from . import i18n, media

KIND = "comfyui"

# ComfyUI's own routes. Nothing here is invented: these are the four the server documents for
# submitting a graph, reading its result, fetching a saved file and asking what it has.
PROMPT_ROUTE = "prompt"
HISTORY_ROUTE = "history/{pid}"
VIEW_ROUTE = "view"
OBJECT_INFO_ROUTE = "object_info"
SYSTEM_STATS_ROUTE = "system_stats"
# The only upload route ComfyUI registers — there is no `/upload/audio`. That is not a shortcut on
# our side: the server's own handler writes whatever bytes it is handed without inspecting them
# (see `image_upload` in ComfyUI's `server.py`), and `LoadAudio` reads the same `input` folder. A
# guessed `/upload/audio` would be a 404, and a `.wav` sent here arrives intact.
UPLOAD_ROUTE = "upload/image"
# A keyframe or a voice sample is a few MB. Past this it is not material any graph here wants.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# What one render costs in requests. The graph is small; the wait is not.
SUBMIT_TIMEOUT = 30.0
STATUS_TIMEOUT = 30.0
# `object_info` on a full ComfyUI install is megabytes — every node class on the instance — so the
# probe gets its own, longer budget than a poll would.
INTROSPECT_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 300.0
UPLOAD_TIMEOUT = 120.0      # a keyframe or a voice sample, over the LAN or over a tunnel

# The negative prompt ComfyUI's own template ships for these models. It is Chinese because that is
# what the template carries and what umt5 was tuned on; the encoder reads either language, and a
# negative prompt is not user-visible text, so it stays as measured rather than translated.
NEGATIVE = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
            "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
            "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
            "杂乱的背景，三条腿，背景人很多，倒着走")

# Saved-file extensions we will hand back. `assets` is where ComfyUI's `SaveVideo` node puts the
# clip (`{"images": [{"filename": "…mp4", "subfolder": …, "type": "output"}]}` — the key is
# `images` and the entry carries `"animated": true`), and `gifs` is where its older save nodes put
# theirs; both are walked rather than one being assumed.
_VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov")


# ------------------------------------------------------------------ the workflows we ship
#
# One row per workflow, and the row is the contract: a workflow that runs somewhere else names
# different files, so the file names cannot live in the graph builder. `needs` is what makes the
# probe able to say "your instance is missing wan2.2_vae.safetensors" instead of "unreachable".
#
# ⚠️ Only what this graph actually has. There is no duration knob on the model, no resolution
# ladder, no audio track and no `seed` on any node other than `KSampler` — so none of those is
# offered as if it existed.
WORKFLOWS: dict[str, dict] = {
    "wan2.2-ti2v-5b": {
        "use": "video",
        "needs": (
            ("UNETLoader", "unet_name", "wan2.2_ti2v_5B_fp16.safetensors"),
            ("CLIPLoader", "clip_name", "umt5_xxl_fp16.safetensors"),
            ("VAELoader", "vae_name", "wan2.2_vae.safetensors"),
        ),
        "fps": 24,
        # ComfyUI's template runs 1280x704 for 121 frames at 20 steps. 1280 is the long side both
        # the released checkpoint and the template are built around; painting more than that is
        # where the 5B model starts repeating itself rather than adding detail.
        "long_side_max": 1280,
        "steps": 20,
        "cfg": 5.0,
        # uni_pc diverges on Apple Silicon for 25+ frames at CFG 5. Euler was verified
        # with the same prompt, seed and 848x480 dimensions; see ComfyUI issue #15921.
        "sampler": "euler",
    },
}

DEFAULT_WORKFLOW = "wan2.2-ti2v-5b"

# A capability whose graph is **not here yet**, declared anyway. Why this is not a lie: the nodes it
# needs are part of ComfyUI itself (`WanInfiniteTalkToVideo`, `AudioEncoderLoader`, … — verified
# against a running 0.35.0), so the only thing missing is the weights. Declaring it means the
# settings dropdown offers it, the probe **names the six files** that are absent, and a render
# selected for it refuses with that list rather than quietly using another workflow.
#
# ⚠️ It must never render. `payload_for` refuses a `setup` row outright, because the alternative —
# falling back to the default graph — is a clip that looks finished and is not the one asked for.
SETUP_WORKFLOWS: dict[str, dict] = {
    "infinite-talk": {
        "use": "video",
        "setup": True,
        "title": "InfiniteTalk (talking head, needs weights)",
        "note": "Photo + a recording of someone speaking → a clip of them talking, Chinese audio "
                "encoder included. The nodes are part of ComfyUI itself; six weight files are not "
                "on this machine yet, and `Test` names them. After installing them, open "
                "ComfyUI's own `video_wan2_1_infinitetalk` template, save it in **API format**, and "
                "drop the graph into a workflow file of your own.",
        # (node class, input field, file name, where to get it) — the field names are the ones the
        # running instance reports, not the order the template's widgets happen to be in.
        "needs": (
            ("UNETLoader", "unet_name", "Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors",
             "https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/"
             "Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors"),
            ("CLIPLoader", "clip_name", "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
             "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/"
             "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
            ("VAELoader", "vae_name", "Wan2_1_VAE_bf16.safetensors",
             "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_bf16.safetensors"),
            ("LoraLoaderModelOnly", "lora_name",
             "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
             "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/"
             "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"),
            ("ModelPatchLoader", "name", "wan2.1_infiniteTalk_multi_fp16.safetensors",
             "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/"
             "model_patches/wan2.1_infiniteTalk_multi_fp16.safetensors"),
            ("AudioEncoderLoader", "audio_encoder_name", "wav2vec2-chinese-base_fp16.safetensors",
             "https://huggingface.co/Kijai/wav2vec2_safetensors/resolve/main/"
             "wav2vec2-chinese-base_fp16.safetensors"),
        ),
    },
}

# ------------------------------------------------------------------ music (ACE-Step 1.5)
#
# The same bargain as the table above, for audio: the nodes are **ComfyUI's own**
# (`comfy_extras/nodes_ace.py`), so what has to be installed is four weight files and nothing else.
# That is worth stating because the obvious assumption is wrong — a music generator "must need a
# custom node" — and the way to know is to ask the running instance, not the documentation. (This
# machine had no ACE-Step node at all until it moved to 0.35, which is why `music_missing` asks
# `object_info` rather than looking in a folder.)
#
# The graph below is ComfyUI's own `blueprints/Text to Audio (ACE-Step 1.5).json`, flattened out of
# its subgraph form into the API format the instance accepts, plus the `SaveAudioMP3` the blueprint
# leaves to whoever calls it. **Every widget value and every link was read off that blueprint**, and
# the combo values were checked against the running instance (`timesignature` is a *string*,
# `language` includes `zh`, `quality` is V0/128k/320k). The only things this adds are the words, the
# length and the seed.
MUSIC_DEFAULT_SECONDS = 60.0
MUSIC_MAX_SECONDS = 300.0
MUSIC_TURBO_STEPS = 8            # what the blueprint ships for the turbo checkpoint
# Both text encoders are required — the blueprint loads them through one `DualCLIPLoader`, which is
# why 7.8 GB of the 13.7 GB total is not optional. Addresses point at the mirror because
# huggingface.co itself answers 502 from this machine while hf-mirror.com serves the same bytes.
MUSIC_FILES: tuple[tuple[str, str, str, str], ...] = (
    # (node class, input field, file, which of ComfyUI's model folders it goes in)
    ("UNETLoader", "unet_name", "acestep_v1.5_turbo.safetensors", "diffusion_models"),
    ("DualCLIPLoader", "clip_name1", "qwen_0.6b_ace15.safetensors", "text_encoders"),
    ("DualCLIPLoader", "clip_name2", "qwen_4b_ace15.safetensors", "text_encoders"),
    ("VAELoader", "vae_name", "ace_1.5_vae.safetensors", "vae"),
)
_MUSIC_MIRROR = ("https://hf-mirror.com/Comfy-Org/ace_step_1.5_ComfyUI_files/resolve/main/"
                 "split_files")


def music_supported(schemas: dict) -> bool:
    """Whether this instance has the ACE-Step nodes at all. False means "your ComfyUI is too old",
    which is a different sentence from "a file is missing\" — and the two have different fixes."""
    return "TextEncodeAceStepAudio1.5" in (schemas or {})


def music_missing(schemas: dict) -> list[str]:
    """Which of the four weight files this instance cannot see.

    Asked of the instance rather than of a folder: ComfyUI builds each loader's list from the
    folders it was *started* with, so this answers "can it run" instead of "did I put a file
    somewhere". `_choices` returns `[]` both for a missing file and for a node that does not exist,
    which is why `music_supported` is asked first.
    """
    out: list[str] = []
    for cls, field, name, _folder in MUSIC_FILES:
        have = _choices(schemas, cls, field)
        if not have or name not in have:
            out.append(name)
    return out


def music_diagnosis(schemas: dict) -> str:
    """Why music cannot be made yet, naming every file and where to get it.

    A bare list of four file names is not an instruction, so each one carries its folder and its
    address. The alternative to this paragraph is a tool that fails with a ComfyUI traceback.
    """
    if not music_supported(schemas):
        return i18n.pick_now(
            "This ComfyUI has no ACE-Step nodes, so it cannot make music. The nodes ship with "
            "ComfyUI itself (`comfy_extras/nodes_ace.py`) — this instance predates them, so "
            "updating ComfyUI is what fixes it.",
            "这套 ComfyUI 里没有 ACE-Step 节点,做不了音乐。这些节点是 ComfyUI 自带的"
            "(`comfy_extras/nodes_ace.py`)——这套实例比它们旧,升级 ComfyUI 即可。")
    rows = {name: (cls, field, folder) for cls, field, name, folder in MUSIC_FILES}
    lines = []
    for name in music_missing(schemas):
        _cls, _field, folder = rows[name]
        lines.append(f"  · {name}\n    → ComfyUI/models/{folder}/\n    {_MUSIC_MIRROR}/"
                     f"{folder}/{name}")
    return i18n.pick_now(
        "Music needs " + str(len(lines)) + " weight file(s) this ComfyUI cannot see yet; they go "
        "in ComfyUI's own model folders:\n" + "\n".join(lines),
        "做音乐还缺 " + str(len(lines)) + " 个权重文件,放进 ComfyUI 自己的模型目录即可:\n"
        + "\n".join(lines))


def music_graph(*, tags: str, lyrics: str = "", seconds: float = MUSIC_DEFAULT_SECONDS,
                bpm: int = 120, language: str = "en", keyscale: str = "C major",
                timesignature: str = "4", seed: int = 0, steps: int = MUSIC_TURBO_STEPS,
                prefix: str = "ace-step") -> dict:
    """ACE-Step 1.5 in API format, carrying this call's words, length and seed.

    `bpm` defaults to 120 rather than 0: the node takes an int and the blueprint ships 190, so a
    value that is legal and ordinary is the safe default — "let the model decide" is not something
    this node was shown to support. `language` only matters when there are lyrics; instrumentals
    ignore it.
    """
    return {
        "105": {"class_type": "DualCLIPLoader",
                "inputs": {"clip_name1": "qwen_0.6b_ace15.safetensors",
                           "clip_name2": "qwen_4b_ace15.safetensors",
                           "type": "ace", "device": "default"}},
        "106": {"class_type": "VAELoader", "inputs": {"vae_name": "ace_1.5_vae.safetensors"}},
        "104": {"class_type": "UNETLoader",
                "inputs": {"unet_name": "acestep_v1.5_turbo.safetensors",
                           "weight_dtype": "default"}},
        "78": {"class_type": "ModelSamplingAuraFlow",
               "inputs": {"model": ["104", 0], "shift": 3.0}},
        "94": {"class_type": "TextEncodeAceStepAudio1.5",
               "inputs": {"clip": ["105", 0], "tags": tags, "lyrics": lyrics, "seed": seed,
                          "bpm": int(bpm), "duration": float(seconds),
                          "timesignature": str(timesignature), "language": language,
                          "keyscale": keyscale, "generate_audio_codes": True,
                          "cfg_scale": 2.0, "temperature": 0.85, "top_p": 0.9, "top_k": 0,
                          "min_p": 0.0}},
        "47": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["94", 0]}},
        "98": {"class_type": "EmptyAceStep1.5LatentAudio",
               "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"model": ["78", 0], "positive": ["94", 0], "negative": ["47", 0],
                         "latent_image": ["98", 0], "seed": seed, "steps": int(steps), "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "18": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["3", 0], "vae": ["106", 0]}},
        # 320k rather than the node's default V0: this file becomes a music bed that gets mixed and
        # re-encoded, and a bed that was already lossy once does not need a second pass at V0.
        "9": {"class_type": "SaveAudioMP3",
              "inputs": {"audio": ["18", 0], "filename_prefix": prefix, "quality": "320k"}},
    }

# ------------------------------------------------------- the user's own workflows, from files
#
# A row above is a workflow *we* ship, so adding one is a code change. This is the other half: a
# folder of JSON files, one workflow each, so a graph the user built in ComfyUI's own UI becomes a
# capability of the app without anybody editing Python. That is what makes a digital-human,
# lip-sync or image-to-video graph something the user *has* rather than something we owe them.
#
# What a user row has to carry that a shipped row does not is its **graph**. `graph()` below builds
# one specific node set — the wan2.2 TI2V template — and the entire reason a second workflow can
# exist is that its nodes are different.
#
# ⚠️ Three things this must never do, each invisible in the product if got wrong:
#   * a file that fails to parse is **reported**, never skipped. A workflow the user wrote and the
#     app quietly does not know about will silently render the *default* workflow instead, and
#     nothing in the result says so.
#   * a name colliding with a shipped one is an **error**, not an override. Two different things
#     answering to one name is how "it worked yesterday" starts.
#   * a `{{placeholder}}` we cannot fill **raises**. Substituting an empty string would render a
#     graph that is nearly the author's, which is worse than not rendering at all.
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
WORKFLOW_SUFFIX = ".json"
MAX_USER_BYTES = 2_000_000     # a graph is a few KB; a 2 MB one is a mistake, not a workflow

_USER: dict[str, dict] = {}
_USER_ERRORS: list[dict] = []
_FOLDER: "Path | None" = None
_SIGN: tuple = ()


def use_folder(folder: "str | Path | None") -> dict:
    """Where the user's own workflows live. Called once at startup; safe to call again.

    Returns the result of the first read, so a caller that wants to print what it found (or what it
    could not parse) does not have to ask twice.
    """
    global _FOLDER, _SIGN
    _FOLDER = Path(folder) if folder else None
    _SIGN = ()
    if _FOLDER is not None:
        try:
            _FOLDER.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Not fatal and not hidden: with no folder there are simply no user workflows, and the
            # reason is recorded where `user_errors` reports it rather than swallowed here.
            _note_folder_error("workflows/", i18n.pick_now(
                f"the folder could not be created ({type(e).__name__}: {e}), so no workflow of "
                "yours can be loaded", f"这个目录建不出来({type(e).__name__}: {e}),所以你的工作流一份都读不到"))
    return reload_user(force=True)


def _note_folder_error(name: str, why: str) -> None:
    global _USER_ERRORS
    _USER_ERRORS = [e for e in _USER_ERRORS if e["file"] != name] + [{"file": name, "why": why}]


def _sign() -> tuple:
    """What the folder looks like right now: enough to notice an edit without re-reading it."""
    if _FOLDER is None or not _FOLDER.is_dir():
        return ()
    out = []
    for p in sorted(_FOLDER.glob("*" + WORKFLOW_SUFFIX)):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(out)


def reload_user(*, force: bool = False) -> dict:
    """Re-read the folder if it changed. `{loaded: [names], errors: [{file, why}]}`.

    Cheap enough to call from every read: unchanged means one `stat` per file and no parsing.
    """
    global _USER, _USER_ERRORS, _SIGN
    sign = _sign()
    if not force and sign == _SIGN:
        return {"loaded": sorted(_USER), "errors": list(_USER_ERRORS)}
    _SIGN = sign
    rows: dict[str, dict] = {}
    keep = [e for e in _USER_ERRORS if e["file"] == "workflows/"]
    errors: list[dict] = list(keep)
    for path in (sorted(_FOLDER.glob("*" + WORKFLOW_SUFFIX)) if _FOLDER else []):
        try:
            raw = path.read_bytes()
        except OSError as e:
            errors.append({"file": path.name, "why": f"{type(e).__name__}: {e}"})
            continue
        if len(raw) > MAX_USER_BYTES:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"the file is {len(raw) // 1024} KB; a workflow graph is a few KB",
                f"这个文件有 {len(raw) // 1024} KB,而一份工作流图只有几 KB")})
            continue
        try:
            body = json.loads(raw)
        except ValueError as e:
            errors.append({"file": path.name, "why": i18n.pick_now(
                f"not valid JSON: {e}", f"不是合法的 JSON:{e}")})
            continue
        try:
            rows[path.stem] = parse_row(path.stem, body)
        except ValueError as e:
            errors.append({"file": path.name, "why": str(e)})
    # A file that failed and then got fixed must stop being an error, which is why `errors` is built
    # fresh from this pass's files and the folder-level note is the only thing carried over.
    _USER = rows
    _USER_ERRORS = errors
    return {"loaded": sorted(rows), "errors": errors}


def parse_row(name: str, body: object) -> dict:
    """One user workflow, validated. Raises `ValueError` whose text is the whole diagnosis.

    Everything is checked here rather than at render time for the reason the whole file exists: a
    graph that is wrong in a way ComfyUI accepts-but-ignores produces a clip that looks fine and is
    not what was asked for.
    """
    if not isinstance(body, dict):
        raise ValueError(i18n.pick_now("the file must contain a JSON object",
                                       "文件内容必须是一个 JSON 对象"))
    if name in WORKFLOWS:
        raise ValueError(i18n.pick_now(
            f"the name \"{name}\" is already taken by a workflow this app ships — rename the file",
            f"「{name}」这个名字已经被本程序自带的工作流占了 —— 请给文件改个名"))
    use = str(body.get("use") or "video")
    if use != "video":
        raise ValueError(i18n.pick_now(
            f"`use` is \"{use}\"; this app has no other kind of workflow",
            f"`use` 是「{use}」,而本程序只有视频这一类工作流"))
    # A `setup` row is a capability declared **before** its graph exists: the nodes are part of
    # ComfyUI itself and only the weights are missing. It exists so the probe can name those files
    # and so a render chosen for it refuses, instead of the name quietly resolving to another graph.
    setup = bool(body.get("setup"))
    g = body.get("graph")
    if setup and not g:
        g = {}
    if not isinstance(g, dict) or (not g and not setup):
        raise ValueError(i18n.pick_now(
            "`graph` is required (unless `setup` is true): the API-format graph, node id to "
            "{class_type, inputs}, exactly as ComfyUI's own \"Save (API format)\" writes it",
            "必须有 `graph`(除非 `setup` 为真):API 格式的图,节点 id 对 {class_type, inputs},"
            "就是 ComfyUI 里「导出(API 格式)」写出来的那个"))
    for nid, node in g.items():
        if not isinstance(node, dict) or not str(node.get("class_type") or ""):
            raise ValueError(i18n.pick_now(f"node {nid!r} has no `class_type`",
                                           f"节点 {nid!r} 没有 `class_type`"))
        if not isinstance(node.get("inputs", {}), dict):
            raise ValueError(i18n.pick_now(f"node {nid!r} has `inputs` that are not an object",
                                           f"节点 {nid!r} 的 `inputs` 不是一个对象"))
    needs = body.get("needs") or []
    if not isinstance(needs, list):
        raise ValueError(i18n.pick_now(
            "`needs` must be a list of [node class, input field, file name] triples",
            "`needs` 必须是 [节点类, 输入字段, 文件名] 三元组的列表"))
    for item in needs:
        if not (isinstance(item, (list, tuple)) and len(item) in (3, 4)
                and all(isinstance(x, str) for x in item)):
            raise ValueError(i18n.pick_now(
                f"`needs` entries must be [node class, input field, file name] or the same with a "
                f"download address as a fourth item; {item!r} is not",
                f"`needs` 的每一项必须是 [节点类, 输入字段, 文件名],或者再加第四项写下载地址;{item!r} 不是"))
    return {
        "use": use,
        "needs": tuple(tuple(str(x) for x in item) for item in needs),
        "fps": _positive(body.get("fps"), 24, "fps"),
        "long_side_max": int(_positive(body.get("long_side_max"), 1280, "long_side_max")),
        # `steps` is an integer input on every sampler there is; `cfg` is a float on all of them.
        # Substituting a float where an int goes is the kind of thing ComfyUI accepts right up
        # until it does not.
        "steps": int(_positive(body.get("steps"), 20, "steps")),
        "cfg": _number(body.get("cfg"), 5.0, "cfg"),
        "graph": g,
        "title": str(body.get("title") or ""),
        "note": str(body.get("note") or ""),
        "user": True,
        "file": name,
        "setup": setup or None,
    }


def _positive(value: object, fallback: float, field: str) -> float:
    n = _number(value, fallback, field)
    if n <= 0:
        raise ValueError(i18n.pick_now(f"`{field}` must be greater than zero",
                                       f"`{field}` 必须大于零"))
    return n


def _number(value: object, fallback: float, field: str) -> float:
    if value is None or value == "":
        return float(fallback)
    try:
        return float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(i18n.pick_now(f"`{field}` must be a number, not {value!r}",
                                       f"`{field}` 必须是个数字,{value!r} 不是")) from None


def available() -> dict[str, dict]:
    """Every workflow that can run: the ones we ship, plus the user's own folder.

    Includes the `setup` rows — capabilities whose weights are not installed. They are offered on
    purpose: a name in the dropdown is how the user finds out what to install and why, and the probe
    is what tells them which files. What they cannot do is render, and `payload_for` enforces that.
    """
    reload_user()
    return {**WORKFLOWS, **SETUP_WORKFLOWS, **_USER}


def names() -> list[str]:
    """Their names, sorted — one list for the dropdown, the tool error text and the probe."""
    return sorted(available())


def user_errors() -> list[dict]:
    """Files in the user's folder that could not be used, each with the reason."""
    reload_user()
    return list(_USER_ERRORS)


def workflow_of(model: str) -> dict:
    """The workflow a model name refers to. An unknown name gets the default, which is the same
    direction `video.shape_of` takes: a name we do not ship keys off the thing we do.

    The fallback is deliberate and the caller is expected to *say* it happened — see `resolve`,
    which is what the generation path uses. Reading this directly is for the graph builder and the
    probe, both of which have already decided which workflow they mean.
    """
    known = available()
    return known.get((model or "").strip()) or known[DEFAULT_WORKFLOW]


def resolve(model: str) -> tuple[str, str]:
    """(the workflow that will actually run, a sentence when it is not the one that was asked for).

    The second value is the whole reason this function exists. The fallback above is a deliberate
    design choice, and a fallback nobody is told about is the app rendering something other than
    what the user picked — which is the failure this codebase treats as the expensive kind.
    """
    want = (model or "").strip()
    known = available()
    if want and want not in known:
        broken = [e for e in _USER_ERRORS if e["file"] == want + WORKFLOW_SUFFIX]
        why = (" " + broken[0]["why"]) if broken else ""
        return DEFAULT_WORKFLOW, i18n.pick_now(
            f"The workflow \"{want}\" is not one this app knows, so \"{DEFAULT_WORKFLOW}\" is what "
            f"ran instead.{why} Known: {', '.join(names())}.",
            f"「{want}」不是本程序认识的工作流,所以实际用的是「{DEFAULT_WORKFLOW}」。{why}"
            f"现有的:{'、'.join(names())}。")
    return (want or DEFAULT_WORKFLOW), ""


# ------------------------------------------------------------------ numbers the graph will accept
def frames_for(seconds: int, *, fps: int) -> int:
    """How many frames `seconds` is, in the only counts Wan renders.

    `4n+1` is the model's own requirement, not a preference: its temporal compression is 4, so a
    latent whose length is not `4n+1` is rejected (or silently truncated, depending on the node).

    `+1` after the multiplication, because the template's own arithmetic is that way round: its
    five seconds at 24 fps is **121** frames, not 120. The extra frame is the first one, and
    dropping it would make every clip start one frame later than asked. Rounding down is deliberate
    — a clip shorter than asked for is a smaller surprise than one longer than the caller budgeted.
    """
    want = max(5, int(round(max(1, int(seconds)) * fps)) + 1)
    return want - (want - 1) % 4


def size_for(ratio: str, short_edge: int, *, long_side_max: int) -> tuple[int, int]:
    """(width, height) for an aspect ratio and the user's short edge.

    Two constraints, both the model's: both sides must be a multiple of 16 (VAE 8, patch 2), and
    the long side must not exceed what the checkpoint was built for. Both are *rounded* rather
    than refused — a 700px request becoming 704 is not something the caller should have to know
    about — and the short edge is raised to 128 so a tiny setting cannot produce a 0-pixel graph.
    """
    try:
        a, b = (int(x) for x in str(ratio or "16:9").split(":"))
        a, b = max(1, a), max(1, b)
    except (TypeError, ValueError):
        a, b = 16, 9
    short = max(128, int(short_edge or 704))
    if a >= b:
        height, width = short, round(short * a / b)
    else:
        width, height = short, round(short * b / a)
    width, height = round(width / 16) * 16, round(height / 16) * 16
    width, height = max(128, width), max(128, height)
    # The long side is what the cap applies to, whichever side that is.
    longest = max(width, height)
    if longest > long_side_max:
        scale = long_side_max / longest
        width = max(128, round(width * scale / 16) * 16)
        height = max(128, round(height * scale / 16) * 16)
    return width, height


def graph(model: str, *, prompt: str, width: int, height: int, frames: int, seed: int,
          prefix: str, negative: str = NEGATIVE, image: str = "", last_image: str = "",
          audio: str = "") -> dict:
    """The API graph, in the format `POST /prompt` takes: node id -> `{class_type, inputs}`.

    Two sources, one shape. A workflow we ship is built here from ComfyUI's own
    `video_wan2_2_5B_ti2v` template with the template's own node ids, so a graph exported from the
    user's UI can be compared against this one line by line. A workflow the user wrote brings its
    own graph and gets our numbers substituted into it (`render_graph`) — the only reason to add a
    workflow at all is that its nodes differ from these.
    """
    w = workflow_of(model)
    if w.get("graph"):
        return render_graph(w, prompt=prompt, negative=negative, width=width, height=height,
                            frames=frames, seed=seed, prefix=prefix, image=image,
                            last_image=last_image, audio=audio)
    return {
        "37": {"class_type": "UNETLoader",
               "inputs": {"unet_name": w["needs"][0][2], "weight_dtype": "default"}},
        "38": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": w["needs"][1][2], "type": "wan", "device": "default"}},
        "39": {"class_type": "VAELoader", "inputs": {"vae_name": w["needs"][2][2]}},
        # `shift` is the template's 8.0. It is a flow-matching schedule parameter, and for this
        # checkpoint it is not a taste knob — 8 is what the released template uses.
        "48": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["37", 0], "shift": 8.0}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["38", 0], "text": prompt}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["38", 0], "text": negative}},
        "55": {"class_type": "Wan22ImageToVideoLatent",
               "inputs": {"vae": ["39", 0], "width": int(width), "height": int(height),
                          "length": int(frames), "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"model": ["48", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["55", 0], "seed": int(seed), "steps": int(w["steps"]),
                         "cfg": float(w["cfg"]), "sampler_name": w["sampler"],
                         "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
        "57": {"class_type": "CreateVideo", "inputs": {"images": ["8", 0], "fps": float(w["fps"])}},
        # `mp4`, not `auto`: the tool saves every clip as `.mp4`, and a server that decided WebM
        # this time would leave a file whose name lies about its contents.
        "58": {"class_type": "SaveVideo",
               "inputs": {"video": ["57", 0], "filename_prefix": prefix, "format": "mp4"}},
    }


def render_graph(row: dict, **values: object) -> dict:
    """The user's own graph with our numbers substituted into it.

    Substitution is by name and it is **total**: a `{{name}}` we cannot supply raises rather than
    becoming an empty string, because a graph with a blank where its prompt should be still
    renders — just not the thing its author wrote. A value written as the whole string keeps its
    type (`"{{width}}"` is an integer, which is what a `width` input requires); one embedded in
    longer text is stringified.

    What there is to use: `{{prompt}}` `{{negative}}` `{{width}}` `{{height}}` `{{frames}}`
    `{{seed}}` `{{fps}}` `{{steps}}` `{{cfg}}` `{{slug}}` `{{workflow}}`, plus `{{image}}`,
    `{{last_image}}` and `{{audio}}` — which are only there when the caller actually has one, so a
    `LoadImage` fed by a member's photo works and one fed by nothing raises instead of rendering a
    graph pointed at an empty filename. Use `{{slug}}` in a `filename_prefix`; `{{prompt}}` in a
    filename is whatever was typed, slashes and all.
    """
    values = dict(values, fps=row.get("fps"), steps=row.get("steps"), cfg=row.get("cfg"),
                  workflow=row.get("file") or "")
    # A `filename_prefix` cannot take the raw prompt: it may contain `/` or characters a filesystem
    # will not take, and ComfyUI would either nest directories or refuse the job. `{{slug}}` is the
    # prompt reduced to something safe, and `{{prompt}}` is what a *text* input wants.
    values.setdefault("slug", _slug(str(values.get("prompt") or "")))
    # Material the caller did not supply is *absent*, not empty. A `LoadImage` pointed at "" is a
    # node ComfyUI accepts and fails on after the wait — the graph has to say so up front instead.
    for key in ("image", "last_image", "audio"):
        if not values.get(key):
            values.pop(key, None)
    missing: set[str] = set()

    def one(v: object) -> object:
        if isinstance(v, str):
            whole = PLACEHOLDER.fullmatch(v)
            if whole:
                key = whole.group(1)
                if key not in values or values[key] is None:
                    missing.add(key)
                    return v
                return values[key]

            def sub(m: "re.Match[str]") -> str:
                key = m.group(1)
                if key not in values or values[key] is None:
                    missing.add(key)
                    return m.group(0)
                return str(values[key])

            return PLACEHOLDER.sub(sub, v)
        if isinstance(v, dict):
            return {k: one(x) for k, x in v.items()}
        if isinstance(v, list):
            return [one(x) for x in v]
        return v

    out = one(row["graph"])
    if missing:
        supplied = ", ".join(sorted(k for k, val in values.items() if val is not None))
        raise VideoError(i18n.pick_now(
            f"The workflow \"{row.get('file') or ''}\" asks for "
            f"{{{{{'}}, {{'.join(sorted(missing))}}}}}, which this app does not fill in. It can "
            f"fill: {supplied}.",
            f"工作流「{row.get('file') or ''}」用了 "
            f"{{{{{'}}、{{'.join(sorted(missing))}}}}},而本程序不提供这个值。它能填的是:{supplied}。"))
    assert isinstance(out, dict)
    return out


def wants(row: dict) -> set[str]:
    """Which `{{placeholders}}` a graph actually uses.

    This is what makes "this graph has nowhere to put a keyframe" a fact read off the graph rather
    than a claim about the app. The alternative — assuming every workflow is text-to-video — is how
    a member ends up asking for image-to-video and being told it is unsupported while the graph in
    front of us has a `LoadImage` in it.
    """
    found: set[str] = set()

    def walk(v: object) -> None:
        if isinstance(v, str):
            found.update(m.group(1) for m in PLACEHOLDER.finditer(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(row.get("graph") or {})
    return found


async def upload(prov: dict, data: bytes, filename: str, *, client: httpx.AsyncClient,
                 subfolder: str = "") -> str:
    """Hand a file to the instance; return the name a graph's input takes.

    Named by content, and uploaded with `overwrite`, so the same picture reused across ten renders
    is one file in `input/` rather than ten copies — and re-uploading a *changed* picture under the
    same name cannot happen, because the name is its hash.

    The name comes back from the server rather than being assumed: ComfyUI renames a collision
    (`x (1).png`) and returning what we hoped for would leave the graph pointing at a file that is
    not there — which the instance accepts and then fails on, after the wait.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise VideoError(i18n.pick_now(
            f"{filename} is {len(data) / 1024 / 1024:.0f} MB, over the {MAX_UPLOAD_BYTES // 1024 // 1024} MB "
            "this app will send to ComfyUI.",
            f"{filename} 有 {len(data) / 1024 / 1024:.0f} MB,超过本程序愿意发给 ComfyUI 的 "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB。"))
    base = (prov.get("base_url") or "").strip()
    # ComfyUI writes the name it is given into its own `input` folder, so it is reduced to a
    # basename here: a path with a separator in it would be a directory the user did not ask for,
    # and `image_upload` checks the joined path stays under `input` and answers 400 if it does not.
    stem, dot, suffix = Path(filename).name.rpartition(".")
    safe = f"{media.slug(stem, 40) or 'upload'}-{hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16]}{dot}{suffix}"
    try:
        r = await client.post(
            media.api_url(base, UPLOAD_ROUTE),
            # `_headers` is the *json* route's set, and its `Content-Type: application/json` cannot
            # survive here: on a multipart request it overrides the type httpx would set — boundary
            # and all — so the instance reads the body as JSON, finds no file in it, and answers
            # HTTP 400. Only the credential is wanted from it.
            headers={k: v for k, v in _headers(prov).items() if k.lower() != "content-type"},
            files={"image": (safe, data, "application/octet-stream")},
            data={"overwrite": "true", "type": "input", "subfolder": subfolder},
            timeout=UPLOAD_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise VideoError(i18n.pick_now(
            f"Could not send {filename} to ComfyUI: {type(e).__name__}: {e}",
            f"把 {filename} 发给 ComfyUI 失败:{type(e).__name__}: {e}")) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"ComfyUI refused {filename} ({_why(r)}), so nothing was generated.",
            f"ComfyUI 拒收了 {filename}({_why(r)}),没有生成。"))
    try:
        got = r.json()
    except ValueError:
        got = None
    name = str((got or {}).get("name") or "") if isinstance(got, dict) else ""
    if not name:
        raise VideoError(i18n.pick_now(
            f"ComfyUI accepted {filename} but did not say what it called it, so the graph has "
            "nothing to point at.",
            f"ComfyUI 收下了 {filename},但没说它存成了什么名字,图里就没有可指的对象。"))
    where = str((got or {}).get("subfolder") or "")     # type: ignore[union-attr]
    return f"{where}/{name}" if where else name



# ------------------------------------------------------------------ the four steps
def _headers(prov: dict) -> dict[str, str]:
    """ComfyUI itself wants no credential. One put behind a reverse proxy that does want one is
    the reason this exists — and it is the *same* host the request goes to, so the key is not
    being handed to a third party (unlike the object-store links MetaChat and Ark report)."""
    return media.auth_headers(prov.get("api_key", ""))


async def submit(prov: dict, payload: dict, *, client: httpx.AsyncClient) -> str:
    """Send the graph and return ComfyUI's `prompt_id`.

    A reply with `node_errors` is refused *here* rather than polled for: the instance has already
    told us which node is unhappy, and waiting for a job that will never start until the deadline
    would turn a clear error into "gave up after 600s".
    """
    base = (prov.get("base_url") or "").strip()
    url = media.api_url(base, PROMPT_ROUTE)
    body = {"prompt": payload["graph"], "client_id": uuid.uuid4().hex}
    try:
        r = await client.post(url, headers=_headers(prov), json=body, timeout=SUBMIT_TIMEOUT)
    except httpx.HTTPError as e:
        raise VideoError(i18n.pick_now(
            f"Could not reach ComfyUI at {base}: {type(e).__name__}: {e}. Is it running?",
            f"连不上 ComfyUI({base}):{type(e).__name__}: {e}。它启动了吗?",
        )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"ComfyUI refused the workflow ({_why(r)})", f"ComfyUI 拒绝了这份工作流({_why(r)})"
        ))
    try:
        body_got = r.json()
    except ValueError:
        raise VideoError(i18n.pick_now(
            f"{base} is not answering like ComfyUI: it did not return JSON. Check the address — a "
            "ComfyUI API lives at the root, usually http://127.0.0.1:8188.",
            f"{base} 的回应不像 ComfyUI:没有返回 JSON。请检查地址 —— ComfyUI 的接口在根路径上,"
            "通常是 http://127.0.0.1:8188。",
        )) from None
    pid = str((body_got or {}).get("prompt_id") or "").strip()
    if not pid:
        node_errors = (body_got or {}).get("node_errors") or (body_got or {}).get("error")
        raise VideoError(i18n.pick_now(
            f"ComfyUI did not accept the workflow: {_brief(node_errors)}. The usual reason is a "
            "file the graph names that this instance does not have — test the service to see which.",
            f"ComfyUI 没有接受这份工作流:{_brief(node_errors)}。通常是因为图里写的文件这个实例没有,"
            "测试一下服务就能看出缺哪个。",
        ))
    return pid


async def status_of(prov: dict, pid: str, *, client: httpx.AsyncClient) -> tuple[str, dict]:
    """(one of `video._DONE` / `video._FAILED` / still-working, the detail).

    ComfyUI removes a finished job from the queue and leaves it in `/history`, so "not in history"
    is not failure — it is either "still queued/running" or "this instance no longer knows about
    it" (a restart loses the queue). Both keep the caller waiting, which is the safe direction:
    the deadline is what ends the wait, and calling a job failed that is merely slow would throw
    away GPU time that has already been spent.
    """
    base = (prov.get("base_url") or "").strip()
    url = media.api_url(base, HISTORY_ROUTE.format(pid=urllib.parse.quote(pid, safe="")))
    try:
        r = await client.get(url, headers=_headers(prov), timeout=STATUS_TIMEOUT)
    except httpx.HTTPError as e:
        raise VideoError(i18n.pick_now(
            f"Lost contact with ComfyUI while waiting: {type(e).__name__}: {e}",
            f"等待期间与 ComfyUI 失去联系:{type(e).__name__}: {e}",
        )) from None
    if r.status_code >= 400:
        raise VideoError(i18n.pick_now(
            f"Could not read the render status ({_why(r)})", f"读取生成状态失败({_why(r)})"
        ))
    try:
        hist = r.json()
    except ValueError:
        hist = {}
    item = (hist or {}).get(pid) if isinstance(hist, dict) else None
    if not isinstance(item, dict):
        return "running", {}
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    messages = status.get("messages") or []
    failed = status.get("status_str") == "error" or any(
        isinstance(m, list) and m and m[0] in ("execution_error", "execution_interrupted")
        for m in messages
    )
    if failed:
        # The interesting part of a ComfyUI failure is inside `messages`, in a per-node shape —
        # `["execution_error", {"node_type": …, "exception_message": …}]`. `_brief` digs it out so
        # the user is told "the VAE could not load" rather than "the server reported failure".
        return "failed", {"error": _brief(messages) or _brief(status)}
    if status.get("completed") is True or status.get("status_str") == "success":
        return "completed", item
    return "running", item


def link_of(detail: dict) -> str:
    """The `view` path of the finished clip, relative to the instance.

    `download` joins it with the provider's address, so the URL is built entirely from our own
    configuration plus the filename the instance reported — nothing the instance returns is
    followed as a URL, and there is no object store to hand a key to.

    ComfyUI names the result by *file*, not by task: `outputs[<node>]` holds entries carrying
    `filename`/`subfolder`/`type`. The key it sits under is `images` even for a clip
    (`SaveVideo` reports `"animated": true` next to it), so the walk looks at the entries rather
    than at the key — an older save node's `gifs` and a future `videos` are found the same way.
    """
    for d in _saved_files(detail):
        return VIEW_ROUTE + "?" + urllib.parse.urlencode({
            "filename": d["filename"], "subfolder": d.get("subfolder", ""),
            "type": d.get("type", "output"),
        })
    return ""


# The fetch itself is *not* here. `video.download` owns it — with its Content-Length check, its
# empty-file check and its size cap — and it needs nothing from this module beyond the path
# `link_of` returns. A second copy of those three checks would be a second chance to get one
# wrong, which is the same reason `media.save_bytes` is shared by both generators.


def setup_needs(name: str) -> tuple:
    """A declared workflow's `(node class, input field, file, where to get it)` rows.

    An accessor rather than reaching into the table, so that a test asserting "every file was named,
    each with its address" reads the same list the probe does instead of a second copy of it.
    """
    row = SETUP_WORKFLOWS.get((name or "").strip()) or {}
    return tuple(row.get("needs") or ())


def _missing_list(missing: list[str], row: dict) -> str:
    """The file names to go and get, with where to get them — one per line.

    A bare list of six file names is not an instruction. The addresses come from the workflow row
    (`needs`, fourth item) exactly as the official template names them, so nothing here is guessed.
    """
    where = {n[2]: (n[3] if len(n) > 3 else "") for n in row.get("needs") or []}
    return "\n".join(
        f"  · {name}" + (f"\n    {where[name]}" if where.get(name) else "") for name in missing)


def setup_diagnosis(row: dict, name: str) -> str:
    """Why a declared-but-uninstalled workflow cannot render, and what would fix it.

    This is the whole reason `SETUP_WORKFLOWS` exists: the alternative to this sentence is a name in
    a dropdown that either does nothing or — much worse — silently renders something else.
    """
    files = _missing_list([n[2] for n in row.get("needs") or []], row)
    return i18n.pick_now(
        f"The \"{name}\" workflow is declared but not installed, so nothing was rendered. It runs "
        "on nodes ComfyUI already has; what is missing is the weights, which go in ComfyUI's own "
        f"model folders:\n{files}\n"
        "After installing them, open ComfyUI's own template of the same name, save it in **API "
        "format**, and drop the graph into a workflow file of your own in the workflows folder — "
        "then this name renders instead of refusing.",
        f"「{name}」工作流只是**声明**了、还没有装好,所以没有渲染。它用的节点 ComfyUI 里本来就有,"
        f"缺的是权重文件 —— 放进 ComfyUI 自己的模型目录:\n{files}\n"
        "装好之后,在 ComfyUI 里打开同名的官方模板,**导出(API 格式)**,把那张图放进你 workflows 目录下的"
        "一个工作流文件里 —— 那时这个名字就会真的渲染,而不是拒绝。")


# ------------------------------------------------------------------ probe
async def probe(prov: dict, *, client: httpx.AsyncClient) -> tuple[bool, str]:
    """Is ComfyUI there, and can it run the workflow it is set to?

    Three questions, because "the server answered" is the least useful of them: a ComfyUI with no
    video checkpoint is running perfectly and cannot make a clip. So this asks `/system_stats`
    (is it up, and is it recent enough to have a version), `/object_info` (does it have the nodes)
    and then reads the loader choices for the three files the workflow names. The answer names the
    missing file, which is the one thing the user can act on.
    """
    base = (prov.get("base_url") or "").strip()
    if not base:
        return False, i18n.pick_now("This provider has no address configured.", "这个服务商没有填地址。")
    try:
        stats = await client.get(media.api_url(base, SYSTEM_STATS_ROUTE),
                                 headers=_headers(prov), timeout=STATUS_TIMEOUT)
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"Could not reach ComfyUI at {base}: {type(e).__name__}: {e}. Start it with "
            "`python main.py` in its directory and try again.",
            f"连不上 ComfyUI({base}):{type(e).__name__}: {e}。在它的目录里用 `python main.py` 启动后再试。",
        )
    if stats.status_code in (401, 403):
        return False, i18n.pick_now(
            f"{base} is up but rejected the key ({stats.status_code}).", f"{base} 是活的,但密钥被拒绝了({stats.status_code})。"
        )
    if stats.status_code >= 400:
        return False, i18n.pick_now(
            f"{base} answered {stats.status_code} for /system_stats, so this does not look like a "
            "ComfyUI root address. The API lives at the root — usually http://127.0.0.1:8188.",
            f"{base} 对 /system_stats 返回了 {stats.status_code},这个地址看起来不是 ComfyUI 的根地址。"
            "接口在根路径上 —— 通常是 http://127.0.0.1:8188。",
        )
    version = ""
    try:
        body = stats.json()
        if isinstance(body, dict) and isinstance(body.get("system"), dict):
            version = str(body["system"].get("comfyui_version") or "")
    except ValueError:
        return False, i18n.pick_now(
            f"{base} answered /system_stats with something that is not JSON, so this is not a "
            "ComfyUI API address.",
            f"{base} 的 /system_stats 返回的不是 JSON,这个地址不是 ComfyUI 的接口。",
        )
    if not version:
        return False, i18n.pick_now(
            f"{base} answered but reported no ComfyUI version, so it is not (or is not only) "
            "ComfyUI. Point this at the ComfyUI API root.",
            f"{base} 有响应但没报 ComfyUI 版本,它(至少不完全是)ComfyUI。请把这个地址指向 ComfyUI 的接口根。",
        )
    # What the instance can actually load, per loader. A file the workflow names but the instance
    # does not have is the single most likely reason a render will never start.
    name = _workflow_name(prov)
    if not name:
        # Not "unreachable" and not "yes": the instance may be perfectly fine and there is still
        # nothing to check, because the thing to check is not here.
        return False, _unknown_workflow(prov)
    w = workflow_of(name)
    try:
        info = await client.get(media.api_url(base, OBJECT_INFO_ROUTE),
                                headers=_headers(prov), timeout=INTROSPECT_TIMEOUT)
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"ComfyUI {version} is up at {base}, but its node list could not be read "
            f"({type(e).__name__}), so it is unknown whether the workflow can run.",
            f"{base} 上的 ComfyUI {version} 是活的,但读不到节点清单({type(e).__name__}),"
            "无法判断这份工作流能不能跑。",
        )
    try:
        schemas = info.json()
    except ValueError:
        schemas = None
    if info.status_code >= 400 or not isinstance(schemas, dict) or not schemas:
        return False, i18n.pick_now(
            f"ComfyUI {version} is up at {base}, but /object_info returned nothing usable, so it "
            "is unknown whether the workflow can run.",
            f"{base} 上的 ComfyUI {version} 是活的,但 /object_info 没返回可用的东西,"
            "无法判断这份工作流能不能跑。",
        )
    # `n[0]`, not tuple unpacking: a `needs` entry may carry a download address as a fourth
    # item, and unpacking three is what turns that into "too many values to unpack" — at the
    # probe, which is the one place a user goes to find out what is wrong.
    if w.get("setup"):
        required_nodes = {n[0] for n in w["needs"]}
    else:
        template = w.get("graph") or graph(name, prompt="probe", width=256, height=256,
                                          frames=25, seed=1, prefix="probe")
        required_nodes = {node["class_type"] for node in template.values()} | {n[0] for n in w["needs"]}
    missing_nodes = sorted(required_nodes - schemas.keys())
    if missing_nodes:
        return False, i18n.pick_now(
            f"ComfyUI {version} is up, but it has no {'or'.join(missing_nodes)} node, which the "
            f"\"{name}\" workflow uses — this install is too old or is missing its nodes. Update "
            "ComfyUI, or install the custom node that provides them.",
            f"ComfyUI {version} 是活的,但没有 {'/'.join(missing_nodes)} 节点,而「{name}」工作流要用它 —— "
            "这个安装太旧或缺少节点。请更新 ComfyUI,或者装提供这个节点的自定义节点。",
        )
    missing = [n[2] for n in w["needs"] if n[2] not in _choices(schemas, n[0], n[1])]
    if missing:
        shipped = "" if w.get("user") or w.get("setup") else i18n.pick_now(
            " `wan2.2-ti2v-5b` needs the Wan2.2 TI2V 5B diffusion model, an umt5 text encoder and "
            "the Wan2.2 VAE.", " `wan2.2-ti2v-5b` 需要 Wan2.2 TI2V 5B 扩散模型、一个 umt5 文本编码器和 Wan2.2 的 VAE。")
        # Where a row knows where its files come from, say so: "you are missing six files" is a
        # diagnosis, and the addresses are what turn it into something the user can act on.
        where = _missing_list(missing, w)
        detail = ("\n" + where) if any(len(n) > 3 for n in w["needs"]) else ", ".join(missing)
        tail = "\n— put the file(s) in its model folders and try again." if "\n" in detail \
            else " — put the file(s) in its model folders and try again."
        return False, i18n.pick_now(
            f"ComfyUI {version} is up and has the nodes, but it does not have {detail}{tail}{shipped}",
            f"ComfyUI {version} 是活的、节点也有,但它没有:\n{detail}\n{tail.strip('— ')}{shipped}",
        )
    if w.get("setup"):
        return False, setup_diagnosis(w, name)
    n = len(w["needs"])
    return True, i18n.pick_now(
        (f"ComfyUI {version} at {base} has the nodes and the {n} file(s) the \"{name}\" workflow "
         "needs. A clip renders on this machine with no per-clip cost.") if n else
        (f"ComfyUI {version} at {base} has every node the \"{name}\" workflow uses, and that "
         "workflow names no extra file. A clip renders on this machine with no per-clip cost."),
        (f"{base} 上的 ComfyUI {version} 有需要的节点,也有「{name}」工作流要的 {n} 个文件。"
         "生成在这台机器上完成,不按次计费。") if n else
        (f"{base} 上的 ComfyUI {version} 有「{name}」工作流用到的全部节点,而且这份工作流不需要额外的模型文件。"
         "生成在这台机器上完成,不按次计费。"),
    )


def _workflow_name(prov: dict) -> str:
    """Which workflow to check: the one the provider advertises, when this app knows it.

    "" when the provider names something we do not have. The probe then says so by name, and that
    is the only way a user finds out their own workflow file is missing or malformed — checking the
    default instead would answer confidently about a workflow nobody asked about.
    """
    known = available()
    advertised = [str(m) for m in (prov.get("models") or [])]
    for m in advertised:
        if m in known:
            return m
    return "" if advertised else DEFAULT_WORKFLOW


def _unknown_workflow(prov: dict) -> str:
    """Why there is nothing to check: the provider is set to a workflow this app does not have.

    The parse errors are included because "my workflow is not there" and "my workflow file has a
    typo" need different fixes, and this message is the only place that can tell them apart.
    """
    advertised = ", ".join(str(m) for m in (prov.get("models") or [])) or "(none)"
    broken = user_errors()
    why = ""
    if broken:
        why = i18n.pick_now(
            " Files in the workflows folder that could not be used: "
            + "; ".join(f"{e['file']}: {e['why']}" for e in broken) + ".",
            "workflows 目录里这些文件没能用上:"
            + " ; ".join(f"{e['file']}:{e['why']}" for e in broken) + "。")
    return i18n.pick_now(
        f"This provider is set to \"{advertised}\", which is not a workflow this app knows, so "
        f"there is nothing to check.{why} Known workflows: {', '.join(names())}.",
        f"这个服务商设的是「{advertised}」,而本程序不认识这个工作流,所以没有可检查的对象。{why}"
        f"现有的工作流:{'、'.join(names())}。")


def _choices(schemas: dict, cls: str, field: str) -> list:
    """One loader's list of files it can load. `None` when the node does not declare it, which is
    what the caller turns into "this install cannot run the workflow" rather than "the file is
    missing" — those are different things to fix."""
    node = schemas.get(cls) if isinstance(schemas, dict) else None
    spec = ((node or {}).get("input") or {}).get("required", {}).get(field)
    if isinstance(spec, list) and spec and isinstance(spec[0], list):
        return [str(x) for x in spec[0]]
    return []


# ------------------------------------------------------------------ small shared readers
def payload_for(model: str, *, prompt: str, ratio: str, seconds: int, short_edge: int,
                seed: int, image: str = "", last_image: str = "", audio: str = "") -> dict:
    """Everything `video.generate` needs for one render, in the shape the other kinds use: a dict
    the caller hands to `submit` without knowing what is inside it.

    `seed=0` becomes a real seed rather than being passed through: 0 is what "not specified" means
    in our tool schema, and KSampler would take it literally, so every unspecified call would
    return the same clip.

    The workflow is resolved **once**, here, and the name that comes out is the one reported. This
    used to report the name it was *asked* for while `graph()` quietly fell back to the default, so
    a payload could say `"workflow": "my-digital-human"` and carry wan2.2's graph. Reporting the
    request instead of the fact is the exact mistake the clip measurement below exists to prevent.
    """
    name, note = resolve(model)
    w = workflow_of(name)
    # ⛔ A declared-but-uninstalled workflow refuses here rather than falling through. Falling
    # through is what `workflow_of` does for an unknown name, and for *this* case it would render a
    # clip that looks finished and is a different workflow entirely.
    if w.get("setup"):
        raise VideoError(setup_diagnosis(w, name))
    width, height = size_for(ratio, short_edge, long_side_max=int(w["long_side_max"]))
    frames = frames_for(seconds, fps=int(w["fps"]))
    # Resolved **once**: the caller reports this seed back to the user, and computing it again for
    # the graph would report a number the render did not use. (Derived from the clock rather than
    # `random` on purpose — a reproducible seed is the point, and the clock is what the caller can
    # copy out of the log and paste back in.)
    resolved = int(seed) if int(seed or 0) > 0 else int(time.time() * 1000) % (2 ** 31)
    return {
        "workflow": name,
        "asked_for": (model or "").strip(),
        "note": note,
        "prompt": prompt,
        "width": width, "height": height, "frames": frames,
        "seconds": round(frames / int(w["fps"]), 2),
        "seed": resolved,
        "graph": graph(name, prompt=prompt, width=width, height=height, frames=frames,
                       seed=resolved, prefix="team-agent/%s" % _slug(prompt),
                       image=image, last_image=last_image, audio=audio),
    }


def _slug(text: str, limit: int = 32) -> str:
    """A folder-safe fragment, so clips are recognisable inside ComfyUI's own output directory.
    ComfyUI would accept more than this, but a name that survives every filesystem is one less
    thing that breaks on someone else's machine."""
    return media.slug(text, limit) or "clip"


def _saved_files(value: object) -> list[dict]:
    """Every saved-output descriptor inside a ComfyUI reply, video ones first.

    One walk for one shape: `{"filename": …, "subfolder": …, "type": "output"}` nested anywhere
    under `outputs`. Anything in a ComfyUI reply that has those three keys is a file it saved, so
    the walk does not need to know which node class made it.
    """
    found: list[dict] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            if (isinstance(v.get("filename"), str) and v.get("filename")
                    and v.get("type") == "output"):
                found.append(v)
                return
            for c in v.values():
                walk(c)
        elif isinstance(v, list):
            for c in v:
                walk(c)

    walk(value)
    return sorted(found, key=lambda d: 0 if str(d["filename"]).lower().endswith(_VIDEO_EXT) else 1)


def _brief(value: object, limit: int = 300) -> str:
    """The sentence out of a ComfyUI error, whichever of its four shapes it arrived in.

    Measured shapes: `node_errors` is `{node: {"errors": [{"message": …}]}}`, `execution_error` is
    a list whose second item is a dict with `exception_message`, and a plain refusal is
    `{"error": {"message": …}}`. Flattening them here keeps that knowledge in one place instead of
    in every caller that wants to say something useful.
    """
    pieces: list[str] = []

    def walk(v: object, depth: int = 0) -> None:
        if depth > 6 or len(pieces) > 6:
            return
        if isinstance(v, dict):
            for key in ("message", "exception_message", "exception_type", "details", "error"):
                if isinstance(v.get(key), str) and v[key].strip():
                    pieces.append(v[key].strip())
            for val in v.values():
                walk(val, depth + 1)
        elif isinstance(v, list):
            for val in v:
                walk(val, depth + 1)
        elif isinstance(v, str) and v.strip():
            pieces.append(v.strip())

    walk(value)
    out = " · ".join(dict.fromkeys(pieces))
    return " ".join(out.split())[:limit]


def _why(r: httpx.Response) -> str:
    """The HTTP status plus whatever the body says. ComfyUI answers a bad graph with a JSON
    explanation, and dropping it would leave the user with a number and no reason."""
    said = ""
    try:
        body = r.json()
        said = _brief(body, 240)
    except ValueError:
        said = " ".join((r.text or "").split())[:240]
    return f"HTTP {r.status_code}" + (f" · {said}" if said else "")


class VideoError(Exception):
    """A failure worth showing the user: the message is already in the request language.

    Defined here rather than imported from `video` — that module imports this one, so the
    dependency cannot run both ways. `video.generate` does not care which class it catches; it
    catches `httpx`/its own and lets anything else surface.
    """


__all__ = ["KIND", "WORKFLOWS", "DEFAULT_WORKFLOW", "workflow_of", "graph", "payload_for",
           "frames_for", "size_for", "submit", "status_of", "link_of", "probe",
           "VideoError", "NEGATIVE",
           # the user's own workflows: where they come from, what they are, and the resolve that
           # keeps "which one actually ran" answerable
           "use_folder", "reload_user", "parse_row", "available", "names", "user_errors",
           "resolve", "render_graph", "WORKFLOW_SUFFIX", "SETUP_WORKFLOWS", "setup_needs",
           "setup_diagnosis",
           # handing a local file to the instance, and reading a graph for what it can accept
           "upload", "wants", "UPLOAD_ROUTE", "MAX_UPLOAD_BYTES"]
