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
import xml.etree.ElementTree as ET
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
# What models *actually* type on this machine, read off the stored transcripts rather than assumed.
# Four shapes, each of which cost a real round its work before it was handled here:
#   ① `<tool_call>{json}</tool_call>` — the format the prompt asks for.
#   ② `<tool_call>{json} <tool_call>{json}` — the same but with **no closing tags at all**, which is
#      the commonest miss. The old pattern `.*?(?:</tool_call>|\Z)` had to run to the end of the
#      reply, so four separate calls were read as one piece of nonsense and **all four were lost**
#      (measured on a live round: the host opened four, two were dropped as "(malformed)").
#   ③ `<tool_calls>` — the **plural**, which a model reaches for when it wants its vendor's native
#      function calling. It was not recognised at all, so the markers were printed into the chat:
#      one stored message contained 120 of them, with the model's real answer lying between them.
#   ④ several JSON objects inside one block, which used to be glued together by the
#      outermost-braces shortcut and then fail to parse as a whole.
_DSML_PREFIX = r"[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*"
_MARKER = rf"(?:{_DSML_PREFIX}(?:calls|tool_calls?|function_calls?)|tool_calls?|function_calls?)"
_OPEN = re.compile(rf"<{_MARKER}>", re.I)
_CLOSE = re.compile(rf"</{_MARKER}>", re.I)
# ⚠️ `(?:\s[^>]*)?` and not `[^>]*`: the loose form also matched prose that merely *starts*
# like a tag — an independent review found it would delete `<tool_callback>` out of a sentence,
# and would delete from a bare `<tool_call` all the way to the next `>`. Whitespace before any
# extra content keeps the real shapes (`<tool_call name="x": ...>`) while leaving words alone.
_ANY_MARKER = re.compile(rf"</?{_MARKER}(?:\s[^>]*)?>", re.I)
_PLAN_BLOCK = re.compile(r"<plan>.*?(?:</plan>|\Z)", re.S | re.I)
# How many blocks one reply may contain before the scanner stops looking. See `_spans`.
_SCAN_LIMIT = 200

# ⚠️⚠️ ⑤ The dialect that is not a dialect at all: **the gateway's own token separator, leaked into
# the text**. minimax-m3 answers through a transport that wraps every token as `]<]minimax[>[`, and
# the wrapper arrives as ordinary characters — measured 2026-09-26 (group 介入术式科, member
# Storyboard):
#     {"name": "library_search">]<]minimax[>[<query>起伏参数表 起伏版 WAV narration]<]minimax[>[</query>]<]minimax[>[</invoke>
# Every other matcher in this file is defeated by it at once: the separator sits *between* the tool
# name and its arguments, so the JSON never closes, and `_ARGS_HINT` never sees a marker it knows.
# Both calls in that turn were discarded as "(malformed)" — the member was told twice that its
# arguments had the wrong shape, which was true and useless: what was wrong was the transport's
# noise, not its call.
#
# The repair is to remove the noise **before** anything else looks at the text, because that turns
# an unknown dialect back into one this file already handles. The tag name is a parameter because
# the same transport names itself differently (`minimax`, `abab`, …).
_TOKEN_SEP = re.compile(r"\]<\]\s*[A-Za-z0-9_.\-]{1,32}\s*\[>\[")


def _denoise(text: str) -> str:
    """Drop a gateway's own token separators. Not content, and not a tag — just transport noise."""
    return _TOKEN_SEP.sub("", text)


def _loads_lenient(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        # Models sometimes put literal newlines/tabs in a document body. These
        # preserve the exact string value; no keys, quotes or missing text are guessed.
        return json.loads(t, strict=False)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)  # with explanatory text mixed in around it, take the outermost braces
    if m:
        try:
            return json.loads(m.group(0), strict=False)
        except ValueError:
            return None
    return None


def _closed_arguments_call(body: str) -> dict | None:
    """Recover a closed argument object with only its outer call brace missing.

    Measured MiniMax response: {"name":"write_document","arguments":{...}
    Never finish strings, add values, repair quotes, or recover truncated content.
    """
    raw = body.strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        return None
    try:
        value = json.loads(raw + "}", strict=False)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("arguments"), dict):
        return None
    return value if set(value) <= {"name", "arguments"} and isinstance(value.get("name"), str) else None


def _json_objects(body: str) -> list[tuple[Any, int, int]]:
    """Every complete top-level JSON object in `body` as (value, start, end), in order.

    A hand-written scanner rather than one regex, because `arguments` is itself an object: the
    outermost-braces shortcut a regex can do either stops at the first `}` or glues two calls into
    one, and both of those were live failures (see ④ above). String literals are tracked so a brace
    inside a prompt — `"prompt": "一座{古镇}的石桥"` — cannot end the object early. The offsets are
    returned as well so a caller can tell how much of the body the object actually accounts for.
    """
    out: list[tuple[Any, int, int]] = []
    depth = 0
    start = -1
    quoted = escaped = False
    for i, ch in enumerate(body):
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                obj = _loads_lenient(body[start:i + 1])
                if isinstance(obj, dict):
                    out.append((obj, start, i + 1))
                start = -1
    return out


_FENCE_ONLY = re.compile(r"\s*(?:```[A-Za-z0-9_+-]*\s*)?")


def _leading_json_end(text: str, after: int) -> int | None:
    """End offset of the JSON object(s) at the very start of a block's body, or None.

    ⚠️ **This is what protects a call whose arguments quote the protocol itself.** The markers are
    found by scanning text, and a `write_document` body is text — so a member documenting this app
    (or an instruction quoting the format, which the app's own prompts do) writes
    `… "body": "工具格式是 <tool_call>{…}</tool_call> 这样" …` and the scan finds two markers *inside
    a JSON string*. Measured: that turned one perfectly good call into **two malformed ones** and
    leaked `这样"}}` into the chat.

    So a body that starts with JSON is read as JSON and nothing else: the object decides where the
    call ends, whatever the string contains. Only when there is no leading object does the marker
    scan below get a say — which is the case it exists for (a call whose JSON never arrived).
    """
    body = text[after:]
    objs = _json_objects(body)
    if not objs:
        return None
    if not _FENCE_ONLY.fullmatch(body[:objs[0][1]]):
        # Something was written before the object. That is *usually* prose, and prose must stay prose
        # — but a call that the model introduced with a sentence ("Here is the tool call:") is still
        # a call, and swallowing it as text loses the work silently. So the prefix is tolerated only
        # when what follows actually looks like a call: a JSON object with a `name`.
        lead = objs[0][0]
        if not (isinstance(lead, dict) and (lead.get("name") or lead.get("tool"))):
            return None
        # The block — marker, prose and all — is hidden, like every other block: the sentence was
        # written *inside* the tag, so it is part of the call, and printing half a block is how the
        # chat ends up with stray fragments of JSON around it.
        return after + objs[0][2]
    end = objs[0][2]
    for _o, start, stop in objs[1:]:
        if body[end:start].strip():
            break                        # real text between objects, so the run of calls ends here
        end = stop
    return after + end


def _spans(text: str) -> list[tuple[int, int, int]]:
    """(block start, body start, block stop) for every tool-call block, markers included.

    A block ends at whichever comes first: **the JSON it opened with**, its own closing tag, the next
    opening tag, or — when none of those ever comes — the marker alone.

    Each of those rules is load-bearing, and every one was a real round's failure:

    * the **JSON** one, because a body may contain the markers as text (see `_leading_json_end`);
    * the *next opening tag*, because a model that opens a call, closes nothing and opens the next
      (measured: four calls in one reply, not one closing tag among them) would otherwise have the
      first match run to the end of the reply, so every following call became part of the first
      one's body and all of them were lost together;
    * the *marker alone* fallback, because "no closing tag" does not mean "the rest of the reply is my
      arguments". A stored reply carried **120** bare `<tool_calls>` markers in front of a correct
      diagnosis; reading an unterminated marker as body-is-everything is exactly how that diagnosis
      disappears from the user's screen.
    """
    spans: list[tuple[int, int, int]] = []
    consumed = 0
    for m in _OPEN.finditer(text):
        if len(spans) >= _SCAN_LIMIT:
            # Every block re-searches the rest of the text for its terminator, so K markers over N
            # characters costs O(K×N). A model that degenerates into repeating the opening tag (one
            # stored message had 120 of them) makes that quadratic for no gain: past a couple of
            # hundred there is nothing left worth parsing. The rest of the text is simply left alone,
            # which for that input is what we want anyway — it is noise, not calls.
            break
        if m.start() < consumed:
            # ⚠️ A marker **inside the block just read** — which is what a quoted `<tool_call>` in a
            # JSON string looks like. `finditer` sees every marker in the text regardless of what the
            # previous block turned out to be, so without this line the inner one opened a second
            # block of its own and produced a second, malformed call.
            continue
        after = m.end()
        if "dsml" in m.group().lower() and re.match(rf"\s*<{_DSML_PREFIX}invoke\b", text[after:], re.I):
            close = re.search(re.escape("</" + m.group()[1:]), text[after:], re.I)
            stop = after + close.start() if close else len(text)
            spans.append((m.start(), after, stop))
            consumed = stop
            continue
        stop = _leading_json_end(text, after)
        if stop is None:
            nxt = _OPEN.search(text, after)
            close = _CLOSE.search(text, after)
            stops = [len(text)]
            if nxt:
                stops.append(nxt.start())
            if close:
                stops.append(close.start())
            stop = min(stops)
            if close and close.start() == stop:
                stop = close.start()        # the tag itself is swept from the text later
            elif stop == len(text) and nxt is None:
                # A truncated native XML call must stay hidden and request repair.
                stop = len(text) if "dsml" in m.group().lower() else after
        spans.append((m.start(), after, stop))
        consumed = stop
    return spans


def _visible_without(text: str, spans: list[tuple[int, int, int]]) -> str:
    """`text` with every span cut out (the markers and their bodies both)."""
    return _cut(text, [(start, stop) for start, _, stop in spans])


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    """`text` with every [start, stop) cut out. Spans may overlap; later ones win."""
    keep: list[str] = []
    at = 0
    for start, stop in sorted(spans):
        if stop <= at:
            continue
        if start > at:
            keep.append(text[at:start])
        at = stop
    keep.append(text[at:])
    return "".join(keep)


# ----------------------------------------------------------------- calls with no tags at all
# ⚠️⚠️ The most expensive failure this file has met. Some models drop the protocol tags and send the
# JSON bare. Measured (2026-09-25, group 未破裂颅内动脉瘤): the Storyboard member wrote
#
#     我先把分镜表从对话上下文里整段落地到工作目录,再开始生成动画。{"name": "write_document",
#     "arguments": {"path": "…/分镜表.md", "body": "# 未破裂…\n\n| 镜号 | 起止秒 |…"}}
#
# Nothing in this module matched it — every rule above is anchored on a marker token — so two things
# happened at once, and the second is the one that hurt:
#   * the call was **never run**. The file it believed it had written does not exist on disk, the
#     next task's input was missing, and the whole animation chain failed (its own next message
#     opens with 「上游分镜表读取失败」). `meta.tools` for that reply lists only the one call that
#     did parse;
#   * the JSON was **printed into the chat** as body text — markdown headings, table pipes and
#     literal `\n` escapes right in the bubble, which is what 「成员发表的内容没有格式,看起来很乱」
#     looks like from the user's side.
# So: an object that has the *shape* of a call counts as one even without its tags. The shape test is
# deliberately narrow (identifier-looking name + a dict under one of the three argument keys) and it
# skips fenced code, because prose that merely quotes JSON must stay prose.
_CALL_KEYS = ("name", "tool")
_ARG_KEYS = ("arguments", "args", "parameters")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _fenced(text: str) -> list[tuple[int, int]]:
    """Ranges covered by ``` fences — a member quoting JSON in a code block is not calling a tool."""
    out: list[tuple[int, int]] = []
    at = 0
    while True:
        i = text.find("```", at)
        if i < 0:
            return out
        j = text.find("```", i + 3)
        if j < 0:
            out.append((i, len(text)))
            return out
        out.append((i, j + 3))
        at = j + 3


def looks_like_call(obj: Any, known: set[str] | None = None) -> str | None:
    """The tool name if `obj` has the shape of a call, else None.

    `known` (the tool names this round actually has) makes it strict when it is available: a name
    that is not on the list is not a call we could run anyway, and leaving it in the text is
    better than silently deleting a sentence someone wrote. With no list we fall back to shape
    alone — which is what the display-side cleanup (`strip_hidden`) has to do.
    """
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name.strip()):
        return None
    name = name.strip()
    if known and name not in known:
        return None
    if not isinstance(next((obj[k] for k in _ARG_KEYS if k in obj), None), dict):
        return None
    return name


def _bare_complete(text: str, known: set[str] | None = None) -> list[tuple[int, int]]:
    """[start, stop) of every **complete** tag-less call in `text`, in order."""
    fences = _fenced(text)
    out: list[tuple[int, int]] = []
    for obj, start, stop in _json_objects(text):
        if any(a <= start < b for a, b in fences):
            continue
        if looks_like_call(obj, known):
            out.append((start, stop))
    return out


# ⚠️⚠️ The *other* half of the tag-less case, and the one that actually happened to this user: the
# model was cut off **inside** the call. The stored message (7156 characters) ends mid-string —
# `…params={sac_at_x: 0.34, …, show: [\"flow\",\"sac\"]}\n\n---\n\n## 三、动画调用清单（t4 用）…`
# — so the object never closes, `_json_objects` (which only reports balanced objects) sees nothing,
# and the whole thing renders as body text. A half-written object cannot be run, but it still has to
# leave the screen: everything after `"arguments": {` is inside a JSON string.
#
# ⚠️⚠️ And the shape that turned up the next day (2026-09-26, group 冗长扩张动): a JSON head followed
# by a **DSML argument tag** —
#     {"name": "library_read"> <parameter name="doc">交付/冗长扩张动脉瘤综述.md</parameter>…
# — the name is there, the arguments never come as JSON, so nothing can ever balance it. The old
# pattern (which insisted on `, "arguments": {` right after the name) did not match it either, and
# both halves of the failure came back together: the call did not run, and the text was printed into
# the bubble. So the test is now two parts — a name of identifier shape, and a **parameter marker of
# some dialect** within a short window after it. Prose that merely contains `{"name": …}` without any
# argument marker stays prose, and fenced code is skipped either way.
_NAMED_CALL = re.compile(
    r'\{\s*"(?:name|tool)"\s*:\s*"([A-Za-z_][A-Za-z0-9_]{0,63})"'        # {"name": "x"
    r'|\{\s*"?invoke"?\s+name\s*=\s*"([A-Za-z_][A-Za-z0-9_]{0,63})"')     # {"invoke name="x"> …
_ARGS_HINT = re.compile(
    r'"(?:arguments|args|parameters)"\s*:'
    r'|,\s*"(?:parameter|arg|argument)\b'          # …", "parameter name="path">  (a JSON quote, XML body)
    r'|>\s*<\s*[A-Za-z_][A-Za-z0-9_]{0,63}\s*>'   # {"name":"x"> <query>…</query>  (element = argument)
    r'|<\s*\|{0,2}\s*(?:DSML\s*)?\|{0,2}\s*(?:parameter|arg|arguments|invoke)\b'
    r'|<\s*(?:antml:)?(?:parameter|arg|arguments)\b', re.I)
# How far after the name an argument marker may appear and still count. Short on purpose: this is a
# scan of the whole reply, and a wide window starts matching prose that talks *about* the protocol.
_ARG_WINDOW = 400


def _balanced_end(text: str, start: int) -> int | None:
    """Offset just past the `}` that closes the `{` at `start`, or None if it never closes."""
    depth = 0
    quoted = escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, stop in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], stop))
        else:
            out.append((start, stop))
    return out


def _bare_spans(text: str, known: set[str] | None = None) -> list[tuple[int, int]]:
    """Everything that has to leave the screen because it is a tag-less call.

    Complete objects, and — cut short at the end of `text` — ones the model never finished writing
    (whether it ran out of output or switched dialects mid-call).
    """
    fences = _fenced(text)
    spans = _bare_complete(text, known)
    for m in _NAMED_CALL.finditer(text):
        start = m.start()
        if any(a <= start < b for a, b in fences):
            continue
        name = m.group(1) or m.group(2)
        if known and name not in known:
            continue
        if not _ARGS_HINT.search(text, m.end(), min(len(text), m.end() + _ARG_WINDOW)):
            continue
        end = _balanced_end(text, start)
        spans.append((start, len(text) if end is None else end))
    return _merged(spans)



def _call_from(obj: Any, raw: str) -> ToolCall:
    """One parsed JSON object as a call, or a failed call carrying the reason and what arrived."""
    if not isinstance(obj, dict) or not (obj.get("name") or obj.get("tool")):
        return ToolCall("", {}, i18n.pick_now("Wrong shape: expected {\"name\": ..., \"arguments\": {...}}", "格式不对:需要 {\"name\": ..., \"arguments\": {...}}"), raw.strip()[:200])
    args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
    if isinstance(args, str):
        args = _loads_lenient(args)
    if not isinstance(args, dict):
        return ToolCall(str(obj.get("name") or obj.get("tool")), {}, i18n.pick_now("arguments has to be a JSON object", "arguments 必须是 JSON 对象"), raw.strip()[:200])
    return ToolCall(str(obj.get("name") or obj.get("tool")), args, "", raw.strip()[:200])


def _outside(blocks: list[tuple[int, int]]):
    """Filter keeping only the spans that do not overlap any of `blocks`."""
    def keep(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [(s, e) for s, e in spans
                if not any(s < b_stop and b_start < e for b_start, b_stop in blocks)]
    return keep


def _dsml_calls(body: str) -> list[ToolCall] | None:
    """DeepSeek gateways can emit native XML calls despite the requested JSON protocol."""
    if not re.match(rf"\s*<{_DSML_PREFIX}invoke\b", body, re.I):
        return None
    try:
        xml = re.sub(rf"(</?){_DSML_PREFIX}", r"\1", body, flags=re.I)
        if "<!" in xml:
            raise ValueError("XML declarations are not tool parameters")
        root = ET.fromstring(f"<calls>{xml}</calls>")
        calls = []
        for invocation in root:
            name = invocation.attrib.get("name", "")
            if invocation.tag != "invoke" or not _NAME_RE.fullmatch(name):
                raise ValueError("invalid tool name")
            args = {}
            for param in invocation:
                key = param.attrib.get("name", "")
                if param.tag != "parameter" or not key or key in args or len(param):
                    raise ValueError("invalid parameter")
                value = param.text or ""
                args[key] = json.loads(value) if param.attrib.get("string", "true").lower() == "false" else value
            calls.append(ToolCall(name, args, raw=body))
        return calls
    except (ET.ParseError, ValueError):
        return [ToolCall("", {}, i18n.pick_now(
            "Malformed native tool call. Retry using <tool_call> with JSON name and arguments.",
            "原生工具调用格式不完整。请用 <tool_call> 包裹 JSON 的 name 和 arguments 重试。"), body[:200])]


_ELEMENT_HEAD = re.compile(
    r'\{\s*"(?:name|tool)"\s*:\s*"([A-Za-z_][A-Za-z0-9_]{0,63})"\s*>')
# …and the head with **no JSON at all**: `<invoke name="library_read"><doc>x.md</doc></invoke>`.
# ⚠️ This is the shape that cost group 介入术式科 its t5 on 2026-09-26: Storyboard emitted three of
# these, all three answered "(malformed) — wrong shape", and the task then failed for lack of the
# file it had asked for. `_dsml_calls` does not match it because it insists on the `||DSML||` prefix
# in front of `<invoke`, and the JSON head test above does not match it because there is no JSON.
_INVOKE_HEAD = re.compile(
    r'<\s*invoke\s+name\s*=\s*"([A-Za-z_][A-Za-z0-9_]{0,63})"\s*>', re.I)
# One `<invoke …>…</invoke>` block. ⚠️ Lazy inner, so four side-by-side invocations come out as four
# calls: the observed message had four, and one merged call carrying everybody's arguments would have
# been a worse answer than "wrong shape".
_INVOKE_BLOCK = re.compile(
    r'<\s*invoke\s+name\s*=\s*"([A-Za-z_][A-Za-z0-9_]{0,63})"\s*>(.*?)<\s*/\s*invoke\s*>',
    re.S | re.I)
_ELEMENT_PARAM = re.compile(
    r"<\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*((?:[^<>\"]|\"[^\"]*\")*)>"
    r"((?:(?!<\s*/).)*?)<\s*/\s*\1\s*>", re.S)
_ELEMENT_KEY_ATTR = re.compile(r'name\s*=\s*"([A-Za-z_][A-Za-z0-9_]{0,63})"')
_ARG_TAGS = {"parameter", "arg", "argument", "arguments"}


def _element_calls(body: str) -> list[ToolCall] | None:
    """`{"name": "library_search"> <query>…</query> </invoke>` — the element name *is* the argument.

    The third dialect in three days, and the only one where the argument tags carry no `name=`
    attribute: `<query>` means "the argument called query". `_dsml_calls` requires
    `<parameter name=` and the JSON never closes, so without this the call is unreadable — measured
    2026-09-26 (group 介入术式科, member Storyboard): two `library_search` calls, both discarded as
    "(malformed)", both with the same text echoed back at the member. It had asked for the narration
    timing table and got told its syntax was wrong twice.

    Two heads are accepted. The JSON one (`{"name": "x"> <query>…`) and the plain XML one
    (`<invoke name="x"><doc>…</doc></invoke>`, `_INVOKE_HEAD`) — the second cost the same group its
    **t5** in the same round, three calls at once, because `_dsml_calls` insists on the `||DSML||`
    prefix and the JSON head test insists on JSON. Each `<invoke>` is **its own call**: the member
    sent four side by side, and merging them would run one call with everybody's arguments.

    Deliberately conservative: a head of the right shape **and** at least one well-formed `<k>v</k>`
    element, and never a bare `{"name": …}` that merely looks like a call. `invoke` is skipped as an
    argument because it is the wrapper.
    """
    if _ELEMENT_HEAD.search(body):
        head = _ELEMENT_HEAD.search(body)
        args = _element_args(body[head.end():])
        return [ToolCall(head.group(1), args, raw=body)] if args else None
    calls = []
    for name, inner in _INVOKE_BLOCK.findall(body):
        args = _element_args(inner)
        if args:
            calls.append(ToolCall(name, args, raw=body))
    return calls or None


def _element_args(inner: str) -> dict[str, str]:
    """`<k>v</k>` pairs (and `<parameter name="k">v</parameter>`) → the arguments they spell."""
    args: dict[str, str] = {}
    for element, attrs, value in _ELEMENT_PARAM.findall(inner):
        low = element.lower()
        if low in ("invoke", "tool_call", "tool_calls"):
            continue
        key = element
        if low in _ARG_TAGS:
            found = _ELEMENT_KEY_ATTR.search(attrs or "")
            if not found:
                continue
            key = found.group(1)
        if key in args:
            continue
        args[key] = value.strip()
    return args


def parse_tool_calls(text: str, max_calls: int = 3, known: set[str] | None = None) -> tuple[str, list[ToolCall]]:
    """Returns (the visible text with tool call blocks removed, the list of calls).

    `known` is the set of tool names this round may actually call, when the caller has it. Pass it:
    it is what keeps a tag-less call (`_bare_spans`) from being recognised by shape alone, and a
    call made in the model's own dialect is still a call that has to run.
    """
    found: list[tuple[int, ToolCall]] = []
    text = _denoise(text)
    spans = _spans(text)
    for _, body_start, stop in spans:
        # ⚠️ No stripping of closing tags inside the body any more. It looked harmless — "the tag is
        # not content" — but a JSON string may *quote* the tag, and deleting it corrupts the
        # arguments (measured: a `write_document` body lost the `</tool_call>` it was documenting).
        # Stray tags outside a block are removed by the sweep at the end of this function.
        body = text[body_start:stop]
        native = _dsml_calls(body)
        if native is not None:
            found.extend((body_start, call) for call in native)
            continue
        elements = _element_calls(body)
        if elements is not None:
            found.extend((body_start, call) for call in elements)
            continue
        objs = _json_objects(body)
        if objs:
            for obj, _, _ in objs:
                found.append((body_start, _call_from(obj, body)))
            continue
        if not body.strip():
            # A bare marker carries nothing. Worth stating, because it is not hypothetical: one
            # stored message was 120 repetitions of `<tool_calls>` with no JSON anywhere. Reporting
            # each one as a failed call would have fed the model 120 error messages and burned the
            # round's tool allowance on noise.
            continue
        recovered = _closed_arguments_call(body)
        if recovered and looks_like_call(recovered, known):
            found.append((body_start, _call_from(recovered, body)))
            continue
        found.append((body_start, ToolCall("", {}, i18n.pick_now("Wrong shape: expected {\"name\": ..., \"arguments\": {...}}", "格式不对:需要 {\"name\": ..., \"arguments\": {...}}"), body.strip()[:200])))
    # Calls sent with no tags at all. ⚠️ A bare object that sits **inside** a tagged block was
    # already read there (it is that block's body), so only the ones outside every block count.
    tagged = [(start, stop) for start, _, stop in spans]
    outside = _outside(tagged)
    for start, stop in outside(_bare_complete(text, known)):
        found.append((start, _call_from(_loads_lenient(text[start:stop]), text[start:stop])))
    # Tag-less, in a dialect the JSON branch cannot read. Its span is recorded so the visible text
    # loses it — these *are* runnable, which is the whole test for what may leave the screen.
    recovered_bare: list[tuple[int, int]] = []
    if known:
        for start, stop in outside(_bare_spans(text, known)):
            piece = text[start:stop]
            elements = _element_calls(piece)
            if elements is not None and all(c.name in known for c in elements):
                found.extend((start, c) for c in elements)
                recovered_bare.append((start, stop))
                continue
            recovered = _closed_arguments_call(piece)
            if recovered and looks_like_call(recovered, known):
                found.append((start, _call_from(recovered, piece)))
                recovered_bare.append((start, stop))
    # ⚠️ `_bare_complete` and **not** `_bare_spans` for the visible half. The two answer different
    # questions, and the router depends on the difference: `parse_tool_calls` may only remove what it
    # could actually **run** (a balanced object with a name and arguments, or a dialect this file can
    # read), so what stays behind is prose plus the protocol-shaped text that no reader here could
    # turn into a call — which is exactly what the router's "the model emitted private XML" check
    # looks for. `strip_hidden` is the stricter one (it also removes shapes nobody can run) and is
    # what the display side and the store use.
    #
    # ⚠️⚠️ 2026-09-26: this balance moved, and the direction matters. Mixed dialects used to be
    # display-only — hidden from the bubble and **still not executed**, the worst of both worlds.
    # `_element_calls` now runs them, so a runnable dialect is no longer evidence that the reply is
    # broken and the router correctly stops seeing it. What keeps the router's signal alive is that
    # a shape with **no readable arguments at all** (`<minimax:tool_call><invoke name="x">…`) is
    # still nobody's call. Tested from both sides:
    # test_a_call_written_in_two_dialects_at_once_now_runs and
    # test_private_tool_markup_triggers_fallback_instead_of_fake_success. Widening `_cut` here, or
    # dropping `recovered_bare` from it, breaks one of the two.
    visible = _cut(text, tagged + outside(_bare_complete(text, known)) + recovered_bare)
    return _ANY_MARKER.sub("", visible).strip(), [c for _, c in sorted(found, key=lambda x: x[0])][:max_calls]


def parse_failure(call: ToolCall) -> str:
    """What the member is told when its own call could not be parsed.

    The reason alone is not enough, and this machine has the evidence: one round was recorded with
    **seven** parse failures from a single member, emitting the same malformed call with the same
    arguments every time. `error` says what was expected; quoting `raw` says what actually arrived.
    Without the second half the member can only guess which part of its own text was wrong — which,
    from the outside, is exactly what "same arguments, same failure" looks like. The round cost its
    whole output: no images, no narration, no film.

    Kept as a function rather than inlined where the round builds its feedback, so what the member
    receives can be read and tested without running a round.
    """
    shown = " ".join((call.raw or "").split())
    if len(shown) > 300:
        shown = shown[:300] + "…"
    if not shown:
        return call.error
    return call.error + i18n.pick_now(f"\nWhat you sent: {shown}", f"\n你发出去的是: {shown}")


def format_result(name: str, ok: bool, text: str, limit: int = 6000) -> str:
    body = text if len(text) <= limit else text[:limit] + i18n.pick_now(f"\\n… (truncated; {len(text)} characters in total)", f"\n…(已截断,共 {len(text)} 字)")
    return f'<tool_result name="{name}" ok="{str(ok).lower()}">\n{body}\n</tool_result>'


def strip_hidden(text: str, known: set[str] | None = None) -> str:
    """Remove <plan> blocks, every tool-call marker still in the text, and tag-less calls.

    Two passes, because they fail differently. A `<plan>` with no closing tag is dropped **with
    everything after it** — half a JSON plan is worse than none, and the plan is the last thing a
    host writes. A tool-call block is cut only up to its own end (`_spans`), because a bare marker
    in the middle of a sentence must not cost the sentence: the stored transcript that put 120
    `<tool_calls>` markers *in front of* a perfectly good diagnosis is why the distinction is here
    rather than one shared regex.

    The third pass (`_bare_spans`) is for the model that sends the call with **no tags at all**.
    This is the function the display side leans on — the messages already in the database were
    stored before the parser learned that shape, and re-running this on the way out is what makes
    those old bubbles readable without rewriting anyone's history.
    """
    out = _denoise(text)
    out = _PLAN_BLOCK.sub("", out)
    out = _visible_without(out, _spans(out))
    out = _ANY_MARKER.sub("", out)
    return _cut(out, _bare_spans(out, known)).strip()


# ----------------------------------------------------------------- streaming filter
# Which tags hide their body while streaming. `tool_calls` and the `function_call*` spellings are
# here for the same reason the parser learned them: they turn up, and an unrecognised marker is not
# neutral — it is printed into the chat bubble.
_DSML_TAGS = tuple(f"{bar}dsml{bar} {name}" for bar in ("||", "｜｜", "|", "｜")
                   for name in ("calls", "tool_call", "tool_calls", "function_call", "function_calls"))
HIDDEN_TAGS = ("tool_call", "tool_calls", "function_call", "function_calls", "plan", *_DSML_TAGS)
# A tag whose body is dropped when the stream ends inside it. Half a JSON **plan** is worse than
# nothing (it is the last thing the host writes, and a truncated one reads as noise), but a tool
# call that never closed must **not** take the answer with it: one stored reply was 120 bare
# `<tool_calls>` markers followed by a correct diagnosis, and swallowing on flush is how that
# diagnosis would have been lost from the live view.
_DROP_TAIL = {"plan", *_DSML_TAGS}


class TagFilter:
    """Hide <tool_call>...</tool_call> / <plan>...</plan> while streaming.

    A tag can be split across two chunks, so a tail that "could be the start of a tag" is
    held back and judged once the next chunk arrives.

    A block also ends where the **next opening tag** starts, not only at its own closing tag. That
    case is not exotic — it is what a model produces when it emits four calls and closes none of
    them — and without the rule the first block runs to the end of the reply and every following
    call is printed as if it were body text."""

    def __init__(self, tags: tuple[str, ...] = HIDDEN_TAGS):
        self.tags = tags
        self.buf = ""
        self.inside: str | None = None
        self.held = ""       # body of an unterminated *releasable* block, kept for `flush`

    def _opening(self) -> tuple[int, str]:
        """The earliest opening marker in the buffer, as (index, tag).

        ⚠️ Searched in a **lower-cased copy of the same length**, because the parser matches tags
        case-insensitively (`re.I`) while this filter used to match exactly. `<TOOL_CALL>` was
        therefore parsed correctly and printed into the chat at the same time — the two halves have to
        agree on what a tag is, or the filter is worse than no filter.
        """
        low = self.buf.lower()
        hits = [(low.find(f"<{t}>"), t) for t in self.tags]
        hits = [(i, t) for i, t in hits if i >= 0]
        return min(hits) if hits else (-1, "")

    def _closing(self) -> tuple[int, int]:
        """The earliest *stray* closing marker with no opening of its own, as (start, end)."""
        low = self.buf.lower()
        best = (-1, -1)
        for t in self.tags:
            i = low.find(f"</{t}>")
            if i >= 0 and (best[0] < 0 or i < best[0]):
                best = (i, i + len(f"</{t}>"))
        return best

    def _hold(self) -> int:
        """How many trailing characters to keep back: they could be half of some marker."""
        keep = 0
        for t in self.tags:
            for op in (f"<{t}>", f"</{t}>"):
                for k in range(min(len(op) - 1, len(self.buf)), 0, -1):
                    if self.buf.lower().endswith(op[:k]):
                        keep = max(keep, k)
                        break
        return keep

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        out: list[str] = []
        while True:
            if self.inside:
                close = self.buf.lower().find(f"</{self.inside}>")
                nxt, _tag = self._opening()
                # ⚠️ A tool tag opening inside a plan does **not** end the plan. Measured leak from an
                # independent review: `<plan>隐藏A<tool_call>{}</tool_call>隐藏B</plan>` ended the plan at
                # the inner `<tool_call>`, so `隐藏B` — which the model had put inside the plan — was
                # streamed into the chat. The "next opening ends the block" rule exists for a model
                # that opens tool calls without closing them; a plan is a different thing, and the
                # only thing that ends one is its own closing tag.
                if self.inside in _DROP_TAIL:
                    nxt = -1
                if nxt >= 0 and (close < 0 or nxt < close):
                    # A second opening ends this block, and the body collected so far was a **call**
                    # — not something to show — so it is dropped rather than held.
                    self.buf = self.buf[nxt:]
                    self.held, self.inside = "", None
                    continue
                if close >= 0:
                    self.buf = self.buf[close + len(self.inside) + 3:]
                    self.held, self.inside = "", None
                    continue
                keep = self._hold()   # a half closing tag ends the block, a half opening starts one
                body = self.buf[: len(self.buf) - keep]
                if self.inside in _DROP_TAIL:
                    self.held = ""    # half a plan is worse than none of it
                else:
                    self.held += body
                self.buf = self.buf[len(self.buf) - keep:]
                break
            i, t = self._opening()
            s, e = self._closing()
            if s >= 0 and (i < 0 or s < i):
                out.append(self.buf[:s])
                self.buf = self.buf[e:]
                continue
            if i >= 0:
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(t) + 2:]
                self.inside = t
                continue
            keep = self._hold()  # hold back a tail that is a prefix of some marker
            out.append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest = self.held + self.buf
        if self.inside in _DSML_TAGS:
            self.buf, self.held, self.inside = "", "", None
            return ""
        # An unterminated block that was holding **a call's JSON** keeps holding it: JSON is not
        # something to show a user, and the call itself is handled by the parser. What must be
        # released is prose — which is the whole reason `held` exists (a bare marker in front of a
        # correct answer must not take the answer with it).
        # ⚠️ `_bare_spans` and not just "the object at the front": a tag-less call can sit at the end
        # of a paragraph (that is exactly where the 2026-09-25 one was), and the old front-only test
        # left it on screen.
        self.buf, self.held, self.inside = "", "", None
        return _ANY_MARKER.sub("", _cut(rest, _bare_spans(rest)))
