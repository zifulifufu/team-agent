"""提示词装配:全局系统提示词 + 成员设定 + 补充提示词 + 群提示词 + 群/成员技能 + 成员分工表(含强项)+ 记忆 + 工具。

变量(在系统提示词、提示词库、群提示词里都能用,写法 {{变量}},未知变量原样保留):
  {{agent_name}} {{agent_role}} {{group_name}} {{members}} {{model_name}} {{date}} {{time}} {{datetime}} {{weekday}} {{os}} {{username}}
"""

from __future__ import annotations

import platform
import re
from datetime import datetime

from .router import ModelRouter
from .store import Store
from .tools import skills_prompt

WEEKDAYS = "一二三四五六日"
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
VARIABLES = [
    ("agent_name", "当前发言成员的名字"), ("agent_role", "当前发言成员的岗位"), ("group_name", "群聊名称"),
    ("members", "群成员名单"), ("model_name", "当前成员使用的模型"), ("date", "今天的日期"), ("time", "当前时间"),
    ("datetime", "日期加时间"), ("weekday", "星期几"), ("os", "操作系统"), ("username", "你的称呼"),
]


def estimate_tokens(text: str) -> int:
    """粗估 token 数:汉字约 1 个/字,其它约 4 字符 1 个。仅供参考。"""
    cjk = len(re.findall(r"[㐀-鿿]", text))
    return cjk + (len(text) - cjk + 3) // 4


def render_vars(text: str, values: dict[str, str]) -> str:
    return VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def merge_strengths(agent: dict, model: dict | None) -> list[str]:
    """成员的强项 = 岗位需要的标签 + 模型自身的强项(岗位在前),最多 6 个。"""
    out: list[str] = []
    for t in list(agent.get("tags") or []) + list((model or {}).get("strengths") or []):
        if t not in out:
            out.append(t)
    return out[:6]


class PromptBuilder:
    def __init__(self, store: Store, router: ModelRouter):
        self.store, self.router = store, router

    # --------------------------------------------------------------- variables
    def values(self, group: dict, agent: dict | None, members: list[dict], user_name: str = "我") -> dict[str, str]:
        now = datetime.now()
        model = self.router.resolve(agent["model_id"], agent.get("tags")) if agent and not agent.get("engine") else None
        return {
            "agent_name": agent["name"] if agent else "", "agent_role": (agent["role"] or "成员") if agent else "",
            "group_name": group["name"], "members": "、".join(m["name"] for m in members),
            "model_name": (model["display_name"] if model else (agent["name"] if agent and agent.get("engine") else "")),
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "datetime": now.strftime("%Y-%m-%d %H:%M"),
            "weekday": "星期" + WEEKDAYS[now.weekday()], "os": platform.system(), "username": user_name,
        }

    # ------------------------------------------------------------------ roster
    def roster_entries(self, members: list[dict]) -> list[dict]:
        out = []
        for m in members:
            if m.get("engine"):   # 外部智能体:不经过模型路由
                out.append({"agent": m, "model": None, "strengths": merge_strengths(m, None),
                            "model_display": f"外部智能体({m['name']})"})
                continue
            model = self.router.resolve(m["model_id"], m.get("tags"))
            out.append({
                "agent": m, "model": model, "strengths": merge_strengths(m, model),
                "model_display": model["display_name"] if model else "(暂无可用模型)",
            })
        return out

    def roster_text(self, members: list[dict], me_id: str | None = None, host_id: str | None = None) -> str:
        lines = []
        for e in self.roster_entries(members):
            m = e["agent"]
            bits = [f"{m['name']}({m['role'] or '成员'})"]
            if m["id"] == host_id:
                bits.append("〔群主〕")
            if m["id"] == me_id:
                bits.append("← 这是你")
            line = "- " + "".join(bits)
            line += f" | 强项:{'、'.join(e['strengths']) or '通用'} | 模型:{e['model_display']}"
            if m["skills"]:
                line += f" | 技能:{'、'.join(m['skills'])}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------ system prompt
    def global_extras(self) -> str:
        items = [p for p in self.store.list_prompts() if p["use_globally"]]
        if not items:
            return ""
        return "【补充要求】\n" + "\n".join(f"- {p['content'].strip()}" for p in items)

    def system_prompt(
        self, group: dict, agent: dict, members: list[dict], *,
        memory_block: str = "", tools_block: str = "", extra: str = "",
    ) -> str:
        cfg = self.store.get_settings()
        vals = self.values(group, agent, members)
        sk_dir = self.store.data_dir / "skills"
        extras = self.global_extras()
        parts = [
            render_vars(cfg["system_prompt"], vals).strip(),
            render_vars(agent["prompt"], vals).strip(),
            render_vars(extras, vals) if extras else "",
            ("【本群提示词】\n" + render_vars(group["prompt"].strip(), vals)) if (group.get("prompt") or "").strip() else "",
            skills_prompt(sk_dir, group["ext"]["skills"], group=True),
            skills_prompt(sk_dir, agent["skills"]),
            "【群成员与分工】(强项 = 岗位所需 + 所用模型的擅长;分工时按强项来,不要抢别人的活)\n"
            + self.roster_text(members, agent["id"], group.get("host_agent_id")),
            memory_block,
            tools_block,
            extra,
        ]
        return "\n\n".join(p for p in parts if p)
