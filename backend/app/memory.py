"""Memory: keeps past preferences, decisions, lessons and "who did what" so the most
relevant ones can be retrieved before the next reply.

Three sources:
  1. Manual: you add them on the "Memory" page (or have a member call the memory_save tool).
  2. Auto-extraction: after a collaboration round a cheap model distills 0-3 long-lived
     preferences/decisions/lessons from the conversation (can be disabled).
  3. Activity log: after each round the program itself records "task -> who did what,
     which tools were used, whether it fell back", without calling a model.
Retrieval ranks by BM25 relevance + pinned + recency, and only the few most relevant
entries go into the prompt. Memory lives only in the local database; auto-extraction
filters out things like keys and long digit strings.
"""

from __future__ import annotations

from . import i18n

import json
import re
import time

from .router import AllRoutesFailed, ModelRouter
from .store import Store
from .textindex import BM25, tokenize

# id -> (English label, Chinese label). Pairs rather than a `pick_now` call: a module-level
# call would be evaluated once at import and freeze whichever language was current then.
KIND_LABEL = {
    "preference": ("Preference", "偏好"),
    "fact": ("Fact", "事实"),
    "decision": ("Decision", "决定"),
    "lesson": ("Lesson", "教训"),
    "action": ("Past action", "过往做法"),
}


def kind_label(kind: str) -> str:
    """The memory kind as it should read in the request language."""
    pair = KIND_LABEL.get(kind)
    return i18n.pick_now(*pair) if pair else kind
_SECRETish = re.compile(r"(sk-[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{12,}|\d{11,}|[A-Za-z0-9_\-]{32,}|password|密码|口令|secret|api[_ -]?key)", re.I)  # i18n-keep: must still match 密码/口令 to catch secrets


def looks_sensitive(text: str) -> bool:
    return bool(_SECRETish.search(text))


class MemoryService:
    def __init__(self, store: Store, router: ModelRouter):
        self.store, self.router = store, router

    # ---------------------------------------------------------------- recall
    def recall(self, group_id: str, agent_id: str, query: str, k: int | None = None) -> list[dict]:
        cfg = self.store.get_settings()
        k = k if k is not None else int(cfg["memory_top_k"])
        if k <= 0:
            return []
        pool = self.store.memories_for(group_id, agent_id)
        if not pool:
            return []
        pinned = [m for m in pool if m["pinned"]][: max(1, k // 2)]
        rest = [m for m in pool if m not in pinned]
        picked = list(pinned)
        q = tokenize(query)
        if rest and q:
            bm = BM25([tokenize(m["content"]) for m in rest])
            now = time.time()
            scored = []
            for i, s in bm.scores(q).items():
                m = rest[i]
                age_days = (now - m["updated_at"]) / 86400
                boost = 1.0 + 0.3 / (1 + age_days / 14) + (0.2 if m["kind"] in ("preference", "lesson") else 0)
                scored.append((s * boost, m))
            scored.sort(key=lambda x: -x[0])
            picked += [m for _, m in scored[: k - len(picked)]]
        # with no keyword hit, preference memories are still worth including
# (they are usually independent of the specific topic)
        if len(picked) < k:
            extra = [m for m in rest if m["kind"] == "preference" and m not in picked]
            extra.sort(key=lambda m: -m["updated_at"])
            picked += extra[: k - len(picked)]
        self.store.touch_memories([m["id"] for m in picked])
        return picked

    @staticmethod
    def block(mems: list[dict]) -> str:
        if not mems:
            return ""
        lines = [f"- [{kind_label(m['kind'])}] {m['content']}" for m in mems]
        head = i18n.pick_now(
            "[Memory] (from earlier collaboration; ignore anything unrelated to the current "
            "task, and do not repeat it back to the user)\n",
            "【记忆】(来自以往协作,与当前任务无关的可以忽略,不要向用户复述)\n",
        )
        return head + "\n".join(lines)

    # ---------------------------------------------------------------- record
    def record_action(self, group: dict, task_text: str, steps: list[dict], elapsed_s: float) -> dict | None:
        """steps: [{"agent": name, "model": model id, "tools": [tool names], "fallback": bool, "ok": bool}]"""
        if not steps:
            return None
        parts = []
        for s in steps:
            bits = [s["agent"]]
            if s.get("model"):
                bits.append(f"({s['model'].split('/', 1)[-1]})")
            if s.get("tools"):
                bits.append(i18n.pick_now(
                    "used " + ", ".join(dict.fromkeys(s["tools"])),
                    "用了 " + "、".join(dict.fromkeys(s["tools"])),
                ))
            if s.get("fallback"):
                bits.append(i18n.pick_now("fell back to another model", "发生过回退"))
            if not s.get("ok", True):
                bits.append(i18n.pick_now("failed", "失败"))
            parts.append("".join(bits[:2]) + (" " + " ".join(bits[2:]) if len(bits) > 2 else ""))
        text = (
            i18n.pick_now(f'Task "{task_text.strip()[:60]}" -> ', f"任务「{task_text.strip()[:60]}」→ ")
            + ";".join(parts)
            + i18n.pick_now(f"; {elapsed_s:.0f}s total", f";共 {elapsed_s:.0f} 秒")
        )
        m = self.store.add_memory(text, "group", group["id"], "action", "auto")
        self.store.trim_memories("group", group["id"], 40, "action")
        return m

    def save_manual(self, content: str, scope: str, scope_id: str, kind: str = "fact", source: str = "manual") -> dict:
        return self.store.add_memory(content, scope, scope_id, kind, source)

    async def extract(self, group: dict, user_text: str, final_text: str) -> list[dict]:
        """Distill long-lived memories using a cheap model. Every failure is silent (memory is a
bonus and must not affect the main flow)."""
        cfg = self.store.get_settings()
        if not (cfg["memory_enabled"] and cfg["memory_auto_extract"]):
            return []
        prompt = i18n.pick_now(
            # English side: stays a plain string because of the `{` in the JSON example.
            (
                "You are a memory editor. Below are one user request and the team's final answer. "
                "Extract only the long-term information that will still be useful later: the user's "
                "preferences and habits, decisions already made, pitfalls and lessons, and stable "
                "facts (project name, audience, house style, and so on).\n"
                "Strict rules: do not record anything temporary (arrangements for one particular day, "
                "a one-off answer); do not record keys, passwords, ID numbers, bank card numbers, "
                "phone numbers or any other personal data; one sentence each, at most 60 characters; "
                "at most 3 items; if nothing is worth keeping, output an empty array.\n"
                'Output only a JSON array, for example: '
                '[{"scope":"global","kind":"preference","content":"prefers the conclusion first, then the data"}]. '
                "scope may only be global (applies to every group) or group (this group only); "
                "kind may only be preference/fact/decision/lesson.\n\n"
                "[User request]\n"
            )
            + f"{user_text[:1500]}\n\n[Team answer]\n{final_text[:2500]}",
            # Chinese side: the original wording, with its own f-string pieces.
            (
                "你是记忆整理员。下面是用户的一次请求和团队的最终答复。请只提炼「以后还会用到」的长期信息:"
                "用户的偏好和习惯、已经做出的决定、踩过的坑/教训、稳定的事实(项目名、受众、口径等)。\n"
                "严格规则:不要记临时性的内容(具体某天的安排、一次性的问题答案);不要记密钥、密码、证件号、银行卡号、"
                "手机号或其它个人隐私;每条一句话、不超过 60 字;最多 3 条;没有值得记的就输出空数组。\n"
                '只输出 JSON 数组,例:[{"scope":"global","kind":"preference","content":"周报喜欢先给结论再列数据"}]。'
                "scope 只能是 global(对所有群通用)或 group(只对本群),kind 只能是 preference/fact/decision/lesson。\n\n"
            )
            + f"【用户请求】\n{user_text[:1500]}\n\n【团队答复】\n{final_text[:2500]}",
        )
        try:
            res = await self.router.complete(
                [{"role": "user", "content": prompt}], tags=["speed", "low-cost"], max_tokens=400, temperature=0
            )
        except (AllRoutesFailed, Exception):  # noqa: BLE001
            return []
        m = re.search(r"\[.*\]", res.text, re.S)
        if not m:
            return []
        try:
            items = json.loads(m.group(0))
        except ValueError:
            return []
        saved: list[dict] = []
        existing = [tokenize(x["content"]) for x in self.store.list_memories(limit=300)]
        for it in items[:3]:
            if not isinstance(it, dict):
                continue
            content = str(it.get("content", "")).strip()
            if not content or len(content) > 120 or looks_sensitive(content):
                continue
            toks = set(tokenize(content))
            if any(toks and len(toks & set(e)) / len(toks | set(e)) > 0.7 for e in existing if e):
                continue  # almost a duplicate of an existing memory
            scope = "group" if it.get("scope") == "group" else "global"
            kind = it.get("kind") if it.get("kind") in ("preference", "fact", "decision", "lesson") else "fact"
            saved.append(self.store.add_memory(content, scope, group["id"] if scope == "group" else "", kind, "auto"))
        return saved
