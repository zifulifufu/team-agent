"""What every media generator shares: which provider kinds are generators rather than chat
models, where their output is allowed to land, and whether a non-local one may run at all.

There are two generators now (video and image) with more than one provider kind between
them, so the questions "is this a chat model?" and "can the video tool drive this provider?"
stopped being the same question. Conflating them would let the video tool pick an image
provider — a failure that looks like a broken server rather than a wrong lookup.
"""

from __future__ import annotations

import itertools
import os
import time
from pathlib import Path

import httpx

from . import i18n
from .coderun import inside as _inside

# Every provider kind that produces media instead of holding a conversation. This is the
# list `store.list_models()` filters by, so a member can never be pointed at a generator.
# Each generator imports its own subset for picking a provider (`video.KINDS`,
# `imagegen.KINDS`) — add the new kind here **and** to that subset.
#
# `metachat_media` covers both media: its one key reaches an image API *and* a video API, so it
# belongs in both subsets rather than being configured twice under two names. `ark_video` is
# Volcengine's own video API (Doubao Seedance), one medium and its own job shape again.
# `comfyui` is a graph runner on the user's own machine (`app/comfyui.py`): no key, no bill, and
# the thing that is chosen is a workflow rather than a model name.
MEDIA_KINDS: tuple[str, ...] = ("minimax_video", "metachat_media", "ark_video", "openai_image",
                                "comfyui")

# The two answers `purpose_of` gives for something a generator can make. A member may *be* one of
# these (see `member_target`), which is why the pair is named here rather than spelled out at each
# place that needs it.
MEDIA_USES: tuple[str, ...] = ("image", "video")

# A member whose whole job is to generate: it does not hold a conversation, it takes the user's
# sentence as the prompt and runs the generator. Recorded in `agents.origin`, the same column that
# already distinguishes "a member I made" from "a member the app made out of a model".
MEDIA_ORIGIN = "media"


def may_host(agent: dict | None) -> bool:
    """Can this member run a group chat — decide how the work is split, and be addressed as the one
    in charge? Two kinds cannot, for two different reasons:

    * an **external** member (`engine`) thinks with its own tools and does not take part in the
      `<plan>` protocol, so there is nothing here for it to decide;
    * a **generating** member (`origin == MEDIA_ORIGIN`) has no judgement to add — its turn is the
      user's own sentence handed straight to a generator.

    ⚠️ This is the **only** place that answers it, on purpose. It used to be answered twice — the
    API refused such a group outright, while the orchestrator quietly handed the chair to somebody
    else — and the client could not ask at all, so the "who is in this group" picker offered every
    one of them as a candidate. Choosing one as the first member then failed the whole creation at
    the last moment, which reads as "the members I chose never joined".

    Note what is *not* here: a generating member is a legitimate **member** (you can address it and
    it will make the thing). It just cannot be the one who runs the room.
    """
    if agent is None:
        return False
    return not agent.get("engine") and (agent.get("origin") or "") != MEDIA_ORIGIN


def member_target(store, agent: dict) -> dict | None:
    """What a media member generates with: `{"model", "provider", "use"}` — or None.

    None means "not a media member" as well as "one whose model has gone": a deleted provider takes
    its models with it, and a member left pointing at nothing must be reported as broken rather
    than quietly falling back to the chat roster (where it would then answer questions it cannot).

    The test is on the **model**, not the provider kind, because a chat gateway can serve image
    models under its own key — MetaChat's OpenAI-compatible address reports eleven of them — and
    pointing a member at one of those fails in exactly the same way.
    """
    if (agent.get("origin") or "") != MEDIA_ORIGIN:
        return None
    row = store.get_model(str(agent.get("model_id") or ""))
    if not row:
        return None
    use = row.get("use") or purpose_of(row.get("model_name", ""))
    if use not in MEDIA_USES:
        return None
    prov = store.get_provider(row["provider_id"])
    if not prov:
        return None
    return {"model": row, "provider": prov, "use": use}

# MetaChat's open media API, one row per model: what it is for, where its job is submitted and
# polled, which parameters its own page of the documentation lists, and where the finished file
# is in the reply.
#
# A table rather than one request shape for the whole prefix, because the paths and the parameters
# genuinely differ. Grok and the image families share the generic `image/generate` and
# `video/generate`; Midjourney has its own `midjourney/imagine` and `midjourney/video` and polls
# both on `midjourney/result/{id}`. The knobs are not the same words either — `duration` means
# nothing to Midjourney (its clips are 5 seconds), whose own parameters are motion / loop / count.
#
# `params` lists **only** what that model's documentation shows. Anything left out takes the
# provider's documented default, which is the safe direction: a parameter a model does not
# recognise comes back as a failure the user has paid for.
#
# `file` is where the result is: `video_url` for a clip, `image_urls` (a list) on the generic image
# path, `image_url` (one picture) for Midjourney.
#
# ⚠️ These ids are MetaChat's *API* catalogue. Its website also offers Seedance, Sora, Kling and
# Veo video; none of those is reachable with a key, which is why they are absent.
MEDIA_MODELS: dict[str, dict] = {
    # ---- video
    "grok-imagine-video-1.5-preview": {
        "use": "video", "submit": "video/generate", "result": "video/result/{vid}",
        "params": ("duration", "ratio", "resolution"), "file": "video_url",
    },
    "mj-video-v1": {
        "use": "video", "submit": "midjourney/video", "result": "midjourney/result/{vid}",
        "params": ("resolution",), "file": "video_url",
    },
    # ---- images
    "grok-imagine-image-2.0": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num", "aspect"), "file": "image_urls",
    },
    "grok-imagine-image-quality": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num", "aspect"), "file": "image_urls",
    },
    "flux-2-pro": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "flux-2-max": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "flux-kontext": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "z-image-turbo": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("aspect",), "file": "image_urls",
    },
    "doubao-seedream-5-0-pro-260628": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num",), "file": "image_urls",
    },
    "doubao-seedream-5-0-260128": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num",), "file": "image_urls",
    },
    "doubao-seedream-4-5-251128": {
        "use": "image", "submit": "image/generate", "result": "image/result/{vid}",
        "params": ("num",), "file": "image_urls",
    },
    # Midjourney's own image endpoint returns one four-up picture rather than a list, and takes its
    # own parameter set (stylize, quality, chaos, style, seed…); only the aspect ratio is wired, so
    # the rest stays at Midjourney's defaults.
    "mj-v82": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "mj-v81": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "mj-v7": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
              "params": ("aspect",), "file": "image_url"},
    "mj-v61": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "niji-7": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
    "niji-6": {"use": "image", "submit": "midjourney/imagine", "result": "midjourney/result/{vid}",
               "params": ("aspect",), "file": "image_url"},
}

# The media APIs whose model list cannot be asked for. Neither Volcengine's Ark nor MetaChat's open
# media API publishes one (`/open/v1/models`, `/open/v1/video/models` and Ark's `/models` all answer
# 404 or nothing), so there is nothing to refresh against and these ids ship with the app — the
# presets seed from here and `discovery` answers a refresh with the same list. Grouped by what a
# model is for, which is what the image and video settings ask for.
BUILTIN_MEDIA_MODELS: dict[str, dict[str, tuple[str, ...]]] = {
    "metachat_media": {
        use: tuple(mid for mid, m in MEDIA_MODELS.items() if m["use"] == use)
        for use in ("image", "video")
    },
    # ⚠️ One model, and it is the one its own documentation names: `doubao-seedance-2-5-260628`.
    # BytePlus (the international face of the same platform) calls it
    # `dreamina-seedance-2-5-260628`; a key issued in one region does not authenticate against the
    # other, so the address and this id have to belong to the same account.
    "ark_video": {"video": ("doubao-seedance-2-5-260628",)},
    # ⚠️ These are **workflow** names, not checkpoints: ComfyUI has no model catalogue to ask, and
    # what the user picks there is a graph this app ships (`comfyui.WORKFLOWS`, which names the
    # checkpoint/encoder/VAE files inside it). A test pins the two lists together, because a name
    # here that no workflow answers to would be a settings entry that fails on submit.
    # The second video name is declared rather than installed: ComfyUI has the nodes, several GB of
    # weights are missing. It is in the list on purpose — the dropdown entry is how the user finds
    # out what a talking-head workflow would need, and `probe` names the files.
    # ⚠️ Grouped by **what the workflow makes**, and it has to stay that way: the image entry is
    # offered under Image generation and the video ones under Video, so a name in the wrong group is
    # a dropdown entry that fails on submit (`comfyui.image_payload_for` refuses a video workflow
    # and `payload_for` refuses a picture one). The pinning test checks this grouping, not just the
    # union — before `z-image-turbo` existed both lists were the same set, which is exactly how a
    # test can pass while the invariant it names is not the one being held.
    "comfyui": {
        "video": ("wan2.2-ti2v-5b", "infinite-talk"),
        "image": ("z-image-turbo",),
    },
}

# For `purpose_of`: a model we ship a job for is what that job says it is. Neither "mj-v82" nor
# "z-image-turbo" is a word the name rules below could be expected to recognise.
#
# The shipped media ids are merged in on top: `comfyui`'s names are workflows rather than provider
# models, so they are in `BUILTIN_MEDIA_MODELS` instead of `MEDIA_MODELS` and would otherwise be
# left to the name rules. ("wan2.2-ti2v-5b" happens to hit the `wan2` needle today; relying on
# that would make the value of a setting depend on a substring.)
_MEDIA_MODEL_USES: dict[str, str] = {
    **{mid.lower(): m["use"] for mid, m in MEDIA_MODELS.items()},
    **{mid.lower(): use for kinds in BUILTIN_MEDIA_MODELS.values()
       for use, ids in kinds.items() for mid in ids},
}

# The generic paths, for a model name typed by hand rather than picked from the shipped list. The
# names on the settings pages are free text (a newer model should not need a release of this app),
# so a model we have no description for still gets a request — just one with no parameters, which
# is the only thing that can be sent without knowing what the model accepts.
_MEDIA_GENERIC: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "image": ("image/generate", "image/result/{vid}", (), "image_urls"),
    "video": ("video/generate", "video/result/{vid}", (), "video_url"),
}


def job_of(model: str, use: str) -> dict:
    """How to run one media job: where it is submitted, where it is polled, which parameters it
    takes, and where the finished file is in the reply.

    `use` is checked rather than trusted: pointing the image tool at a video model is a configuration
    mistake, and answering it with the image job's paths would turn that into a confusing HTTP error.
    """
    m = MEDIA_MODELS.get((model or "").strip())
    if m and m.get("use") == use:
        return m
    submit, result, params, field = _MEDIA_GENERIC[use]
    return {"use": use, "submit": submit, "result": result, "params": params, "file": field}


# ------------------------------------------------- which model a provider will actually run
# Which kinds' catalogues are **closed**: the provider accepts exactly the ids this build ships and
# nothing else, so a name outside the list is a request that will be refused (measured: MetaChat
# answers HTTP 200 carrying `Invalid enum value. Expected 'flux-kontext' | … received 'gpt-image-1'`,
# and nothing used to read that sentence).
#
# ⚠️ `comfyui` is deliberately **not** here. Its "models" are **workflow** names, and a user adds one
# by writing a file into `workflows/` (`comfyui.reload_user` / the layout registries) — the shipped
# list is a starting point, not a catalogue. Correcting a name against it would replace the user's
# own graph with a built-in one, which is the opposite of what that registry exists for. Its test
# says so in as many words: a user's own `my-i2v` graph is what should run.
CLOSED_MODEL_KINDS: tuple[str, ...] = ("metachat_media", "ark_video")


def shipped_models(kind: str, use: str) -> tuple[str, ...]:
    """The model ids this build knows a provider of `kind` serves for `use`.

    Empty for a kind whose models are discovered (a chat gateway's image models are read off its
    `/models`) or typed by hand — there is nothing to check a name against, and a check invented
    there would refuse a newer model the service added, which is the one thing free text is for.
    """
    return tuple(BUILTIN_MEDIA_MODELS.get(str(kind or ""), {}).get(use, ()))


def reconcile_model(provider: dict, use: str, want: str, *, when_empty: str = "first") -> tuple[str, str]:
    """(the model to ask for, a note when it is not the name that was configured).

    Why this exists. A provider whose catalogue is fixed publishes no list to read, so the only
    place this app can know its ids is `BUILTIN_MEDIA_MODELS`. Nothing used to reconcile that list
    with the *setting* — and the drawing setting ships with another provider's id as its default.
    So a group with MetaChat's media API chosen sent `gpt-image-1`, got HTTP 200 carrying
    `Invalid enum value. Expected 'flux-kontext' | ... received 'gpt-image-1'`, retried the same
    wrong call three times, and had its task marked failed: one whole round, no picture. The
    service had named the exact fix in its own reply and nothing read it.

    Substituting silently would be the other half of the same mistake — a user who believes a
    picture was drawn by one model when another drew it. So the note travels back with the name,
    and the caller puts it in what the member is told.

    **Only for the settings value.** A generating member's own model is what the user chose when
    they created that member, and it is passed through untouched (see `callers`).

    `when_empty` decides what an unset name means. `"first"` takes this kind's own first shipped
    model; `""` (what both callers pass) leaves it empty so the caller refuses and names the setting.
    ⚠️ An unset model has to be **reported rather than guessed**: behind one MetaChat key sit fifteen
    drawing models, and picking one at random spends the user's points on a medium or a look nobody
    asked for. Guessing is only for a name that is *set* and provably belongs to another kind.
    """
    kind = str(provider.get("kind") or "")
    name = (want or "").strip()
    # A kind whose names are user-extensible is never corrected — see `CLOSED_MODEL_KINDS`.
    if kind not in CLOSED_MODEL_KINDS:
        return name, ""
    known = shipped_models(kind, use)
    if not known:
        return name, ""
    hit = next((k for k in known if k.lower() == name.lower()), "")
    if hit:
        return hit, ""
    if not name:
        return (known[0] if when_empty == "first" else ""), ""
    picked = known[0]
    return picked, i18n.pick_now(
        f"\"{name}\" is not a model \"{provider.get('name') or provider.get('kind')}\" serves, so "
        f"\"{picked}\" was used instead. The ids it accepts are: {', '.join(known)}. Change the "
        "setting if you meant a different one.",
        f"「{name}」不是「{provider.get('name') or provider.get('kind')}」服务的模型,已改用"
        f"「{picked}」。它接受的 id 是:{', '.join(known)}。如果你要的是别的,请改设置。")


# --------------------------------------------------- MetaChat's reply envelope, read in both tools
#
# The video and image tools talk to the same service and get the same envelope back, so the three
# ways of reading it live here rather than being written twice and drifting apart.
def meta_ok(body: object) -> bool:
    """`status: "Success"` means the call worked. An absent one counts as success too: refusing a
    body that simply does not carry the field would fail over a cosmetic difference."""
    if not isinstance(body, dict):
        return False
    return str(body.get("status") or "").strip().lower() in ("", "success")


def meta_said(body: object) -> str:
    """The sentence out of the envelope.

    MetaChat answers 200 even when it refuses, putting the verdict in `status` and the reason in
    `message`, so a status code is no help and the body is the only place the real reason exists.
    """
    if isinstance(body, dict):
        said = " ".join(str(body.get("message") or "").split())
        return said[:300] or str(body)[:200]
    return str(body)[:200]


def job_links(data: object, field: str) -> list[str]:
    """The finished files in a job's `data`, as a list.

    One field per family: `video_url` for a clip, `image_urls` on the generic image path where
    several may come back, and `image_url` for Midjourney's single four-up picture. Normalising here
    keeps the tools from each growing their own "is it a list or a string" branch.
    """
    if not isinstance(data, dict):
        return []
    got = data.get(field)
    if isinstance(got, str):
        return [got] if got else []
    if isinstance(got, list):
        return [u for u in got if isinstance(u, str) and u]
    return []


# Where the probe asks about a task that cannot exist. Any documented path proves the same two
# things — the service answers, and it takes this key — and this one is shared by every model
# family, so the probe does not need to know which model is configured. It is a *video* result
# path even when the question is about drawing: the point is the service, not the medium.
META_PROBE = "video/result/{vid}"


async def probe_meta(base: str, key: str, *, client: "httpx.AsyncClient") -> tuple[bool, str]:
    """Is MetaChat's media API there, and does it accept this key? Renders and draws nothing.

    Asking about a task id that cannot exist. What comes back for that, measured against the live
    service with a real key, is **HTTP 500 carrying `{"status": "Fail", "message": "视频任务查询失败:
    Video task not found"}`** — not the 404 an H3-style server would give, and not the 200-with-Fail
    the rest of this API uses. So the **status code is no help here** and the only answer that means
    "something is really wrong" is 401/403; everything else, 500 included, proves the request was
    authenticated and processed, which is all this probe claims to establish.

    ⚠️ Treating `>= 400` as unreachable was the earlier mistake, and it was invisible for a while
    because the only key to hand then was an invalid one that answered 401. **A working key reported
    "the service answered HTTP 500", i.e. a healthy provider called broken.** When a probe's verdict
    depends on the failure convention of the service, assert the convention you actually observed.

    Shared by both tools rather than written twice: one key, one host, one question — and a second
    copy could only drift into asking it differently.
    """
    url = api_url(base, META_PROBE.format(vid="team-agent-probe"))
    try:
        r = await client.get(url, headers=auth_headers(key))
    except httpx.HTTPError as e:
        return False, i18n.pick_now(
            f"Could not reach {base}: {type(e).__name__}: {e}", f"连不上 {base}:{type(e).__name__}: {e}"
        )
    if r.status_code in (401, 403):
        return False, i18n.pick_now(
            f"{base} is up but rejected the key ({r.status_code}). MetaChat's key is created under "
            "your avatar → API management; the media API uses the same one.",
            f"{base} 是活的,但密钥被拒绝了({r.status_code})。MetaChat 的密钥在头像 → API 管理里创建,"
            "媒体接口用的是同一把。",
        )
    said = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            said = " ".join(str(body.get("message") or "").split())[:160]
    except ValueError:
        said = " ".join((r.text or "").split())[:160]
    tail = f" · {said}" if said else ""
    if r.status_code >= 400:
        # Reachable, and the key was accepted — otherwise this would have been 401/403. The body is
        # quoted so a service that is up but broken is not silently called healthy either.
        return True, i18n.pick_now(
            f"{base} accepted the key: it answered {r.status_code} for a task that does not exist{tail}.",
            f"{base} 接受了密钥:对不存在的任务返回了 {r.status_code}{tail}。",
        )
    return True, i18n.pick_now(
        f"{base} accepted the key and answered {r.status_code}{tail}.",
        f"{base} 接受了密钥,返回 {r.status_code}{tail}。",
    )


# ------------------------------------------------------------------ what a model is for
#
# A gateway lists everything on one endpoint, so a provider's model list mixes chat models with
# models that cannot hold a conversation at all: asking `gpt-image-1.5` to answer a question, or
# pointing a member at it, fails — and it fails after the user has picked it, which is the worst
# moment. The listing itself is the authority when it says what a model is for (MetaChat sends
# `mode` per entry); reading the name is only a fallback, and it is treated as the guess it is.
#
# Guessing wrong in the "chat" direction costs nothing; guessing wrong towards "image" would make
# a perfectly good chat model look unusable. So the name rules are narrow and specific, and
# anything unrecognised stays `chat`.
#
# The four answers `purpose_of` can give are `chat`, `image`, `video` and `responses` (OpenAI's
# other API shape — not a medium, but the reason a member pointed at one would fail).

# `mode` values as providers spell them. Unknown values fall through to the name rules rather
# than being forced into a bucket.
_MODE_PURPOSE: dict[str, str] = {
    "chat": "chat", "completion": "chat", "completions": "chat", "conversation": "chat",
    "image_generation": "image", "image": "image", "images": "image",
    "video_generation": "video", "video": "video", "videos": "video",
    "responses": "responses",
}

# Whole words and distinctive stems, not substrings: `-image` catches gemini-2.5-flash-image and
# qwen-image, and nothing in the chat catalogue happens to contain it.
_NAME_PURPOSE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("image", ("dall-e", "gpt-image", "-image", "image-", "flux", "seedream", "midjourney",
               "stable-diffusion", "sdxl", "imagen-", "nano-banana", "seededit")),
    ("video", ("sora", "veo-", "veo2", "veo3", "kling", "runway", "pika", "luma",
               "seedance", "hailuo", "cogvideo", "mochi", "-video", "video-", "wan2")),
)


def purpose_of(model_name: str, mode: str | None = None) -> str:
    """What a model is for: `chat` / `image` / `video` / `responses`.

    The provider's own answer wins. `responses` is not a medium but a different API shape — those
    models answer on `/responses` rather than `/chat/completions`, so a member pointed at one
    would fail for a reason nobody could guess from the name.
    """
    said = _MODE_PURPOSE.get((mode or "").strip().lower())
    if said:
        return said
    low = (model_name or "").strip().lower()
    known = _MEDIA_MODEL_USES.get(low)
    if known:
        return known
    for purpose, needles in _NAME_PURPOSE:
        if any(n in low for n in needles):
            return purpose
    return "chat"


def is_chat_model(model_name: str, mode: str | None = None) -> bool:
    """Can a member be pointed at this model?"""
    return purpose_of(model_name, mode) == "chat"

# Names for common unit sizes, so a failure reads "3.2 MB" instead of "3355 KB".
_KB = 1024


def size_label(n: int) -> str:
    return f"{n / _KB:.0f} KB" if n < _KB * _KB else f"{n / _KB / _KB:.1f} MB"


def offline_reason(provider: dict, cfg: dict, what: str, what_zh: str) -> str:
    """The rule the routing layer uses: a non-local provider is out of bounds while outbound
    calls are off. Empty string = allowed."""
    if provider["is_local"] or cfg.get("external_calls_enabled"):
        return ""
    return i18n.pick_now(
        f"\"{provider['name']}\" is not marked as local and outbound calls are switched off, so no "
        f"{what} was generated. Mark it local if it really runs on your own machine, or turn "
        "outbound calls back on.",
        f"「{provider['name']}」不是本地服务,而「允许外呼」是关的,所以没有生成{what_zh}。"
        "如果它确实跑在你自己的机器上,请把它标为本地;否则请打开「允许外呼」。",
    )


def providers_of_kind(store, kinds: tuple[str, ...]) -> list[dict]:
    """The configured providers that generate this kind of media (`imagegen.KINDS`,
    `video.KINDS`) — never a chat model, and never the other generator's kind."""
    return [p for p in store.list_providers() if p["kind"] in kinds]


def api_url(base: str, path: str) -> str:
    """Join a base URL and an API path, tolerating a base that already ends in /v1."""
    b = (base or "").strip().rstrip("/")
    p = path.lstrip("/")
    if p.startswith("v1/") and b.endswith("/v1"):
        p = p[3:]
    return f"{b}/{p}"


def auth_headers(key: str) -> dict[str, str]:
    """The headers both media generators send: JSON, plus a bearer token when there is one."""
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def slug(text: str, limit: int = 40) -> str:
    """A filename-safe fragment of a prompt, so an output file is recognisable later."""
    import re
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip(), flags=re.UNICODE).strip("-")
    return s[:limit].strip("-")


def save_bytes(data: bytes, workspace: Path, *, subdir: str, ext: str, stem_source: str,
               fallback: str, what: str, what_zh: str) -> Path:
    """Write generated media into the group's workspace, and nowhere else.

    Four properties, each for a concrete failure:

    * the directory is checked, not assumed — a member can create `video` (or `image`) as a
      symlink to somewhere else with `run_code`, and `mkdir(exist_ok=True)` would happily
      write through it;
    * **the check and the write are the same object.** Testing the path and then using it again
      leaves a window: a member's own process runs beside this one and can replace the
      directory with a symlink in between, and `O_NOFOLLOW` on the file only covers the last
      component. So the directory is opened `O_NOFOLLOW|O_DIRECTORY` and everything after that
      goes through that descriptor — a directory swapped in mid-flight makes the open fail, or
      is simply not the one the bytes are written into, instead of redirecting them;
    * the staging file is created with O_EXCL|O_NOFOLLOW, so a symlink planted at that name
      cannot make this truncate something outside the workspace, and two simultaneous saves
      cannot share one staging file;
    * publishing uses `link`, which fails if the destination already exists, so a file never
      overwrites something that appeared while it was downloading, and never appears
      partially — the name only comes into existence when the bytes are complete.
    """
    root = workspace.resolve()
    out_dir = workspace / subdir
    if out_dir.is_symlink():
        raise ValueError(i18n.pick_now(
            f"\"{out_dir}\" is a symlink, so the {what} was not saved. Runs keep their output in a "
            "real directory inside the workspace.",
            f"「{out_dir}」是一个符号链接,所以{what_zh}没有保存。产物必须放在工作目录里的真实目录中。",
        ))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not _inside(root, out_dir):
        raise ValueError(i18n.pick_now(
            f"\"{out_dir}\" resolves outside the workspace, so the {what} was not saved.",
            f"「{out_dir}」解析后在工作目录之外,{what_zh}没有保存。",
        ))
    # Opened once and used for every operation below: opening a symlink with O_NOFOLLOW fails,
    # which is also the re-check of the two tests above at the moment that matters.
    try:
        dir_fd = os.open(out_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    except OSError as e:
        raise ValueError(i18n.pick_now(
            f"\"{out_dir}\" could not be opened for writing ({e}), so the {what} was not saved.",
            f"无法打开「{out_dir}」写入({e}),{what_zh}没有保存。",
        )) from None
    try:
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(stem_source) or slug(fallback) or 'output'}"
        staging = f".{stem}.{os.getpid()}.part"
        try:
            fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
                         dir_fd=dir_fd)
        except OSError as e:
            raise ValueError(i18n.pick_now(
                f"Could not create the staging file in {out_dir}: {e}",
                f"无法在 {out_dir} 里创建临时文件:{e}",
            )) from None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            for n in itertools.count(1):
                name = f"{stem}{ext}" if n == 1 else f"{stem}-{n}{ext}"
                try:
                    os.link(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except FileExistsError:
                    continue
                return out_dir / name
        finally:
            try:
                os.unlink(staging, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(dir_fd)
