"""Who looks at the pictures.

A text model cannot see. A member whose model carries the `multimodal` strength looks at images
itself; for every other member the pictures have to become words first — once per file, by one
chosen model, with the result cached on the attachment row so the second question about the same
screenshot costs nothing.

Three things this module will not do:

  * guess. A picture nobody looked at says so, in the prompt, instead of leaving the member to
    invent content;
  * quietly skip. When no model can look, the failure is a sentence the reader can act on
    (pull a local vision model, or allow cloud vision) rather than silence;
  * send a picture off the machine because it happened to help. Cloud vision is `vision_cloud`,
    a separate decision from "cloud models may be called at all".
"""

from __future__ import annotations

from typing import Any

from . import i18n, images, media

# Long enough for a dense screenshot with a table in it, short enough not to dominate the prompt.
MAX_DESCRIPTION = 6000


def _can_see(model: dict) -> bool:
    """Can this model be *shown* a picture?

    Two things have to hold, and the second one is not decoration. It must carry the `multimodal`
    strength — and it must be a model that holds a conversation, because a generator answers on a
    different endpoint and makes pictures rather than reading them. A gateway lists both under one
    name space, so `gpt-image-2.5-sunburst` sat in the "which model looks at pictures" list looking
    exactly like a model that handles images.
    """
    name = str(model.get("model_name") or model.get("id") or "")
    if media.purpose_of(name, model.get("use")) != "chat":
        return False
    return "multimodal" in (model.get("strengths") or [])


def chosen(store: Any, models: list[dict] | None = None) -> dict | None:
    """The model the user named under Settings → General, if it still exists."""
    wanted = str(store.get_settings().get("vision_model_id") or "").strip()
    if not wanted:
        return None
    pool = models if models is not None else []
    return next((m for m in pool if m["id"] == wanted), None) or store.get_model(wanted)


def pick(store: Any, models: list[dict] | None = None) -> dict | None:
    """The model that will look at pictures, or None.

    The user's choice decides *which* model, out of the ones that may be used at all. It does not
    decide where a picture may go: `vision_cloud` is the switch for that, and it gates an explicitly
    chosen cloud model exactly as it gates the automatic one. It used to be bypassed right here —
    name a cloud model once and every picture in every group went to that provider with the switch
    off, which is the opposite of what the switch says on screen.

    Local first, then: a description is a reading task, and keeping it on-device means an attached
    screenshot does not leave the machine merely so a member can talk about it.
    """
    cfg = store.get_settings()
    pool = models if models is not None else []
    want = chosen(store, pool)
    if want and _can_see(want) and (want.get("is_local") or cfg["vision_cloud"]):
        return want
    # A named model that cannot look does **not** end the search here, which is what it used to do:
    # the choice was honoured to the letter, so one wrong pick turned every picture in every group
    # into "nobody here can look", with nothing on screen connecting the two. The mis-pick is not
    # swallowed either: `status()` reports it and `reason_missing()` names it.
    local = [m for m in pool if _can_see(m) and m.get("is_local")]
    if local:
        return local[0]
    cloud = [m for m in pool if _can_see(m)]
    return cloud[0] if cloud and cfg["vision_cloud"] else None


def status(store: Any, router: Any) -> dict:
    """What the settings page needs to say, including what to do when the answer is "nobody".

    `blocked_cloud` is the case worth naming separately: a model exists, it can see, and the only
    reason nothing happens is the outbound switch. `configured_sees` is the other one: the user
    picked a model for this job and it cannot do it — None when nothing is picked (automatic).
    """
    cfg = store.get_settings()
    try:
        usable = router.usable_models()
    except Exception:  # noqa: BLE001
        usable = []
    eyes = pick(store, usable)
    want = chosen(store, usable)
    seeing = [m for m in usable if _can_see(m)]
    named = str(cfg.get("vision_model_id") or "").strip()
    return {
        "model_id": eyes["id"] if eyes else "",
        "model_name": (eyes.get("display_name") or eyes["model_name"]) if eyes else "",
        "is_local": bool(eyes and eyes.get("is_local")),
        "configured": named,
        "configured_name": (want.get("display_name") or want["model_name"]) if want else named,
        "configured_found": bool(want),
        "configured_sees": (bool(want and _can_see(want)) if named else None),
        "vision_cloud": bool(cfg["vision_cloud"]),
        "candidates": [{"id": m["id"], "name": m.get("display_name") or m["model_name"],
                        "is_local": bool(m.get("is_local"))} for m in seeing],
        "blocked_cloud": (not eyes) and any(not m.get("is_local") for m in seeing),
    }


def _picked_wrong(store: Any, st: dict) -> str:
    """One sentence for "the model you named for this cannot do it", worded for *how* it is wrong.

    Naming it is the whole point of this sentence: the reader is one dropdown change away from
    fixing it, and without the name they are only told "no model here can look at images" while
    looking straight at a model they picked for exactly that.
    """
    name = st["configured_name"]
    if not st["configured_found"]:
        return i18n.pick_now(
            f"An image is attached, but the model picked to look at pictures ({name}) is not here any "
            "more. Pick another under Settings → General → Which model looks at pictures, or set it back "
            "to automatic.",
            f"附件里有图片,但被指定「看图」的模型({name})已经不在了。请在「设置 → 通用 → 看图用哪个模型」里"
            "重新选一个,或改回自动。")
    want = chosen(store, [])
    makes_pictures = bool(want) and media.purpose_of(
        str(want.get("model_name") or ""), want.get("use")) != "chat"
    # A gateway lists chat models and image generators in one list, so "gpt-image-…" reads exactly
    # like a model that *handles* images. Saying which of the two it is stops the reader from
    # re-picking the same kind of model.
    tail = i18n.pick_now(" It is one that makes pictures, not one that reads them.",
                         "它是生成图片的模型,不是读图片的模型。") if makes_pictures else ""
    return i18n.pick_now(
        f"An image is attached, but the model picked to look at pictures ({name}) cannot look at"
        f" images.{tail} Pick one that can see, or set it back to automatic, under Settings → General →"
        " Which model looks at pictures.",
        f"附件里有图片,但被指定「看图」的模型({name})看不了图。{tail}"
        "请在「设置 → 通用 → 看图用哪个模型」里换一个能看图的,或改回自动。")


def reason_missing(store: Any, router: Any) -> str:
    """One sentence for the reader when nothing can look at a picture."""
    st = status(store, router)
    parts: list[str] = []
    if st["configured_sees"] is False:
        parts.append(_picked_wrong(store, st))
    if not st["model_id"]:
        parts.append(i18n.pick_now(
            "An image is attached, but this app is not allowed to send images to a cloud model. "
            "Turn on cloud vision under Settings → General, or set up a local vision model.",
            "附件里有图片,但本应用不被允许把图片发给云端模型。请在「设置 → 通用」里打开云端视觉,"
            "或配置一个本地视觉模型。") if st["blocked_cloud"] else i18n.pick_now(
            "An image is attached, but no model here can look at images. Pull a vision model in Ollama "
            "(for example `ollama pull qwen2.5vl:3b`) and pick it under Settings → General → vision model.",
            "附件里有图片,但这台机器上没有能看图的模型。可以用 Ollama 拉一个视觉模型"
            "(例如 `ollama pull qwen2.5vl:3b`),然后在「设置 → 通用 → 视觉模型」里选它。"))
    if not parts:
        # A model was there and the description still did not come back — saying "nobody can look"
        # here was simply untrue, and sent the reader to check a setting that was already right.
        parts.append(i18n.pick_now(
            "An image is attached, and the model that looks at pictures did not return a description "
            "this time. The attempt is in the app's log.",
            "附件里有图片,但负责看图的模型这次没有返回描述。详情见本程序的日志。"))
    return " ".join(parts)


async def describe(store: Any, router: Any, pictures: list[tuple[str, bytes]], prompt: str) -> str:
    """One model call over one or more pictures -> prose. Raises only on a routing failure, which
    the caller turns into a sentence; it never returns a made-up description."""
    model = pick(store, router.usable_models())
    if not model or not pictures:
        return ""
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for mime, data in pictures:
        parts.append({"type": "image_url", "image_url": {"url": images.data_uri(data, mime)}})
    res = await router.complete([{"role": "user", "content": parts}], only=model["id"], source="vision")
    return (res.text or "").strip()[:MAX_DESCRIPTION]
