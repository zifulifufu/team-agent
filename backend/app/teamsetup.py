"""Validate a reviewed lineup before creating any of its new members. No model calls here."""
from . import comfyui, external, i18n, media, presets, templates
from .health import static_state


def model_problem(store, model: dict | None) -> str:
    if not model:
        return i18n.pick_now("Model no longer exists", "模型已不存在")
    provider = store.get_provider(model["provider_id"])
    if not provider:
        return i18n.pick_now("Provider no longer exists", "服务商已不存在")
    cfg = store.get_settings()
    off = static_state(model, provider, bool(cfg["external_calls_enabled"]))
    if off:
        return off[1]
    use = model.get("use") or "chat"
    if use == "responses":
        return i18n.pick_now("Unsupported chat interface", "暂不支持此对话接口")
    if use in ("image", "video") and not cfg[f"{use}_enabled"]:
        return i18n.pick_now("Generation is switched off", "生成开关未开启")
    if provider["kind"] == "comfyui" and model["model_name"] in comfyui.SETUP_WORKFLOWS:
        return i18n.pick_now("Workflow setup needed", "待配置工作流")
    return ""


def validate(store, router, lineup: list[dict], host_ref: str) -> list[tuple[str, dict]]:
    """Return validated descriptors. Resolve/create members only AFTER every descriptor passes."""
    if not lineup:
        raise ValueError(i18n.pick_now("Choose a lineup first", "请先选择阵容"))
    resolved = []
    seen = set()
    for item in lineup:
        kind, ident = item.get("kind"), str(item.get("id") or "")
        ref = f"{kind}:{ident}"
        if ref in seen:
            continue
        seen.add(ref)
        if kind == "agent":
            agent = store.get_agent(ident)
            if not agent or agent.get("hidden"):
                raise ValueError(i18n.pick_now("Member no longer exists", "成员已不存在"))
        elif kind == "model":
            model = store.get_model(ident)
            why = model_problem(store, model)
            if why:
                raise ValueError(f"{ident}: {why}")
            agent = {"name": model["display_name"], "model_id": ident,
                     "origin": "media" if model.get("use") in media.MEDIA_USES else "model"}
        elif kind == "preset":
            preset = presets.AGENT_PRESET_BY_KEY.get(ident)
            if not preset or preset.get("system"):
                raise ValueError(i18n.pick_now("Unknown role preset", "角色预设无效"))
            agent = preset
        else:
            raise ValueError(i18n.pick_now("Unknown member source", "成员来源无效"))
        if agent.get("engine"):
            external.clean_cfg(agent.get("engine_cfg"), engine=agent["engine"])
            if not store.get_settings()["external_agents_enabled"]:
                raise ValueError(i18n.pick_now("External agents are switched off", "外部智能体开关未开启"))
        elif agent.get("model_id"):
            why = model_problem(store, store.get_model(agent["model_id"]))
            if why:
                raise ValueError(f"{agent['name']}: {why}")
        elif not router.resolve(tags=agent.get("tags") or []):
            raise ValueError(i18n.pick_now("No configured chat model can run this member", "没有已配置的对话模型可供成员使用"))
        resolved.append((ref, agent))
    chair = next((a for ref, a in resolved if ref == host_ref), None)
    if not chair or not media.may_host(chair):
        raise ValueError(i18n.pick_now("Choose a conversational host from the lineup", "请从阵容中选择能分工的对话成员作为群主"))
    return resolved


def materialize(store, lineup: list[dict]) -> dict[str, str]:
    out = {}
    for item in lineup:
        kind, ident = item["kind"], item["id"]
        agent = (store.get_agent(ident) if kind == "agent" else store.ensure_model_agent(ident)
                 if kind == "model" else templates.ensure_agent_from_key(store, ident))
        if not agent:
            raise ValueError(i18n.pick_now("The lineup changed; review it again", "阵容已变化,请重新确认"))
        out[f"{kind}:{ident}"] = agent["id"]
    return out
