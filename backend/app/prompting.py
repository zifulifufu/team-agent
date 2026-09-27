"""Prompt assembly: global system prompt + member persona + extra prompt + group prompt +
group/member skills + member roster (with strengths) + memory + tools.

Variables (usable in the system prompt, the prompt library and the group prompt; written
as {{variable}}, unknown variables are left as-is):
  {{agent_name}} {{agent_role}} {{group_name}} {{members}} {{model_name}} {{date}} {{time}} {{datetime}} {{weekday}} {{os}} {{username}}
"""

from __future__ import annotations

import platform
import re
from datetime import datetime

from . import i18n
from . import media, cooperation
from .presets import localize_member
from .router import ModelRouter
from .store import Store
from .tools import skills_prompt

WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "zh": ("一", "二", "三", "四", "五", "六", "日"),
}
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# name, English description, Chinese description — the Prompts page shows one of them.
VARIABLES = [
    ("agent_name", "the name of the member speaking now", "当前发言成员的名字"),
    ("agent_role", "the role of the member speaking now", "当前发言成员的岗位"),
    ("group_name", "the name of the group chat", "群聊名称"),
    ("members", "the list of group members", "群成员名单"),
    ("model_name", "the model this member is using", "当前成员使用的模型"),
    ("date", "today's date", "今天的日期"),
    ("time", "the current time", "当前时间"),
    ("datetime", "date plus time", "日期加时间"),
    ("weekday", "the day of the week", "星期几"),
    ("os", "the operating system", "操作系统"),
    ("username", "how you are addressed", "你的称呼"),
]


def estimate_tokens(text: str) -> int:
    """Rough token count: about 1 token per CJK character, about 1 token per 4 characters
otherwise. For reference only."""
    cjk = len(re.findall(r"[㐀-鿿]", text))  # i18n-keep: counts CJK characters to estimate tokens
    return cjk + (len(text) - cjk + 3) // 4


def render_vars(text: str, values: dict[str, str]) -> str:
    return VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def merge_strengths(agent: dict, model: dict | None) -> list[str]:
    """A member's strengths = the tags the role needs + the model's own strengths
(role tags first), at most 6."""
    out: list[str] = []
    for t in list(agent.get("tags") or []) + list((model or {}).get("strengths") or []):
        if t not in out:
            out.append(t)
    return out[:6]


class PromptBuilder:
    def __init__(self, store: Store, router: ModelRouter):
        self.store, self.router = store, router

    # ------------------------------------------------------------------ members
    @staticmethod
    def _shown(agent: dict | None) -> dict | None:
        """A member as it should read in the request language.

        The role and prompt of a seeded member sit in the database in whichever language
        the install was created in, so they are swapped here before they reach the model
        — otherwise an English run would carry Chinese role text into the prompt.
        """
        return localize_member(agent, i18n.current()) if agent else agent

    # ----------------------------------------------------------------- headings
    @staticmethod
    def _heading(en: str, zh: str) -> str:
        """A section heading in the request language."""
        return f"[{en}]" if i18n.current() == "en" else f"【{zh}】"  # i18n-keep: already bilingual: English gets [Head], Chinese gets 【Head】

    @staticmethod
    def _roster_hint() -> str:
        return i18n.pick_now(
            " (strengths = what the role needs plus what the chosen model is good at; split the work "
            "by strength rather than taking someone else's job)",
            "(强项 = 岗位所需 + 所用模型的擅长;分工时按强项来,不要抢别人的活)")

    # --------------------------------------------------------------- variables
    def values(self, group: dict, agent: dict | None, members: list[dict], user_name: str = "") -> dict[str, str]:
        now = datetime.now()
        lang = i18n.current()
        agent = self._shown(agent)
        model = self.router.resolve(agent["model_id"], agent.get("tags")) if agent and not agent.get("engine") else None
        return {
            "agent_name": agent["name"] if agent else "",
            "agent_role": (agent["role"] or i18n.pick(lang, "Member", "成员")) if agent else "",
            "group_name": group["name"],
            "members": i18n.pick(lang, ", ", "、").join(m["name"] for m in members),
            "model_name": (model["display_name"] if model else (agent["name"] if agent and agent.get("engine") else "")),
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "datetime": now.strftime("%Y-%m-%d %H:%M"),
            "weekday": (WEEKDAYS[lang] if lang in WEEKDAYS else WEEKDAYS["en"])[now.weekday()]
                       if lang == "en" else "星期" + WEEKDAYS["zh"][now.weekday()],  # i18n-keep: already bilingual: English spells the weekday, Chinese prefixes 星期
            "os": platform.system(),
            "username": user_name or i18n.pick(lang, "the user", "我"),
        }

    # ------------------------------------------------------------------ roster
    def roster_entries(self, members: list[dict]) -> list[dict]:
        out = []
        for m in (self._shown(x) or x for x in members):
            if m.get("engine"):   # external agent: does not go through model routing
                out.append({"agent": m, "model": None, "strengths": merge_strengths(m, None),
                            "model_display": i18n.pick_now(f"external agent ({m['name']})",
                                                           f"外部智能体({m['name']})")})
                continue
            if (m.get("origin") or "") == media.MEDIA_ORIGIN:
                # A generating member has no conversation and no tools to choose from: its one turn
                # runs its own generator, and which generator that is comes from the model it was
                # made from (`media.member_target` → "image" | "video") — never from the task text.
                #
                # ⚠️ Measured failure without this branch. The host read a role of "Media member ·
                # Video generation · …" as "a member that makes media", planned 「关键帧静帧生成」,
                # and handed it to a **video** member — which can only call `generate_video`, so it
                # died on the missing first frame and took four downstream tasks (§ t4/t6/t7
                # skipped) with it. The roster said the same thing about it as about a chat member,
                # so the host had nothing to plan against. `member_target` is the one place that
                # knows what a generating member can make; this asks it rather than guessing from
                # the name or the tags.
                t = media.member_target(self.store, m)
                out.append({
                    "agent": m, "model": None, "strengths": [],
                    "media_use": (t or {}).get("use") or "gone",
                    "model_display": i18n.pick_now(
                        f"generating member ({m['name']}) — makes video only" if (t or {}).get("use") == "video"
                        else f"generating member ({m['name']}) — makes images only" if t
                        else f"generating member ({m['name']}) — its model is gone, it can do nothing",
                        f"生成成员({m['name']}) —— 只会做视频" if (t or {}).get("use") == "video"
                        else f"生成成员({m['name']}) —— 只会做图片" if t
                        else f"生成成员({m['name']}) —— 它依据的模型已不在,什么也做不了"),
                })
                continue
            model = self.router.resolve(m["model_id"], m.get("tags"))
            out.append({
                "agent": m, "model": model, "strengths": merge_strengths(m, model),
                "model_display": model["display_name"] if model else i18n.pick_now(
                    "(no model available)", "(暂无可用模型)"),
            })
        return out

    def roster_text(self, members: list[dict], me_id: str | None = None, host_id: str | None = None) -> str:
        lang = i18n.current()
        sep = i18n.pick(lang, ", ", "、")
        lines = []
        for e in self.roster_entries(members):
            m = e["agent"]
            bits = [f"{m['name']}({m['role'] or i18n.pick(lang, 'Member', '成员')})"]
            if m["id"] == host_id:
                bits.append(i18n.pick(lang, " (host)", "〔群主〕"))
            if m["id"] == me_id:
                bits.append(i18n.pick(lang, " <- this is you", "← 这是你"))
            line = "- " + "".join(bits)
            line += i18n.pick(
                lang,
                f" | strengths: {sep.join(e['strengths']) or 'general'} | model: {e['model_display']}",
                f" | 强项:{sep.join(e['strengths']) or '通用'} | 模型:{e['model_display']}",
            )
            if m["skills"]:
                line += i18n.pick(lang, f" | skills: {sep.join(m['skills'])}",
                                  f" | 技能:{sep.join(m['skills'])}")
            # ⚠️ What a generating member *cannot* do is the part a host gets wrong. Saying only
            # "makes video" leaves open "and also, presumably, a picture" — which is how a still-frame
            # task reached a video model. So the sentence names the boundary and the one input it
            # needs, and says plainly that the task text cannot change which generator runs.
            worker = cooperation.contract(self.store, m)
            if worker["mode"] == "listener":
                line += " | " + i18n.pick_now("Listening executor: ", "旁听执行成员:") + worker["summary"] + " " + worker["preparation"]
            if e.get("media_use") == "video":
                line += i18n.pick(
                    lang,
                    " | a generating member: it makes video with its own generator and nothing else"
                    " — no writing, no research, no assembling, it cannot use other tools, and it"
                    " accepts only the inputs described above for its workflow;"
                    " pictures it needs are somebody else's task, not its own",
                    " | 生成成员:只会用它自己的生成器做视频,别的都不会(不写字、不查资料、不装配,"
                    "也不能调用其他工具);输入要求以它上面列出的工作流能力为准。"
                    "它需要的前置图片是别人的任务,不是它的。")
            elif e.get("media_use") == "image":
                line += i18n.pick(
                    lang,
                    " | a generating member: it makes pictures with its own generator and nothing"
                    " else — no writing, no research, no assembling, and it cannot use other tools.",
                    " | 生成成员:只会用它自己的生成器做图片,别的都不会(不写字、不查资料、不装配,"
                    "也不能调用其他工具)。")
            lines.append(line)
        if any(cooperation.is_listener(m) for m in members):
            lines.append(i18n.pick_now(
                "Coordination: listening executors do not debate or respond to @all. Use them when their capability fits. Put multi-step work on the task board with preparation → execution → review/assembly dependencies. For a direct handoff, write @name followed by one concrete instruction and real input paths. A mention triggers execution: do not @mention a tool merely to discuss it. After it returns, its assigning member resumes to check the actual files, handle failures and finish the delivery. Never rerun a completed or failed generation blindly, or claim a file exists without evidence.",
                "协作规则:旁听执行成员不参与闲聊或 @all 讨论,能力合适时应主动派给它们。多步骤任务写进任务板,按准备素材/工程→执行→验收/装配建立依赖。直接接力时写 @名字 加一项具体指令和真实输入路径。点名会执行,介绍工具时不要加 @。工具返回后派工成员会继续,负责核对实际文件、处理失败并完成交付。不要盲目重复已经完成或失败的生成,没有文件证据不能宣称完成。"))
        return "\n".join(lines)

    # ------------------------------------------------------------ system prompt
    def global_extras(self) -> str:
        items = [p for p in self.store.list_prompts() if p["use_globally"]]
        if not items:
            return ""
        head = i18n.pick_now("Additional requirements", "补充要求")
        body = "\n".join(f"- {p['content'].strip()}" for p in items)
        return (f"[{head}]\n{body}" if i18n.current() == "en" else f"【{head}】\n{body}")  # i18n-keep: already bilingual: [Head] vs 【Head】

    def system_prompt(
        self, group: dict, agent: dict, members: list[dict], *,
        memory_block: str = "", tools_block: str = "", extra: str = "",
    ) -> str:
        cfg = self.store.get_settings()
        agent = self._shown(agent) or agent
        members = [self._shown(m) or m for m in members]
        vals = self.values(group, agent, members)
        sk_dir = self.store.data_dir / "skills"
        extras = self.global_extras()
        parts = [
            render_vars(cfg["system_prompt"], vals).strip(),
            render_vars(agent["prompt"], vals).strip(),
            render_vars(extras, vals) if extras else "",
            (self._heading("Group prompt", "本群提示词") + "\n"  # i18n-keep: already bilingual pair
             + render_vars(group["prompt"].strip(), vals)) if (group.get("prompt") or "").strip() else "",
            skills_prompt(sk_dir, group["ext"]["skills"], group=True),
            skills_prompt(sk_dir, agent["skills"]),
            self._heading("Members and their parts", "群成员与分工")  # i18n-keep: already bilingual pair
            + self._roster_hint() + "\n"
            + self.roster_text(members, agent["id"], group.get("host_agent_id")),
            memory_block,
            tools_block,
            extra,
        ]
        return "\n\n".join(p for p in parts if p)
