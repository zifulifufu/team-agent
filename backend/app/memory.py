"""记忆:把既往的偏好、决定、教训和「谁做过什么」存下来,下次发言时按相关性取回。

三个来源:
  1. 手动:你在「记忆」页添加(或让成员调用 memory_save 工具记下)。
  2. 自动提炼:一轮协作结束后,用一个便宜的模型从对话里提炼 0~3 条长期有用的偏好/决定/教训(可关闭)。
  3. 行为流水:每轮协作结束后程序自己记一条「任务 → 谁做了什么、用了什么工具、有没有回退」,不调用模型。
取回时按 BM25 相关性 + 置顶 + 新近度排序,只把最相关的几条放进提示词。
记忆只存在本机数据库里;自动提炼会过滤掉像密钥、长数字串这样的内容。
"""

from __future__ import annotations

import json
import re
import time

from .router import AllRoutesFailed, ModelRouter
from .store import Store
from .textindex import BM25, tokenize

KIND_LABEL = {"preference": "偏好", "fact": "事实", "decision": "决定", "lesson": "教训", "action": "过往做法"}
_SECRETish = re.compile(r"(sk-[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{12,}|\d{11,}|[A-Za-z0-9_\-]{32,}|password|密码|口令|secret|api[_ -]?key)", re.I)


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
        # 没有关键词命中时,偏好类记忆仍然值得带上(它们通常与具体话题无关)
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
        lines = [f"- [{KIND_LABEL.get(m['kind'], m['kind'])}] {m['content']}" for m in mems]
        return "【记忆】(来自以往协作,与当前任务无关的可以忽略,不要向用户复述)\n" + "\n".join(lines)

    # ---------------------------------------------------------------- record
    def record_action(self, group: dict, task_text: str, steps: list[dict], elapsed_s: float) -> dict | None:
        """steps: [{"agent":名字,"model":模型 id,"tools":[工具名],"fallback":bool,"ok":bool}]"""
        if not steps:
            return None
        parts = []
        for s in steps:
            bits = [s["agent"]]
            if s.get("model"):
                bits.append(f"({s['model'].split('/', 1)[-1]})")
            if s.get("tools"):
                bits.append("用了 " + "、".join(dict.fromkeys(s["tools"])))
            if s.get("fallback"):
                bits.append("发生过回退")
            if not s.get("ok", True):
                bits.append("失败")
            parts.append("".join(bits[:2]) + (" " + " ".join(bits[2:]) if len(bits) > 2 else ""))
        text = f"任务「{task_text.strip()[:60]}」→ " + ";".join(parts) + f";共 {elapsed_s:.0f} 秒"
        m = self.store.add_memory(text, "group", group["id"], "action", "auto")
        self.store.trim_memories("group", group["id"], 40, "action")
        return m

    def save_manual(self, content: str, scope: str, scope_id: str, kind: str = "fact", source: str = "manual") -> dict:
        return self.store.add_memory(content, scope, scope_id, kind, source)

    async def extract(self, group: dict, user_text: str, final_text: str) -> list[dict]:
        """用便宜的模型提炼长期有用的记忆。失败一律静默(记忆是加分项,不能影响主流程)。"""
        cfg = self.store.get_settings()
        if not (cfg["memory_enabled"] and cfg["memory_auto_extract"]):
            return []
        prompt = (
            "你是记忆整理员。下面是用户的一次请求和团队的最终答复。请只提炼「以后还会用到」的长期信息:"
            "用户的偏好和习惯、已经做出的决定、踩过的坑/教训、稳定的事实(项目名、受众、口径等)。\n"
            "严格规则:不要记临时性的内容(具体某天的安排、一次性的问题答案);不要记密钥、密码、证件号、银行卡号、"
            "手机号或其它个人隐私;每条一句话、不超过 60 字;最多 3 条;没有值得记的就输出空数组。\n"
            '只输出 JSON 数组,例:[{"scope":"global","kind":"preference","content":"周报喜欢先给结论再列数据"}]。'
            "scope 只能是 global(对所有群通用)或 group(只对本群),kind 只能是 preference/fact/decision/lesson。\n\n"
            f"【用户请求】\n{user_text[:1500]}\n\n【团队答复】\n{final_text[:2500]}"
        )
        try:
            res = await self.router.complete(
                [{"role": "user", "content": prompt}], tags=["速度", "低成本"], max_tokens=400, temperature=0
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
                continue  # 和已有记忆几乎重复
            scope = "group" if it.get("scope") == "group" else "global"
            kind = it.get("kind") if it.get("kind") in ("preference", "fact", "decision", "lesson") else "fact"
            saved.append(self.store.add_memory(content, scope, group["id"] if scope == "group" else "", kind, "auto"))
        return saved
