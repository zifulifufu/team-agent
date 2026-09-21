"""融合 awesome-llm-apps(https://github.com/Shubhamsaboo/awesome-llm-apps,Apache-2.0):

把那个仓库里的多智能体团队、Agent 技能、MCP 用法、单智能体的提示词,提取成 Team Agent 里能直接用的
「群聊模板 / 技能 / MCP 预填表单 / 提示词与成员预设」。

  * 只做静态阅读:用 ast 解析 Python 源码、读 README 和 SKILL.md,**从不 import、不运行仓库里的任何代码**;
  * 提取结果是一份 JSON 快照(随程序附带在 app/data/awesome_apps.json,数据目录里的同名文件可由用户
    「从本地克隆刷新」覆盖);
  * 提取到的提示词是原作者的英文文字,按 Apache-2.0 保留来源与署名(每一条都带 source_path 和仓库地址);
  * MCP 只提取「命令 + 参数 + 需要哪些环境变量名」,**从不提取环境变量的值**,参数里像密钥的内容会被替换。
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
import time
from pathlib import Path
from typing import Any

from .memory import looks_sensitive

SHIPPED = Path(__file__).parent / "data" / "awesome_apps.json"

SOURCE = {
    "name": "awesome-llm-apps",
    "url": "https://github.com/Shubhamsaboo/awesome-llm-apps",
    "license": "Apache-2.0",
    "author": "Shubham Saboo 及贡献者",
}

# 参与提取的顶层目录 → 中文分类。ai_agent_framework_crash_course 是逐框架的教学片段,不当作现成应用。
CATEGORIES = {
    "starter_ai_agents": "入门智能体",
    "advanced_ai_agents": "进阶智能体 / 多智能体团队",
    "advanced_llm_apps": "进阶 LLM 应用",
    "rag_tutorials": "RAG(检索增强)",
    "mcp_ai_agents": "MCP 智能体",
    "voice_ai_agents": "语音智能体",
    "generative_ui_agents": "生成式界面智能体",
    "always_on_agents": "常驻智能体",
}
SKILLS_DIR = "agent_skills"

MAX_PY_BYTES = 400_000
MAX_PY_PER_APP = 30
MAX_AGENTS_PER_APP = 12
MAX_TEXT = 6000                 # 单条提示词的长度上限
SKILL_BODY_LIMIT = 3600         # 技能正文上限:Team Agent 每个成员的技能提示总共只放 4000 字

AGENT_CALLS = {"Agent", "LlmAgent", "AssistantAgent", "ConversableAgent"}
TEAM_CALLS = {"Team"}
PIPELINE_CALLS = {"SequentialAgent", "ParallelAgent", "LoopAgent"}
PROMPT_KEYS = ("instructions", "instruction", "system_message", "system_prompt", "backstory", "goal")


class AwesomeError(Exception):
    pass


# =================================================================== 字符串还原
class _Env:
    """一个文件里的简单赋值(NAME = "…" / 列表 / 拼接),用来还原 instructions=SOME_PROMPT 这类引用。"""

    def __init__(self, tree: ast.AST):
        self.vars: dict[str, ast.AST] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                self.vars.setdefault(n.targets[0].id, n.value)


def _text(node: ast.AST | None, env: _Env, depth: int = 0) -> str | None:
    if node is None or depth > 5:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        out = []
        for p in node.values:
            if isinstance(p, ast.Constant):
                out.append(str(p.value))
            elif isinstance(p, ast.FormattedValue):
                try:
                    out.append("{" + ast.unparse(p.value) + "}")
                except Exception:  # noqa: BLE001
                    out.append("{…}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text(node.left, env, depth + 1), _text(node.right, env, depth + 1)
        return None if a is None and b is None else (a or "") + (b or "")
    if isinstance(node, (ast.List, ast.Tuple)):
        parts = [t for t in (_text(e, env, depth + 1) for e in node.elts) if t]
        return "\n".join(parts) if parts else None
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in ("dedent", "strip", "lstrip", "rstrip", "format", "join") and isinstance(fn, ast.Attribute) and name != "join":
            return _text(fn.value, env, depth + 1)
        if name == "dedent" and node.args:
            return _text(node.args[0], env, depth + 1)
        return None
    if isinstance(node, ast.Name):
        return _text(env.vars.get(node.id), env, depth + 1) if node.id in env.vars else None
    return None


def _clean(text: str | None, limit: int = MAX_TEXT) -> str:
    if not text:
        return ""
    t = textwrap.dedent(text).strip()
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t[:limit].rstrip()


def _fname(call: ast.Call) -> str:
    fn = call.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def _kw(call: ast.Call, *names: str) -> ast.AST | None:
    for k in call.keywords:
        if k.arg in names:
            return k.value
    return None


def _tool_names(node: ast.AST | None) -> list[str]:
    out: list[str] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for e in node.elts:
            if isinstance(e, ast.Call):
                out.append(_fname(e))
            elif isinstance(e, ast.Name):
                out.append(e.id)
            elif isinstance(e, ast.Attribute):
                out.append(e.attr)
    return [x for x in out if x]


def _model_id(node: ast.AST | None, env: _Env) -> str:
    if isinstance(node, ast.Call):
        v = _kw(node, "id", "model", "name")
        t = _text(v, env)
        if t:
            return t[:60]
        if node.args:
            return (_text(node.args[0], env) or "")[:60]
    return (_text(node, env) or "")[:60]


# =================================================================== 智能体提取
class _Collector(ast.NodeVisitor):
    def __init__(self, env: _Env):
        self.env = env
        self.agents: list[dict] = []
        self.teams: list[dict] = []
        self.pipelines: list[dict] = []
        self._assigned: str = ""

    def visit_Assign(self, node: ast.Assign) -> None:
        prev = self._assigned
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
            self._assigned = node.targets[0].id
        self.generic_visit(node)
        self._assigned = prev

    def visit_Call(self, node: ast.Call) -> None:
        name = _fname(node)
        var, self._assigned = self._assigned, ""
        if name in AGENT_CALLS:
            a = self._agent(node, var)
            if a:
                self.agents.append(a)
        elif name in TEAM_CALLS:
            self.teams.append(self._team(node, var))
        elif name in PIPELINE_CALLS:
            self.pipelines.append(self._pipeline(node, name, var))
        self.generic_visit(node)

    def _agent(self, call: ast.Call, var: str) -> dict | None:
        env = self.env
        if _kw(call, "sub_agents") is not None:         # 只是把别的智能体串起来的外壳(如 SequentialAgent 式的 LlmAgent),不是一个成员
            self.pipelines.append({"var": var, "kind": "LlmAgent", "name": _clean(_text(_kw(call, "name"), env), 60),
                                   "order": self._members(_kw(call, "sub_agents"))})
            return None
        name = _clean(_text(_kw(call, "name"), env), 60)
        role = _clean(_text(_kw(call, "role"), env), 200)
        desc = _clean(_text(_kw(call, "description"), env), 600)
        goal = _clean(_text(_kw(call, "goal"), env), 600)
        back = _clean(_text(_kw(call, "backstory"), env), 1200)
        instr = _clean(_text(_kw(call, "instructions", "instruction", "system_message", "system_prompt"), env))
        if not (instr or goal or back or role or desc):
            return None
        if not name:
            name = role if (not var and role) else var.replace("_agent", "").replace("_", " ").strip().title() or role
        if not name:
            return None
        if goal or back:                                    # CrewAI 风格:目标 + 背景故事
            instr = "\n\n".join(x for x in (instr, ("Goal: " + goal) if goal else "", ("Background: " + back) if back else "") if x)
        return {
            "var": var, "name": name, "role": role, "description": desc, "instructions": instr,
            "tools": _tool_names(_kw(call, "tools")), "model": _model_id(_kw(call, "model", "llm"), env),
        }

    def _members(self, node: ast.AST | None) -> list[str]:
        if isinstance(node, (ast.List, ast.Tuple)):
            return [e.id for e in node.elts if isinstance(e, ast.Name)]
        return []

    def _team(self, call: ast.Call, var: str) -> dict:
        env = self.env
        return {
            "var": var, "name": _clean(_text(_kw(call, "name"), env), 60),
            "instructions": _clean(_text(_kw(call, "instructions", "instruction", "description"), env)),
            "members": self._members(_kw(call, "members")),
        }

    def _pipeline(self, call: ast.Call, kind: str, var: str) -> dict:
        return {"var": var, "kind": kind, "name": _clean(_text(_kw(call, "name"), self.env), 60),
                "order": self._members(_kw(call, "sub_agents"))}


def _parse(path: Path) -> ast.AST | None:
    try:
        if path.stat().st_size > MAX_PY_BYTES:
            return None
        return ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


# =================================================================== README / 分类辅助
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]+")


def _readme_info(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:6000]
    except OSError:
        return "", ""
    title, desc = "", ""
    for line in text.splitlines():
        s = line.strip()
        if not title and s.startswith("#"):
            title = _EMOJI_RE.sub("", s.lstrip("#")).strip()
            continue
        if title and s and not s.startswith(("#", "!", "<", "```", "[!", "|", "-", "*", ">")):
            desc = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
            desc = _EMOJI_RE.sub("", re.sub(r"[*_`]", "", desc)).strip()
            break
    return title[:80], desc[:240]


def _framework(tree_imports: set[str]) -> str:
    for key, label in (("agno", "Agno"), ("google.adk", "Google ADK"), ("crewai", "CrewAI"), ("autogen", "AutoGen"),
                       ("ag2", "AG2"), ("langgraph", "LangGraph"), ("langchain", "LangChain"), ("openai", "OpenAI SDK"),
                       ("swarm", "Swarm"), ("pydantic_ai", "PydanticAI"), ("smolagents", "smolagents")):
        if any(i == key or i.startswith(key + ".") or i.startswith(key.replace(".", "_")) for i in tree_imports):
            return label
    return ""


def _imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def _slug(rel: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")[:90]


def _safe_member_name(name: str) -> str:
    n = re.sub(r"[@\s]+", "-", name.strip())
    n = re.sub(r"[^\w\-·.一-鿿]", "", n).strip("-_.")
    return n[:30] or "成员"


# =================================================================== MCP 提取
_CMD_RE = re.compile(r"^(npx|uvx|uv|docker|node|python3?|deno|bunx)\s+\S")
_SECRET_ARG = "<你的密钥>"


def _scrub_args(args: list[str]) -> list[str]:
    out = []
    for a in args:
        out.append(_SECRET_ARG if looks_sensitive(a) or re.match(r"^(sk|ghp|github_pat|xox[bp]|AIza)[-_A-Za-z0-9]{10,}", a) else a)
    return out


def _server_name(command: str, args: list[str]) -> str:
    cands = [a for a in args if not a.startswith("-")] or [command]
    pkg = cands[0] if command not in ("docker",) else next((a for a in args if "/" in a and not a.startswith("-")), cands[0])
    pkg = pkg.split("@latest")[0].rsplit("/", 1)[-1]
    pkg = re.sub(r"^(server-|mcp-server-|mcp-)", "", pkg)
    return pkg[:40] or command


def _mcp_from_tree(tree: ast.AST, env: _Env) -> list[dict]:
    found: list[dict] = []

    def add(command: str, args: list[str], env_keys: list[str], note: str = "") -> None:
        if not command or command not in ("npx", "uvx", "uv", "docker", "node", "python", "python3", "deno", "bunx"):
            return
        args = _scrub_args([a for a in args if a])
        found.append({"command": command, "args": args, "env_keys": sorted(set(env_keys)), "name": _server_name(command, args), "note": note})

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _fname(n) == "StdioServerParameters":
            cmd = _text(_kw(n, "command"), env) or ""
            av = _kw(n, "args")
            args = [t for t in (_text(e, env) for e in av.elts) if t] if isinstance(av, (ast.List, ast.Tuple)) else []
            ev = _kw(n, "env")
            keys = [_text(k, env) or "" for k in ev.keys if k is not None] if isinstance(ev, ast.Dict) else []
            add(cmd, args, [k for k in keys if k])
        elif isinstance(n, ast.Dict):                       # {"name": "github", "command": "npx", "args": [...]}
            d = {(_text(k, env) if k is not None else None): v for k, v in zip(n.keys, n.values)}
            if "command" in d and "args" in d and isinstance(d["args"], (ast.List, ast.Tuple)):
                add(_text(d["command"], env) or "", [t for t in (_text(e, env) for e in d["args"].elts) if t], [])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and _CMD_RE.match(n.value.strip()) and len(n.value) < 200:
            parts = n.value.strip().split()
            if parts[0] in ("npx", "uvx", "docker", "bunx", "deno") and any("mcp" in p.lower() or "server" in p.lower() for p in parts[1:]):
                add(parts[0], parts[1:], [])
    return found


# =================================================================== 提取一个应用
def _find_apps(root: Path) -> list[Path]:
    """叶子应用目录:自己有 README.md、子树里有 .py,且下面没有另一个也满足条件的目录。"""
    cands: list[Path] = []
    for top in CATEGORIES:
        base = root / top
        if not base.is_dir():
            continue
        for readme in base.rglob("README.md"):
            d = readme.parent
            if any(p.startswith(".") or p in ("node_modules", "venv", ".venv", "__pycache__") for p in d.relative_to(root).parts):
                continue
            if d == base:
                continue
            if next(d.rglob("*.py"), None) is not None:
                cands.append(d)
    def own_py(d: Path) -> bool:
        return any(True for _ in d.glob("*.py"))

    leaves = [d for d in cands if own_py(d) or not any(o != d and d in o.parents for o in cands)]
    return sorted(set(leaves))


def _extract_app(root: Path, d: Path, others: list[Path] = ()) -> tuple[dict | None, list[dict]]:  # type: ignore[assignment]
    rel = d.relative_to(root).as_posix()
    title, desc = _readme_info(d / "README.md")
    if not title or re.search(r"install|getting started|setup|readme", title, re.I):
        title = d.name.replace("_", " ").replace("-", " ").title()
    files = [f for f in sorted(d.rglob("*.py")) if not any(o != d and d in o.parents and o in f.parents for o in others)][:MAX_PY_PER_APP]
    agents: dict[str, dict] = {}
    teams: list[dict] = []
    pipes: list[dict] = []
    imports: set[str] = set()
    mcp: list[dict] = []
    tools: set[str] = set()
    for f in files:
        if any(part in ("tests", "test", ".venv", "venv", "node_modules") for part in f.relative_to(d).parts):
            continue
        tree = _parse(f)
        if tree is None:
            continue
        env = _Env(tree)
        imports |= _imports(tree)
        col = _Collector(env)
        col.visit(tree)
        for a in col.agents:
            key = a["name"].lower()
            old = agents.get(key)
            if old is None or len(a["instructions"]) > len(old["instructions"]):
                agents[key] = {**a, "file": f.relative_to(root).as_posix()}
        teams += col.teams
        pipes += col.pipelines
        mcp += [{**m, "source_path": f.relative_to(root).as_posix()} for m in _mcp_from_tree(tree, env)]
    alist = list(agents.values())[:MAX_AGENTS_PER_APP]
    for a in alist:
        tools.update(a["tools"])
    by_var = {a["var"]: a for a in alist if a["var"]}

    lead = None
    order: list[str] = []
    for t in teams:
        if t["members"]:
            order = [by_var[v]["name"] for v in t["members"] if v in by_var]
            if t["name"] or t["instructions"]:
                lead = {"name": t["name"] or "Team Lead", "instructions": t["instructions"]}
            break
    sequential = False
    if not order:
        for p in pipes:
            names = [by_var[v]["name"] for v in p["order"] if v in by_var]
            if len(names) >= 2:
                order, sequential = names, p["kind"] == "SequentialAgent"
                break
    if order:
        rest = [a["name"] for a in alist if a["name"] not in order]
        order = order + rest
    else:
        order = [a["name"] for a in alist]
    ordered = [next(a for a in alist if a["name"] == n) for n in order]
    generic = {"agent", "agno", "assistant", "bot"}
    for a in ordered:
        a.pop("var", None)
        if a["name"].lower() in generic:
            a["name"] = _safe_member_name(title or d.name.replace("_", " ").title())[:30]
    if not ordered and not mcp:
        return None, []
    app = {
        "id": _slug(rel), "title": title or d.name.replace("_", " ").title(), "desc": desc,
        "category": CATEGORIES.get(rel.split("/", 1)[0], ""), "path": rel,
        "framework": _framework(imports), "kind": "team" if len(ordered) >= 2 else "agent",
        "agents": ordered, "lead": lead, "sequential": sequential, "tools": sorted(tools),
    }
    return (app if ordered else None), mcp


# =================================================================== 技能
def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    key = ""
    for line in m.group(1).splitlines():
        if re.match(r"^[A-Za-z_][\w-]*:", line):
            k, _, v = line.partition(":")
            key, v = k.strip(), v.strip()
            meta[key] = "" if v in (">-", ">", "|", "|-") else v.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            sub = line.strip()
            if ":" in sub and key in ("metadata",):
                k2, _, v2 = sub.partition(":")
                meta[k2.strip()] = v2.strip().strip("\"'")
            else:
                meta[key] = (meta.get(key, "") + " " + sub).strip()
    return meta, m.group(2)


_RUNTIME_HINT = re.compile(r"\b(python3?|bash|shell|git|npx|uvx|docker|curl|jq|cli)\b|scripts/|\.py\b|\.sh\b", re.I)


def _extract_skills(root: Path) -> list[dict]:
    out = []
    base = root / SKILLS_DIR
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        f = d / "SKILL.md"
        if not d.is_dir() or not f.is_file() or d.name == "evals":
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        meta, body = _frontmatter(text)
        body = body.strip()
        needs_runtime = (d / "scripts").is_dir() or bool(_RUNTIME_HINT.search(meta.get("compatibility", ""))) or (
            len(re.findall(r"```(?:bash|sh|shell)", body)) >= 2)
        clipped = len(body) > SKILL_BODY_LIMIT
        if clipped:
            cut = body[:SKILL_BODY_LIMIT]
            body = cut[: cut.rfind("\n")].rstrip() + "\n\n(篇幅所限,后面的内容已省略;完整版见来源:" + SOURCE["url"] + "/tree/main/" + f"{SKILLS_DIR}/{d.name})"
        out.append({
            "id": d.name, "name": meta.get("name") or d.name, "description": _clean(meta.get("description", ""), 500),
            "body": body, "clipped": clipped, "needs_runtime": needs_runtime,
            "compatibility": _clean(meta.get("compatibility", ""), 300), "author": meta.get("author", ""),
            "version": meta.get("version", ""), "license": meta.get("license", "Apache-2.0"),
            "source_path": f"{SKILLS_DIR}/{d.name}/SKILL.md",
        })
    return out


# =================================================================== 总入口
def check_root(root: Path) -> None:
    if not root.is_absolute():
        raise AwesomeError("请填写完整的绝对路径")
    if not root.is_dir():
        raise AwesomeError("这个文件夹不存在")
    if not (root / "README.md").is_file() or not any((root / t).is_dir() for t in CATEGORIES):
        raise AwesomeError("这不像 awesome-llm-apps 的克隆目录(找不到 README.md 和 advanced_ai_agents 等文件夹)")
    lic = root / "LICENSE"
    if not lic.is_file() or "Apache License" not in lic.read_text(encoding="utf-8", errors="ignore")[:400]:
        raise AwesomeError("没有找到 Apache-2.0 的 LICENSE 文件,为保证署名与授权,不能导入")


def _git_head(root: Path) -> str:
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            return (root / ".git" / head.split(" ", 1)[1]).read_text().strip()[:12] or ""
        return head[:12]
    except OSError:
        return ""


def extract(root: Path | str) -> dict:
    root = Path(root)
    check_root(root)
    apps, mcp_all = [], []
    dirs = _find_apps(root)
    for d in dirs:
        app, mcp = _extract_app(root, d, dirs)
        if app:
            apps.append(app)
        mcp_all += mcp
    seen: set[tuple] = set()
    mcp_out = []
    for m in mcp_all:
        key = (m["command"], tuple(m["args"]))
        if key in seen:
            continue
        seen.add(key)
        m["id"] = _slug(f"{m['name']}-{len(mcp_out) + 1}")
        mcp_out.append(m)
    teams = [a for a in apps if a["kind"] == "team"]
    agents = [a for a in apps if a["kind"] == "agent"]
    return {
        "source": SOURCE, "commit": _git_head(root), "generated_at": time.strftime("%Y-%m-%d"),
        "teams": teams, "agents": agents, "skills": _extract_skills(root), "mcp": mcp_out,
    }


# =================================================================== 载入与校验
def validate(data: object) -> str | None:
    if not isinstance(data, dict):
        return "不是 JSON 对象"
    for k in ("teams", "agents", "skills", "mcp"):
        if not isinstance(data.get(k), list):
            return f"缺少 {k}"
    for t in data["teams"] + data["agents"]:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not isinstance(t.get("agents"), list):
            return "应用条目格式不对"
    return None


def load(data_dir: Path) -> tuple[dict, str]:
    """(数据, 来源 shipped|local)。数据目录里有用户刷新过的就用它。"""
    for p, src in ((Path(data_dir) / "awesome_apps.json", "local"), (SHIPPED, "shipped")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if validate(data) is None:
            return data, src
    return {"source": SOURCE, "commit": "", "generated_at": "", "teams": [], "agents": [], "skills": [], "mcp": []}, "empty"


# =================================================================== 转成 Team Agent 的对象
_AVATARS = [
    (("search", "research", "web", "news"), "🔎"), (("code", "coding", "developer", "engineer", "program"), "💻"),
    (("financ", "stock", "invest", "trading"), "💹"), (("legal", "law", "contract"), "⚖️"),
    (("design", "ui", "ux", "visual"), "🎨"), (("teach", "learn", "tutor", "education", "professor"), "🎓"),
    (("travel", "trip", "itinerar", "hotel", "flight"), "🧭"), (("recruit", "hiring", "resume", "candidate"), "🧑‍💼"),
    (("seo", "market", "sales", "competitor"), "📈"), (("write", "content", "blog", "copy"), "✍️"),
    (("health", "medical", "fitness", "nutrition", "diet"), "🩺"), (("game",), "🎮"), (("real estate", "property"), "🏠"),
    (("data", "analy"), "📊"), (("review", "critic", "audit"), "🧐"), (("plan", "strategy", "manager", "lead", "coordinator"), "🧭"),
]
_TAGS = [
    (("search", "research", "web", "news", "scrape", "crawl"), ["长文本", "工具调用"]),
    (("code", "coding", "developer", "engineer", "program", "debug"), ["代码", "推理"]),
    (("financ", "stock", "invest", "data", "analy", "statistic"), ["推理"]),
    (("write", "content", "blog", "copy", "story", "script", "creative"), ["写作"]),
    (("translat", "chinese", "language"), ["中文", "写作"]),
    (("plan", "strategy", "manager", "lead", "coordinator", "review", "critic", "audit"), ["推理"]),
    (("summar", "read", "document", "pdf", "rag"), ["长文本"]),
    (("vision", "image", "video", "multimodal", "design"), ["多模态"]),
]


def _guess(text: str, table: list) -> Any:
    low = text.lower()
    for keys, val in table:
        if any(k in low for k in keys):
            return val
    return None


def member_from(a: dict, app_title: str) -> dict:
    """提取到的智能体 → create_agent 所需的字段。"""
    probe = f"{a['name']} {a.get('role', '')} {a.get('description', '')}"
    role = a.get("role") or a.get("description") or ""
    parts = []
    if a.get("role"):
        parts.append(f"Role: {a['role']}")
    if a.get("description"):
        parts.append(a["description"])
    if a.get("instructions"):
        parts.append(a["instructions"])
    notes = []
    if a.get("tools"):
        notes.append("the original app gave this agent tools (" + ", ".join(a["tools"][:6]) + "); they may not exist here — "
                     "if you cannot do something without them, say so plainly instead of pretending")
    prompt = "\n\n".join(parts) or "You are a helpful specialist."
    prompt += (f"\n\n(This role comes from the open-source example \"{app_title}\" in awesome-llm-apps. "
               + ("; ".join(notes) + ". " if notes else "") + "Reply in the language the user writes in.)")
    return {
        "name": _safe_member_name(a["name"]), "avatar": _guess(probe, _AVATARS) or "🤖", "role": role[:60],
        "prompt": prompt[:MAX_TEXT + 600], "tags": _guess(probe, _TAGS) or ["推理"],
    }
