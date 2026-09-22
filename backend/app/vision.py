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

from . import i18n, images

# Long enough for a dense screenshot with a table in it, short enough not to dominate the prompt.
MAX_DESCRIPTION = 6000


def _can_see(model: dict) -> bool:
    return "multimodal" in (model.get("strengths") or [])


def pick(store: Any, models: list[dict] | None = None) -> dict | None:
    """The model that will look at pictures, or None.

    The user's explicit choice wins. Otherwise any usable multimodal model, local first: a
    description is a reading task, and keeping it on-device means an attached screenshot does not
    leave the machine merely so a member can talk about it.
    """
    cfg = store.get_settings()
    wanted = str(cfg.get("vision_model_id") or "").strip()
    pool = models if models is not None else []
    if wanted:
        found = next((m for m in pool if m["id"] == wanted), None) or store.get_model(wanted)
        if found and _can_see(found):
            return found
        return None
    local = [m for m in pool if _can_see(m) and m.get("is_local")]
    if local:
        return local[0]
    cloud = [m for m in pool if _can_see(m)]
    return cloud[0] if cloud and cfg["vision_cloud"] else None


def status(store: Any, router: Any) -> dict:
    """What the settings page needs to say, including what to do when the answer is "nobody".

    `blocked_cloud` is the case worth naming separately: a model exists, it can see, and the only
    reason nothing happens is the outbound switch.
    """
    cfg = store.get_settings()
    try:
        usable = router.usable_models()
    except Exception:  # noqa: BLE001
        usable = []
    chosen = pick(store, usable)
    seeing = [m for m in usable if _can_see(m)]
    return {
        "model_id": chosen["id"] if chosen else "",
        "model_name": (chosen.get("display_name") or chosen["model_name"]) if chosen else "",
        "is_local": bool(chosen and chosen.get("is_local")),
        "configured": str(cfg.get("vision_model_id") or ""),
        "vision_cloud": bool(cfg["vision_cloud"]),
        "candidates": [{"id": m["id"], "name": m.get("display_name") or m["model_name"],
                        "is_local": bool(m.get("is_local"))} for m in seeing],
        "blocked_cloud": (not chosen) and any(not m.get("is_local") for m in seeing),
    }


def reason_missing(store: Any, router: Any) -> str:
    """One sentence for the reader when nothing can look at a picture."""
    st = status(store, router)
    if st["blocked_cloud"]:
        return i18n.pick_now(
            "An image is attached, but this app is not allowed to send images to a cloud model. "
            "Turn on cloud vision under Settings → General, or set up a local vision model.",
            "附件里有图片,但本应用不被允许把图片发给云端模型。请在「设置 → 通用」里打开云端视觉,"
            "或配置一个本地视觉模型。")
    return i18n.pick_now(
        "An image is attached, but no model here can look at images. Pull a vision model in Ollama "
        "(for example `ollama pull qwen2.5vl:3b`) and pick it under Settings → General → vision model.",
        "附件里有图片,但这台机器上没有能看图的模型。可以用 Ollama 拉一个视觉模型"
        "(例如 `ollama pull qwen2.5vl:3b`),然后在「设置 → 通用 → 视觉模型」里选它。")


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
