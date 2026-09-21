"""模板中心:程序自带的一手模板目录 + 一键应用。

为什么改成这样(与之前的「示例库」相比):
  * 以前要先 clone 第三方仓库、再把本机路径填进来做静态提取 —— 对人来说太绕,对涉密机器也不合适。
    现在模板随程序分发,**打开即可用**:不 clone、不填路径、不联网;
  * 内容全部是本程序自己的原创文本,不含任何第三方项目的文件或数据,所以随程序分发
    不产生第三方许可义务(见 LICENSE 与 THIRD_PARTY_NOTICES.md);
  * **单一数据源**:团队模板、成员岗位、技能、提示词、MCP 各自只有一个内置定义
    (presets.TEMPLATES / presets.AGENT_PRESETS / tools.EXAMPLE_SKILLS /
     presets.SEED_PROMPTS / gallery.MCP_TEMPLATES)。本模块只做归一化与应用,不再复制一份内容,
    避免「两处定义慢慢漂移」这种最难查的问题;
  * **扩展口**:数据目录下的 `templates/*.json` 可以放团队自备模板,按 schema 校验后合并进目录
    (见 `_load_custom`)。校验不通过的条目会被丢弃并在界面上写明原因,不静默忽略;
  * **安全边界**:自定义模板只允许纯文本类(team / agent / skill / prompt),**不接受 mcp** ——
    那等于让一个 JSON 文件决定本机要执行什么命令。MCP 只能从内置清单里加,且一律以停用状态导入。

本模块不联网、不读数据目录以外的路径、不执行任何代码;应用模板只会往本机数据库和
skills 目录里写文本(以及添加一个默认停用的 MCP 条目)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .presets import builtin_for, builtin_names
from . import i18n
from .presets import AGENT_PRESETS, SEED_AGENTS, SEED_PROMPTS, TEMPLATES
from .store import Store
from .templates import ensure_agent
from .tools import EXAMPLE_SKILLS, list_skills, write_skill

CATALOG_VERSION = "2.0.0"      # 目录内容版本(语义化):模板有增改时升这个号
SCHEMA_VERSION = 1            # 自定义模板文件格式版本
CUSTOM_DIRNAME = "templates"  # 数据目录下放自定义模板的子目录
CUSTOM_MAX_BYTES = 512 * 1024
CUSTOM_MAX_ITEMS = 200
PLACEHOLDER_DIR = "/path/to/allowed/dir"

# MCP 用法清单(唯一数据源;api_ext 的 /api/mcp/templates 也从这里取)。
# 这些只是「预填表单」:命令与参数都要你自己核对,导入后一律停用。
MCP_TEMPLATES: list[dict] = [
    {"name": "文件系统", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", PLACEHOLDER_DIR],
     "env_keys": [], "note": "让成员读写指定目录里的文件。最后一个参数换成你允许访问的目录。需要 Node.js。"},
    {"name": "网页抓取", "command": "uvx", "args": ["mcp-server-fetch"],
     "env_keys": [], "note": "抓取网页并转成文本,成员可以读链接内容。需要 uv(uvx)。"},
    {"name": "知识图谱记忆", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"],
     "env_keys": [], "note": "MCP 官方的记忆服务器(与本程序自带的「记忆」是两套东西)。需要 Node.js。"},
    {"name": "顺序思考", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
     "env_keys": [], "note": "帮助模型把复杂问题分步思考。需要 Node.js。"},
    {"name": "浏览器(Playwright)", "command": "npx", "args": ["@playwright/mcp@latest"],
     "env_keys": [],
     "note": "让成员驱动真实的浏览器:打开网页、点击、填写。属于执行类工具,默认每次调用前都会问你。"
             "首次使用可能要下载浏览器,具体参数以 Playwright MCP 项目的文档为准。需要 Node.js。"},
    {"name": "时间与时区", "command": "uvx", "args": ["mcp-server-time"], "env_keys": [],
     "note": "查询和换算各地时间。需要 uv(uvx)。"},
    {"name": "Git", "command": "uvx", "args": ["mcp-server-git", "--repository", "/path/to/repo"],
     "env_keys": [], "note": "读取指定 Git 仓库的历史和差异。把最后的路径换成你的仓库。需要 uv(uvx)。"},
]

CATEGORIES: list[dict] = [
    {"id": "team", "label": "团队", "hint": "一键建成群聊:成员、群主、群规则、依赖的技能一次装好。"},
    {"id": "agent", "label": "角色", "hint": "单个成员:可以单独创建,也可以直接拉进已有的群。"},
    {"id": "skill", "label": "技能", "hint": "写给模型看的纯文本方法与规范,不会执行任何代码。"},
    {"id": "prompt", "label": "提示词", "hint": "常用提示词:存进提示词库后,可挂给某个群或设为全局。"},
    {"id": "mcp", "label": "MCP", "hint": "外部工具的接入写法:导入后一律停用,核对命令并填好密钥再启用。"},
]
KINDS: tuple[str, ...] = tuple(c["id"] for c in CATEGORIES)
CUSTOM_KINDS: tuple[str, ...] = ("team", "agent", "skill", "prompt")   # 自定义模板不允许 mcp

_TEAM_ICONS: dict[str, str] = {
    "office": "🏢", "video": "🎬", "writing": "✍️", "brainstorm": "💡", "review": "🧐",
    "research": "📚", "code": "💻", "data": "📊", "translate": "🌐", "proposal": "📑",
    "report": "📄", "clinical": "🩺",
}
_ICONS = {"team": "🧩", "agent": "🤖", "skill": "🧠", "prompt": "💬", "mcp": "🔌"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GalleryError(Exception):
    """模板不存在、参数不对、或目标（群）不存在 —— 由 API 层转成 400。"""


# --------------------------------------------------------------------- helpers
def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _ver(s: Any) -> tuple[int, ...]:
    """版本号取数字比较:1.10.0 > 1.9.0(字符串比较会判反)。"""
    parts = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in parts[:4]) or (0,)


def _member_defs() -> dict[str, dict]:
    """按名字索引成员定义(内置种子成员 + 岗位预设)。"""
    return {**{a["name"]: a for a in SEED_AGENTS}, **{a["name"]: a for a in AGENT_PRESETS}}


# --------------------------------------------------------------------- 内置条目
def _team_rows() -> list[dict]:
    who = _member_defs()
    out = []
    for t in TEMPLATES:
        members = [{"name": n, "avatar": (who.get(n) or {}).get("avatar", "🤖"),
                    "role": (who.get(n) or {}).get("role", "")} for n in t.get("members", [])]
        out.append({
            "id": f"team:{t['id']}", "kind": "team", "name": t["name"],
            "summary": t.get("desc", ""), "icon": _TEAM_ICONS.get(t["id"], _ICONS["team"]),
            "tags": [f"{len(members)} 个角色"], "source": "builtin", "home": bool(t.get("home", True)),
            "preview": {"members": members, "host": t.get("host", ""), "skills": list(t.get("skills", [])),
                        "prompt": _clip(t.get("prompt", ""), 200)},
            "def": {"members": list(t.get("members", [])), "host": t.get("host", ""),
                    "skills": list(t.get("skills", [])), "prompt": t.get("prompt", ""), "name": t["name"]},
        })
    return out


def _agent_rows() -> list[dict]:
    out = []
    for p in AGENT_PRESETS:
        # `<field>_zh` is carried alongside the English base value so that
        # i18n.localize() can swap it in for the Chinese UI. Built-in names are
        # English on purpose — see presets.SEED_AGENTS.
        out.append({
            "id": f"agent:{p['key']}", "kind": "agent", "name": p["name"],
            "name_zh": p.get("name_zh", ""),
            "summary": p.get("role", ""), "summary_zh": p.get("role_zh", ""),
            "icon": p.get("avatar", _ICONS["agent"]),
            "tags": list(p.get("tags", [])), "source": "builtin",
            "preview": {"avatar": p.get("avatar", "🤖"), "role": p.get("role", ""),
                        "role_zh": p.get("role_zh", ""),
                        "tags": list(p.get("tags", [])), "prompt": _clip(p.get("prompt", ""), 200),
                        "prompt_zh": _clip(p.get("prompt_zh", ""), 200)},
            "def": {"name": p["name"], "name_zh": p.get("name_zh", ""),
                    "avatar": p.get("avatar", "🤖"), "role": p.get("role", ""),
                    "role_zh": p.get("role_zh", ""),
                    "prompt": p.get("prompt", ""), "prompt_zh": p.get("prompt_zh", ""),
                    "tags": list(p.get("tags", []))},
        })
    return out


def _skill_rows() -> list[dict]:
    out = []
    for name, ex in EXAMPLE_SKILLS.items():
        scope = ex.get("scope", "member")
        out.append({
            "id": f"skill:{name}", "kind": "skill", "name": name,
            "summary": ex.get("description", ""), "icon": _ICONS["skill"],
            "tags": ["群聊规则" if scope == "group" else "成员技能"], "source": "builtin",
            "preview": {"description": ex.get("description", ""), "scope": scope,
                        "body": _clip(ex.get("body", ""), 220)},
            "def": {"name": name, "description": ex.get("description", ""),
                    "body": ex.get("body", ""), "scope": scope},
        })
    return out


def _prompt_rows() -> list[dict]:
    out = []
    for p in SEED_PROMPTS:
        kind = p.get("kind", "general")
        out.append({
            "id": f"prompt:{p['title']}", "kind": "prompt", "name": p["title"],
            "summary": _clip(p.get("content", ""), 90), "icon": _ICONS["prompt"],
            "tags": ["群提示词" if kind == "group" else "通用"], "source": "builtin",
            "preview": {"kind": kind, "content": _clip(p.get("content", ""), 200)},
            "def": {"title": p["title"], "content": p.get("content", ""),
                    "kind": kind, "use_globally": bool(p.get("use_globally"))},
        })
    return out


def _mcp_rows() -> list[dict]:
    out = []
    for m in MCP_TEMPLATES:
        out.append({
            "id": f"mcp:{m['name']}", "kind": "mcp", "name": m["name"],
            "summary": m.get("note", ""), "icon": _ICONS["mcp"],
            "tags": ["导入后停用"], "source": "builtin",
            "preview": {"command": m.get("command", ""), "args": list(m.get("args", [])),
                        "env_keys": list(m.get("env_keys", [])), "note": m.get("note", "")},
            "def": {"name": m["name"], "command": m.get("command", ""),
                    "args": list(m.get("args", [])), "env_keys": list(m.get("env_keys", [])),
                    "note": m.get("note", "")},
        })
    return out


def _builtin_rows() -> list[dict]:
    return [*_team_rows(), *_agent_rows(), *_skill_rows(), *_prompt_rows(), *_mcp_rows()]


# --------------------------------------------------------------------- 自定义模板
@dataclass
class Custom:
    dir: str
    exists: bool = False
    files: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


_CUSTOM_CACHE: dict[str, tuple[tuple, Custom]] = {}


def _signature(files: list[Path]) -> tuple:
    out = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    return tuple(sorted(out))


def _require(kind: str, raw: dict) -> str | None:
    """按类别校验自定义模板的必填字段;返回错误原因(通过则 None)。"""
    if kind == "team":
        names = raw.get("members")
        if not isinstance(names, list) or not names or len(names) > 12:
            return "team 需要 1~12 个 members(成员名数组)"
        if any(not isinstance(n, str) or not n.strip() or len(n) > 60 for n in names):
            return "team 的 members 必须是 1~60 字的成员名"
        host = raw.get("host")
        if host is not None and (not isinstance(host, str) or not host.strip()):
            return "team 的 host 必须是成员名"
        skills = raw.get("skills", [])
        if not isinstance(skills, list) or len(skills) > 8 or any(not isinstance(x, str) for x in skills):
            return "team 的 skills 必须是不超过 8 个的技能名数组"
        if len(str(raw.get("prompt", ""))) > 4000:
            return "team 的 prompt 超过 4000 字"
    elif kind == "agent":
        if len(str(raw.get("role", ""))) > 60:
            return "agent 的 role 超过 60 字"
        if len(str(raw.get("prompt", ""))) > 4000:
            return "agent 的 prompt 超过 4000 字"
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 6:
            return "agent 的 tags 必须是不超过 6 个的数组"
    elif kind == "skill":
        body = raw.get("body")
        if not isinstance(body, str) or not body.strip():
            return "skill 需要非空的 body"
        if len(body) > 8000:
            return "skill 的 body 超过 8000 字"
        if raw.get("scope", "member") not in ("member", "group"):
            return "skill 的 scope 只能是 member 或 group"
        if len(str(raw.get("description", ""))) > 200:
            return "skill 的 description 超过 200 字"
    elif kind == "prompt":
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            return "prompt 需要非空的 content"
        if len(content) > 4000:
            return "prompt 的 content 超过 4000 字"
        # 注意:条目的 kind 已经用来表示「这是提示词」了,提示词自己的类型叫 prompt_kind
        if raw.get("prompt_kind", "general") not in ("general", "group"):
            return "prompt 的 prompt_kind 只能是 general(通用) 或 group(群提示词)"
    return None


def _custom_row(raw: Any, seen: set[str]) -> tuple[dict | None, str]:
    """校验并归一化一条自定义模板。返回 (条目, 错误原因)。"""
    if not isinstance(raw, dict):
        return None, "条目必须是对象"
    rid = raw.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return None, "id 必须是 1~64 位、以字母或数字开头的字母数字 . _ - 组合"
    kind = raw.get("kind")
    if kind == "mcp":
        return None, "自定义模板不支持 kind=mcp(命令类请到「MCP」页自己添加)"
    if kind not in CUSTOM_KINDS:
        return None, f"kind 只能是 {' / '.join(CUSTOM_KINDS)}"
    fid = f"{kind}:{rid}"
    if fid in seen:
        return None, f"id「{rid}」与目录里已有的条目不唯一"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        return None, "name 必须是 1~60 个字"
    req = raw.get("requires")
    if req and _ver(req) > _ver(CATALOG_VERSION):
        return None, f"需要程序目录版本 {req} 以上,当前是 {CATALOG_VERSION}"
    bad = _require(kind, raw)
    if bad:
        return None, bad
    seen.add(fid)
    summary = _clip(str(raw.get("summary") or raw.get("description") or ""), 200)
    icon = str(raw.get("icon") or _ICONS[kind])[:2] or _ICONS[kind]
    return {
        "id": fid, "kind": kind, "name": name.strip(), "summary": summary, "icon": icon,
        "tags": [t for t in (raw.get("tags") or []) if isinstance(t, str)][:4] or ["自定义"],
        "source": f"custom:{raw.get('_file', '')}".rstrip(":"),
        "preview": _custom_preview(kind, raw),
        "def": _custom_def(kind, raw, name.strip()),
    }, ""


def _custom_preview(kind: str, raw: dict) -> dict:
    """列表里只带「摘要级」内容,正文放详情接口 —— 目录再大也不会把响应撑爆。"""
    if kind == "team":
        return {"members": [{"name": n, "avatar": "🤖", "role": ""} for n in raw.get("members", [])],
                "host": raw.get("host", ""), "skills": list(raw.get("skills", [])),
                "prompt": _clip(str(raw.get("prompt", "")), 200)}
    if kind == "agent":
        return {"avatar": str(raw.get("avatar") or "🤖")[:2], "role": str(raw.get("role", "")),
                "tags": [t for t in (raw.get("tags") or []) if isinstance(t, str)],
                "prompt": _clip(str(raw.get("prompt", "")), 200)}
    if kind == "skill":
        return {"description": str(raw.get("description", "")), "scope": raw.get("scope", "member"),
                "body": _clip(str(raw.get("body", "")), 220)}
    return {"kind": raw.get("prompt_kind", "general"), "content": _clip(str(raw.get("content", "")), 200)}


def _custom_def(kind: str, raw: dict, name: str) -> dict:
    """安装时用的完整定义;只保留各类别真正需要的字段。"""
    if kind == "team":
        return {"name": name, "members": list(raw.get("members", [])), "host": raw.get("host", ""),
                "skills": list(raw.get("skills", [])), "prompt": str(raw.get("prompt", ""))}
    if kind == "agent":
        return {"name": name, "avatar": str(raw.get("avatar") or "🤖")[:2], "role": str(raw.get("role", "")),
                "prompt": str(raw.get("prompt", "")), "tags": list(raw.get("tags", []))}
    if kind == "skill":
        return {"name": name, "description": str(raw.get("description", "")),
                "body": str(raw.get("body", "")), "scope": raw.get("scope", "member")}
    return {"title": name, "content": str(raw.get("content", "")), "kind": raw.get("prompt_kind", "general"),
            "use_globally": bool(raw.get("use_globally"))}


def _parse_custom(d: Path, files: list[Path]) -> Custom:
    out = Custom(dir=str(d), exists=True)
    seen = {r["id"] for r in _builtin_rows()}
    for p in files:
        try:
            if p.stat().st_size > CUSTOM_MAX_BYTES:
                out.errors.append({"file": p.name, "reason": f"文件超过 {CUSTOM_MAX_BYTES // 1024} KB,已忽略"})
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.errors.append({"file": p.name, "reason": f"读取或解析失败:{e}"})
            continue
        if not isinstance(raw, dict):
            out.errors.append({"file": p.name, "reason": "顶层必须是对象"})
            continue
        sv = raw.get("schema_version", SCHEMA_VERSION)
        if not isinstance(sv, int) or sv > SCHEMA_VERSION:
            out.errors.append({"file": p.name, "reason": f"schema_version={sv} 不支持,本程序支持到 {SCHEMA_VERSION}"})
            continue
        items = raw.get("items")
        if not isinstance(items, list):
            out.errors.append({"file": p.name, "reason": "缺少 items 数组"})
            continue
        if len(items) > CUSTOM_MAX_ITEMS:
            out.errors.append({"file": p.name, "reason": f"条目超过 {CUSTOM_MAX_ITEMS} 条,已忽略整个文件"})
            continue
        accepted = 0
        for i, it in enumerate(items):
            row, reason = _custom_row({**(it if isinstance(it, dict) else {}), "_file": p.name}, seen)
            if row is None:
                out.errors.append({"file": p.name, "index": i,
                                   "id": (it or {}).get("id", "") if isinstance(it, dict) else "",
                                   "reason": reason})
                continue
            out.items.append(row)
            accepted += 1
        out.files.append({"name": p.name, "items": accepted, "version": str(raw.get("catalog_version") or ""),
                          "author": str(raw.get("author") or "")})
    return out


def _load_custom(store: Store) -> Custom:
    """读取数据目录里的自定义模板。按文件 mtime+大小缓存,改完文件无需重启。"""
    d = Path(store.data_dir) / CUSTOM_DIRNAME
    if not d.is_dir():
        return Custom(dir=str(d), exists=False)
    files = sorted(p for p in d.glob("*.json") if p.is_file())
    key = str(d)
    sig = _signature(files)
    hit = _CUSTOM_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    res = _parse_custom(d, files)
    _CUSTOM_CACHE[key] = (sig, res)
    return res


# --------------------------------------------------------------------- 目录
def _all_items(store: Store) -> list[dict]:
    rows = _builtin_rows() + _load_custom(store).items
    order = {k: i for i, k in enumerate(KINDS)}
    rows.sort(key=lambda r: (order.get(r["kind"], 9), r["name"]))
    return rows


def _states(store: Store) -> dict:
    """一次性把各表的现状取出来,用于标注「已装/未装」。"""
    return {
        "groups": [g["name"] for g in store.list_groups()],
        "agents": {n for a in store.list_agents() for n in (builtin_names(builtin_for(a["name"])) or [a["name"]])},
        "skills": {s.name for s in list_skills(store.data_dir / "skills")},
        "prompts": {p["title"] for p in store.list_prompts()},
        "mcp": {m["name"] for m in store.list_mcp()},
    }


def _state(item: dict, st: dict) -> tuple[bool, str]:
    kind, name = item["kind"], item["name"]
    if kind == "team":
        n = sum(1 for g in st["groups"] if g == name or g.startswith(name + " "))
        return (n > 0, i18n.pick_now(f"{n} already created", f"已建 {n} 个群") if n else "")
    if kind == "agent":
        ok = name in st["agents"]
        return (ok, i18n.pick_now("A member with this name exists", "已有同名成员") if ok else "")
    if kind == "skill":
        ok = name in st["skills"]
        return (ok, i18n.pick_now("Already in the skill library", "已在技能库") if ok else "")
    if kind == "prompt":
        ok = name in st["prompts"]
        return (ok, i18n.pick_now("Already in the prompt library", "已在提示词库") if ok else "")
    ok = name in st["mcp"]
    return (ok, i18n.pick_now("Already added", "已添加") if ok else "")


def _slim(item: dict, st: dict) -> dict:
    installed, note = _state(item, st)
    return {k: v for k, v in item.items() if k != "def"} | {"installed": installed, "state_note": note}


def _localized(items: list[dict]) -> list[dict]:
    """内置条目按请求语言返回(英文基础字段 / 中文 `<字段>_zh`)。"""
    lang = i18n.current()
    return [i18n.localize(it, lang) for it in items]


def overview(store: Store) -> dict:
    items = _localized(_all_items(store))
    st = _states(store)
    counts = {k: 0 for k in KINDS}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    custom = _load_custom(store)
    return {
        "catalog_version": CATALOG_VERSION,
        "schema_version": SCHEMA_VERSION,
        "categories": CATEGORIES,
        "counts": counts,
        "total": len(items),
        "items": [_slim(it, st) for it in items],
        "custom": {"dir": custom.dir, "exists": custom.exists, "files": custom.files,
                   "loaded": len(custom.items), "errors": custom.errors},
    }


def find(store: Store, item_id: str) -> dict | None:
    item = next((r for r in _localized(_all_items(store)) if r["id"] == item_id), None)
    if item is None:
        return None
    installed, note = _state(item, _states(store))
    return item | {"installed": installed, "state_note": note}


# --------------------------------------------------------------------- 应用
def _unique_group_name(store: Store, base: str) -> str:
    base = _clip(base or "新群聊", 60) or "新群聊"
    taken = {g["name"] for g in store.list_groups()}
    if base not in taken:
        return base
    for n in range(2, 50):
        cand = _clip(base, 60 - len(str(n)) - 1) + f" {n}"
        if cand not in taken:
            return cand
    return _clip(base, 55) + " 副本"


def _defs_index(store: Store) -> dict[str, dict]:
    """按名字索引可用的技能与成员定义(内置 + 自定义)。"""
    skills: dict[str, dict] = {n: {**e, "name": n} for n, e in EXAMPLE_SKILLS.items()}
    agents: dict[str, dict] = {}
    for it in _load_custom(store).items:
        d = it.get("def") or {}
        if it["kind"] == "skill":
            skills.setdefault(it["name"], d)
        elif it["kind"] == "agent":
            agents[it["name"]] = d
    return {"skills": skills, "agents": agents}


def _ensure_member(store: Store, name: str, idx: dict) -> dict | None:
    """复用同名成员;没有就按岗位预设/自定义角色创建。"""
    for a in store.list_agents():
        if a["name"] == name:
            return a
    a = ensure_agent(store, name)
    if a is not None:
        return a
    d = idx["agents"].get(name)
    if not d:
        return None
    return store.create_agent(name, d.get("avatar", "🤖"), d.get("role", ""), d.get("prompt", ""),
                              None, [], d.get("tags"))


def _install_skill(store: Store, name: str, idx: dict, overwrite: bool) -> str:
    d = store.data_dir / "skills"
    if name in {s.name for s in list_skills(d)} and not overwrite:
        return "skipped"
    sk = idx["skills"].get(name)
    if not sk:
        return "missing"
    write_skill(d, name, sk.get("description", ""), sk.get("body", ""),
                sk.get("scope", "member"), version=CATALOG_VERSION)
    return "written"


def apply(store: Store, item_id: str, opts: dict | None = None) -> dict:
    opts = opts or {}
    item = find(store, item_id)
    if item is None:
        raise GalleryError("模板不存在(可能刚被移除)")
    fn = {"team": _apply_team, "agent": _apply_agent, "skill": _apply_skill,
          "prompt": _apply_prompt, "mcp": _apply_mcp}[item["kind"]]
    return fn(store, item, opts)


def _result(item: dict, summary: str, **kw: Any) -> dict:
    return {"kind": item["kind"], "id": item["id"], "name": item["name"], "summary": summary,
            "group": None, "agents": [], "added": [], "skipped": [], "notes": [], **kw}


def _apply_team(store: Store, item: dict, opts: dict) -> dict:
    src = item["def"]
    idx = _defs_index(store)
    members, created, reused, notes = [], [], [], []
    for n in src.get("members", []):
        existed = any(a["name"] in (builtin_names(builtin_for(n)) or [n]) for a in store.list_agents())
        a = _ensure_member(store, n, idx)
        if a is None:
            notes.append(f"找不到成员「{n}」的定义(自定义团队模板里的成员要能被岗位预设或自定义角色解析),已跳过")
            continue
        if a["id"] not in {m["id"] for m in members}:
            members.append(a)
            (reused if existed else created).append(n)
    if not members:
        raise GalleryError("这个模板没有可用的成员")

    host = next((m for m in members if m["name"] == src.get("host")), members[0])
    added, skipped = [], []
    skills, fresh, already = [], 0, 0
    for s in src.get("skills", []) or []:
        r = _install_skill(store, s, idx, bool(opts.get("overwrite")))
        if r == "missing":
            notes.append(f"技能「{s}」不在模板中心里,没有挂上去")
            continue
        skills.append(s)
        if r == "skipped":
            already += 1
            skipped.append(f"技能:{s}")
        else:
            fresh += 1
            added.append(f"技能:{s}")

    if fresh and already:
        extra = f",新装 {fresh} 个技能({already} 个本来就有)"
    elif fresh:
        extra = f",并装好 {fresh} 个技能"
    elif already:
        extra = f",随附的 {already} 个技能本来就有"
    else:
        extra = ""

    name = _unique_group_name(store, str(opts.get("name") or src.get("name") or item["name"]))
    g = store.create_group(name, host["id"], [m["id"] for m in members],
                           ext={"skills": skills}, prompt=src.get("prompt", ""))
    return _result(
        item, f"已建好群聊「{g['name']}」:群主 {host['name']},成员 {len(members)} 人{extra}。",
        group=g, agents=[m["name"] for m in members], added=added, skipped=skipped, notes=notes,
    )


def _apply_agent(store: Store, item: dict, opts: dict) -> dict:
    idx = _defs_index(store)
    existed = any(a["name"] in (builtin_names(builtin_for(item["name"])) or [item["name"]]) for a in store.list_agents())
    a = _ensure_member(store, item["name"], idx)
    if a is None:
        raise GalleryError("无法创建这个角色")
    added, notes = [], []
    if existed:
        notes.append(f"已有同名成员「{a['name']}」,直接复用了它")
    else:
        added.append(f"成员:{a['name']}")

    gid = opts.get("group_id")
    if gid:
        g = store.get_group(str(gid))
        if not g:
            raise GalleryError("要拉进的群聊不存在")
        if a["id"] in set(g.get("member_ids") or []):
            notes.append(f"「{a['name']}」本来就在这个群里")
        else:
            store.add_member(g["id"], a["id"])
            added.append(f"入群:{g['name']}")
    summary = f"已准备好成员「{a['name']}」"
    summary += f",并加入群「{g['name']}」。" if gid and not notes else "。"
    return _result(item, summary, agents=[a["name"]], added=added, notes=notes)


def _apply_skill(store: Store, item: dict, opts: dict) -> dict:
    idx = _defs_index(store)
    r = _install_skill(store, item["name"], idx, bool(opts.get("overwrite")))
    if r == "missing":
        raise GalleryError("这个技能没有可写入的正文")
    if r == "skipped":
        return _result(item, f"技能「{item['name']}」已经在技能库里了(要覆盖请选「重新导入」)。",
                       skipped=[f"技能:{item['name']}"],
                       notes=["同名技能已存在。想用模板里的版本覆盖它,点「重新导入」。"])
    scope = (idx["skills"].get(item["name"]) or {}).get("scope", "member")
    where = "在群聊右侧「扩展」里勾给某个群" if scope == "group" else "勾给需要的成员"
    return _result(item, f"已导入技能「{item['name']}」:{where}即可生效。",
                   added=[f"技能:{item['name']}"])


def _apply_prompt(store: Store, item: dict, opts: dict) -> dict:
    d = item["def"]
    title = d["title"]
    have = {p["title"]: p for p in store.list_prompts()}
    if title in have:
        if not opts.get("overwrite"):
            return _result(item, f"提示词「{title}」已经在库里了(要覆盖请选「重新导入」)。",
                           skipped=[f"提示词:{title}"],
                           notes=["同名提示词已存在。想用模板里的版本覆盖它,点「重新导入」。"])
        store.update_prompt(have[title]["id"], {"content": d["content"]})
        return _result(item, f"提示词「{title}」已更新为模板里的版本。", added=[f"提示词:{title}"])
    store.add_prompt(title, d["content"], d.get("kind", "general"), bool(d.get("use_globally")))
    return _result(item, f"已存入提示词库:「{title}」。到「提示词」页可以挂给某个群或设为全局。",
                   added=[f"提示词:{title}"])


def _apply_mcp(store: Store, item: dict, opts: dict) -> dict:
    d = item["def"]
    if any(m["name"] == d["name"] for m in store.list_mcp()):
        return _result(item, f"MCP 服务器「{d['name']}」已经添加过了。",
                       skipped=[f"MCP:{d['name']}"])
    args = [PLACEHOLDER_DIR if a == "/tmp" else str(a) for a in d.get("args", [])]
    row = store.add_mcp(d["name"], d.get("command", ""), args,
                        {k: "" for k in d.get("env_keys", [])}, "", "", {},
                        (d.get("note", "") + " 导入后是停用状态:核对命令、填好密钥后再到「MCP」页启用。").strip())
    store.update_mcp(row["id"], {"enabled": False})
    return _result(item, f"已添加 MCP 服务器「{d['name']}」(停用状态)。到「MCP」页核对命令、填好需要的东西再启用。",
                   added=[f"MCP:{d['name']}"],
                   notes=["MCP 会在你电脑上运行命令,本程序不会替你启用,也不会替你填任何密钥。"])
