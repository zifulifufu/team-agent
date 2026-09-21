"""Tool calls over a text protocol.

Rather than depending on each vendor's function calling (support varies wildly across
Chinese and international models and across small local models), the prompt fixes one
format that any model able to follow a format can use:

    <tool_call>{"name": "library_search", "arguments": {"query": "quote"}}</tool_call>

The program parses the call -> runs it -> hands the result back to the model as
<tool_result> -> generation continues, until the model stops calling or the rounds run out.
While streaming, tags meant "for the program" such as <tool_call> and <plan> are filtered
out and never show up in the chat bubble.
"""

from __future__ import annotations

from . import i18n

import json
import re
from dataclasses import dataclass, field
from typing import Any

HIDDEN_TAGS = ("tool_call", "plan")


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    error: str = ""      # reason for a parse failure (the model receives it and gets a chance to correct itself)
    raw: str = ""


# ----------------------------------------------------------------- prompts
def _param_line(name: str, spec: dict, required: bool) -> str:
    typ = spec.get("type", "any")
    if isinstance(typ, list):
        typ = "|".join(str(t) for t in typ)
    desc = (spec.get("description") or "").replace("\n", " ")[:80]
    enum = spec.get("enum")
    extra = i18n.pick_now(f", one of {enum}", f",可选值 {enum}") if enum else ""
    return i18n.pick_now(f"{name}: {typ}{'' if required else ' (optional)'}{extra}", f"{name}: {typ}{'' if required else '(可选)'}{extra}") + (f" — {desc}" if desc else "")


def tool_signature(t: dict) -> str:
    params = (t.get("parameters") or {}).get("properties") or {}
    req = set((t.get("parameters") or {}).get("required") or [])
    args = "; ".join(_param_line(n, s if isinstance(s, dict) else {}, n in req) for n, s in list(params.items())[:8])
    desc = (t.get("description") or "").replace("\n", " ").strip()[:160]
    return f"- {t['name']}({args}): {desc}" if args else f"- {t['name']}(): {desc}"


def tools_prompt(tools: list[dict], max_calls: int = 3, limit: int = 40) -> str:
    if not tools:
        return ""
    shown = tools[:limit]
    lines = "\n".join(tool_signature(t) for t in shown)
    more = i18n.pick_now(f"\\n({len(tools) - limit} more tools are not listed)", f"\n(还有 {len(tools) - limit} 个工具未列出)") if len(tools) > limit else ""
    return (
        i18n.pick_now((
            "[Available tools]\\n"
            "When you need to look something up, do a calculation, or act on an external system, call a tool instead of guessing. The call format (strict JSON, inside the tag):\\n"
            "<tool_call>{\"name\": \"tool name\", \"arguments\": {\"argument name\": \"value\"}}</tool_call>\n"
            f"Rules: at most {max_calls} tool calls per reply; stop as soon as you call one and wait for the system to return <tool_result> before continuing;"
            "do not invent tool results; if a tool fails, retry with different arguments or say so honestly; if no tool is needed, just answer.\\n"
            f"Tools:\\n{lines}{more}"
        ), (
            "【可用工具】\n"
            "需要查资料、算东西或操作外部系统时,先调用工具,别凭空猜。调用格式(严格 JSON,放在标签里):\n"
            '<tool_call>{"name": "工具名", "arguments": {"参数名": "值"}}</tool_call>\n'
            f"规则:一次回复最多调用 {max_calls} 个工具;调用之后立刻停止输出,等系统返回 <tool_result> 再继续;"
            "不要编造工具结果;工具报错时换个参数重试或如实告知;不需要工具时直接回答。\n"
            f"工具列表:\n{lines}{more}"
        ))
    )


# ----------------------------------------------------------------- parsing
_BLOCK = re.compile(r"<(tool_call)>(.*?)(?:</\1>|\Z)", re.S)


def _loads_lenient(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)  # with explanatory text mixed in around it, take the outermost braces
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def parse_tool_calls(text: str, max_calls: int = 3) -> tuple[str, list[ToolCall]]:
    """Returns (the visible text with tool call blocks removed, the list of calls)."""
    calls: list[ToolCall] = []
    for m in _BLOCK.finditer(text):
        raw = m.group(2)
        obj = _loads_lenient(raw)
        if not isinstance(obj, dict) or not (obj.get("name") or obj.get("tool")):
            calls.append(ToolCall("", {}, i18n.pick_now("Wrong shape: expected {\"name\": ..., \"arguments\": {...}}", "格式不对:需要 {\"name\": ..., \"arguments\": {...}}"), raw.strip()[:200]))
            continue
        args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
        if isinstance(args, str):
            args = _loads_lenient(args) or {}
        if not isinstance(args, dict):
            calls.append(ToolCall(str(obj.get("name") or obj.get("tool")), {}, i18n.pick_now("arguments has to be a JSON object", "arguments 必须是 JSON 对象"), raw.strip()[:200]))
            continue
        calls.append(ToolCall(str(obj.get("name") or obj.get("tool")), args, "", raw.strip()[:200]))
    visible = _BLOCK.sub("", text).strip()
    return visible, calls[:max_calls]


def format_result(name: str, ok: bool, text: str, limit: int = 6000) -> str:
    body = text if len(text) <= limit else text[:limit] + i18n.pick_now(f"\\n… (truncated; {len(text)} characters in total)", f"\n…(已截断,共 {len(text)} 字)")
    return f'<tool_result name="{name}" ok="{str(ok).lower()}">\n{body}\n</tool_result>'


def strip_hidden(text: str) -> str:
    """Remove <plan> and <tool_call> blocks (including unfinished ones)."""
    return re.sub(r"<(tool_call|plan)>.*?(?:</\1>|\Z)", "", text, flags=re.S).strip()


# ----------------------------------------------------------------- streaming filter
class TagFilter:
    """Hide <tool_call>...</tool_call> / <plan>...</plan> while streaming.

    A tag can be split across two chunks, so a tail that "could be the start of a tag" is
    held back and judged once the next chunk arrives."""

    def __init__(self, tags: tuple[str, ...] = HIDDEN_TAGS):
        self.tags = tags
        self.buf = ""
        self.inside: str | None = None

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        out: list[str] = []
        while True:
            if self.inside:
                close = f"</{self.inside}>"
                i = self.buf.find(close)
                if i >= 0:
                    self.buf = self.buf[i + len(close):]
                    self.inside = None
                    continue
                self.buf = self.buf[-(len(close) - 1):]  # keep only a tail that could be half of a closing tag
                break
            hits = [(self.buf.find(f"<{t}>"), t) for t in self.tags]
            hits = [(i, t) for i, t in hits if i >= 0]
            if hits:
                i, t = min(hits)
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(t) + 2:]
                self.inside = t
                continue
            keep = 0  # hold back a tail that is a prefix of some opening tag
            for t in self.tags:
                op = f"<{t}>"
                for k in range(min(len(op) - 1, len(self.buf)), 0, -1):
                    if self.buf.endswith(op[:k]):
                        keep = max(keep, k)
                        break
            out.append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf, self.inside = "", None
        return rest
