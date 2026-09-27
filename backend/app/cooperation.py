"""The executable contract of a listening member, shared by the roster and the UI.

Listening is a participation mode, not an unavailable state. These descriptions do not
claim that an executable or its models are installed; execution still checks readiness.
"""
from . import i18n, localcmd, media


def is_listener(member: dict) -> bool:
    return member.get("origin") == media.MEDIA_ORIGIN or localcmd.row(str(member.get("engine") or "")) is not None


def contract(store, member: dict) -> dict:
    engine = str(member.get("engine") or "")
    row = localcmd.row(engine)
    if row:
        folder = localcmd.folder_name(engine) + "/"
        preparation = i18n.pick_now(
            f"A chat member must first create {row['project']} in {folder}, relative to the group workspace. Assign that preparation as an upstream task; this renderer cannot author it.",
            f"先由对话成员在群工作目录的 {folder} 中创建{row['project_zh']},再作为上游任务交给它;渲染工具不会自己编写工程。",
        ) if row.get("project") else i18n.pick_now(
            "Pass only the exact text to speak in arguments.text. For voice cloning also pass arguments.ref_audio (an existing file inside this group's workspace) and optional ref_text (its transcript). Do not include task instructions or the whole conversation. Each call accepts at most 1000 characters; split longer narration into separate tasks.",
            "用 arguments.text 传入朗读原文；克隆声音还必须传 arguments.ref_audio(本群工作目录中真实存在的录音路径)，可附 ref_text(录音原文)。不要把任务说明或整段对话当作配音内容；每次最多 1000 字，长旁白应拆分任务。")
        return {"mode": "listener", "kind": "local_tool", "tools": [f"local:{engine}"],
                "summary": i18n.pick_now(row["role"], row["role_zh"]),
                "preparation": preparation, "workspace": folder}
    target = media.member_target(store, member)
    if member.get("origin") == media.MEDIA_ORIGIN:
        use = (target or {}).get("use")
        summary = i18n.pick_now("Generates one video clip per task.", "每个任务生成一个视频片段。") if use == "video" else i18n.pick_now("Generates one image per task.", "每个任务生成一张图片。")
        preparation = i18n.pick_now("Provide a concrete prompt and output parameters; editing and assembly belong to a chat member.", "提供具体提示词和输出参数;剪辑、装配由对话成员负责。")
        if not target:
            summary = i18n.pick_now("The generating model is missing.", "生成模型不存在。")
        elif target["provider"]["kind"] == "comfyui":
            from . import comfyui
            workflow = target["model"]["model_name"]
            if workflow == comfyui.DEFAULT_WORKFLOW:
                preparation = i18n.pick_now(
                    "ComfyUI Wan: text-to-video, silent. Use arguments.prompt, duration_seconds and aspect_ratio. This built-in workflow does not accept reference images or audio; add narration and assemble in later tasks.",
                    "ComfyUI Wan:文生视频、无声。用 arguments.prompt、duration_seconds、aspect_ratio 指定画面、时长和比例。当前内置工作流不接收参考图片或音频;配音和装配放在后续任务。")
            elif workflow in comfyui.SETUP_WORKFLOWS:
                preparation = i18n.pick_now("This workflow is a setup placeholder and cannot render yet. Configure a compatible workflow before assigning production.", "这个工作流是待配置入口,目前不能渲染;配置兼容工作流后再派制作任务。")
            else:
                preparation += i18n.pick_now(" ComfyUI references must match the imported workflow's inputs.", " ComfyUI 参考素材必须符合导入工作流的输入要求。")
        elif use == "video" and target["model"]["model_name"] == "mj-video-v1":
            preparation += i18n.pick_now(" Requires a fetchable first_frame image URL from an upstream image task.", " 必须由上游图片任务提供可访问的 first_frame 图片 URL。")
        return {"mode": "listener", "kind": "generator", "tools": ["generate_video" if use == "video" else "generate_image"] if target else [],
                "summary": summary, "preparation": preparation, "workspace": ""}
    return {"mode": "discussion", "kind": "external" if engine else "chat", "tools": [],
            "summary": "", "preparation": "", "workspace": ""}
