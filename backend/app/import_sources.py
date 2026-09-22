"""Importing plugin-equivalents from other AI applications.

**What can actually travel, and what cannot.** This matters more than the code below,
because the honest answer is narrower than "install plugins from other AI apps":

* **MCP servers — yes.** One cross-vendor standard, and it is what "plugin" has settled on
  across the ecosystem: Claude Desktop, Claude Code, Codex, Cursor, Windsurf, Cline, Roo,
  Continue, LM Studio and Zed all keep the same `command` / `args` / `env` or `url` /
  `headers` shape somewhere on disk. These are portable, and they are what this module
  imports.
* **Skills — yes**, when they are Claude-style `SKILL.md` folders: this project's own
  skills use the same frontmatter-plus-markdown format, so the text moves across as-is.
* **WorkBuddy's own experts, skills and connectors — yes**, and this is the common case
  when this app is installed next to WorkBuddy. It keeps them in three separate shapes on
  disk — a skill folder, an expert package whose `agents/*.md` is the prompt, and
  `mcp.json` for connectors — and each one maps onto something that already exists here
  (a skill, a member, an MCP server). `SOURCES` below says where each one lives.
* **This project's own plugins — no.** A plugin here is a Python file calling
  `register()`. No other application produces that, so there is nothing to import; the
  plugin format stays hand-written on purpose (see README, "Plugins").
* **ChatGPT GPTs / Cursor extensions / VS Code extensions — no.** A GPT is a prompt plus
  authenticated actions behind a login; an extension is TypeScript against a different
  host API. Neither has a file that could be read into this app, and an importer that
  appeared to handle them would silently import nothing.

**How it reads.** Only the fixed paths in `SOURCES`, and only the files they name: no
recursive search of the home directory, no following symlinks, a size cap per file and a
cap on how many entries are handled. Secrets in a source config (`env`, `headers`) are
masked on the way out over the API — the import step re-reads the file on the server, so a
credential never has to reach the browser to be imported.

**What it never does.** Nothing is executed, and nothing is enabled: the scan reads text,
and an import writes rows. Connecting an MCP server stays a separate, explicit action in
the MCP page.
"""

from __future__ import annotations

import glob
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from . import i18n
from .mcp_client import parse_mcp_json
from .tools import frontmatter_value

# Bounds: a config file is a few kilobytes, and a skill is a document. Anything larger is
# not what this is looking for, and a home directory is not something to walk unbounded.
MAX_FILE_BYTES = 512 * 1024
MAX_SKILL_BYTES = 256 * 1024
# The ceiling on one source's entries and on a whole scan. It has to sit above a real total: a
# single WorkBuddy connector record holds 227 servers, and the connector catalogue is another
# 226, so a lower number would quietly present a shorter list than the machine actually has.
MAX_ITEMS = 600
MAX_SKILL_DEPTH = 4
# How many of another application's installed plugins are looked at. Its own record of what
# is installed is the only thing read, so this is a sanity bound rather than a search limit.
MAX_PLUGINS = 400
# What an expert's prompt is allowed to be. Expert packages are documents, but one of them
# being a megabyte would mean it is not a prompt.
MAX_EXPERT_BYTES = 256 * 1024

SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|credential|auth)", re.I)
# Characters that turn an argument list into something a shell would interpret. An MCP
# server is started without a shell, so these are the cases where the two differ.
SHELL_HINT = re.compile(r"[;&|`$><]|\$\(")
DOWNLOADERS = {"npx", "pnpx", "uvx", "bunx", "pipx"}
AUTO_YES = {"-y", "--yes", "-f", "--force"}


def _home() -> Path:
    return Path.home()


def _expand(raw: str) -> Path:
    s = raw
    if s.startswith("~"):
        s = str(_home()) + s[1:]
    return Path(s)


# ------------------------------------------------------- what another app has installed
# WorkBuddy keeps every version of a plugin side by side under `plugins/cache`
# (`…/sheetagent/5.5.6-…` next to `…/sheetagent/0.1.1784877812`), so globbing that tree would
# offer the same expert three times over. Its own `installed_plugins.json` says which copy is
# the installed one — one entry per plugin, no duplicates — so that record is what is read.
WORKBUDDY_PLUGINS = "~/.workbuddy/plugins/installed_plugins.json"


def installed_plugins() -> list[dict]:
    """Every plugin another WorkBuddy-style app records as installed: `{key, marketplace, path}`.

    Paths come out of a file this program does not control, so two things are enforced here
    rather than trusted: the record itself has to be inside the home directory, and every
    install path it names has to resolve into the home directory as well. A plugin list is
    not a reason to read anywhere else on the disk.
    """
    out: list[dict] = []
    root = _home().resolve()
    record = _expand(WORKBUDDY_PLUGINS)
    try:
        if record.is_symlink() or not record.is_file() or record.stat().st_size > MAX_FILE_BYTES:
            return []
        data = json.loads(record.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return []
    for key, entries in list(plugins.items())[:MAX_PLUGINS]:
        for e in entries if isinstance(entries, list) else []:
            raw = str((e or {}).get("installPath") or "") if isinstance(e, dict) else ""
            if not raw:
                continue
            p = Path(raw)
            try:
                real = p.resolve()
                if p.is_symlink() or not real.is_dir() or not real.is_relative_to(root):
                    continue
            except (OSError, ValueError):
                continue
            out.append({"key": str(key), "marketplace": str(key).split("@")[-1], "path": real})
            break                      # one install per plugin: the record can hold several
    return out


# ------------------------------------------------------------------ the table
# One entry per application. `paths` is per platform because the operating system decides
# where an app keeps its settings, and a wrong path is invisible: the source simply shows
# as "not found" and the user concludes the feature does not work.
SOURCES: list[dict[str, Any]] = [
    {"key": "claude-desktop", "app": "Claude Desktop", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/Library/Application Support/Claude/claude_desktop_config.json"],
               "win32": ["%APPDATA%/Claude/claude_desktop_config.json"],
               "linux": ["~/.config/Claude/claude_desktop_config.json"]},
     "notes": ("The desktop app's own MCP list. Servers you added there work here too.",
               "桌面应用自己的 MCP 清单。在那里加过的服务器这里也能用。")},
    {"key": "claude-code", "app": "Claude Code", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/.claude.json"], "win32": ["~/.claude.json"], "linux": ["~/.claude.json"]},
     "notes": ("Claude Code keeps its MCP servers in ~/.claude.json, next to a lot of unrelated "
               "state, so only the mcpServers key is read.",
               "Claude Code 把 MCP 服务器放在 ~/.claude.json 里,和大量无关状态混在一起,所以只读 mcpServers 这一项。")},
    {"key": "codex", "app": "Codex CLI", "kind": "mcp", "format": "toml",
     "toml_key": "mcp_servers",
     "paths": {"darwin": ["~/.codex/config.toml"], "win32": ["~/.codex/config.toml"],
               "linux": ["~/.codex/config.toml"]},
     "notes": ("Codex writes MCP servers as [mcp_servers.name] tables; the extra timeout keys it "
               "allows are ignored here.",
               "Codex 用 [mcp_servers.名字] 段落写 MCP 服务器;它允许的超时等额外键在这里会被忽略。")},
    {"key": "cursor", "app": "Cursor", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/.cursor/mcp.json"], "win32": ["%APPDATA%/Cursor/mcp.json"],
               "linux": ["~/.config/Cursor/mcp.json"]},
     "notes": ("Cursor's global MCP file. A project's .cursor/mcp.json is not read: it belongs "
               "to that project, not to this machine's setup.",
               "Cursor 的全局 MCP 文件。项目里的 .cursor/mcp.json 不读:那属于那个项目,不属于本机配置。")},
    {"key": "windsurf", "app": "Windsurf", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/.codeium/windsurf/mcp_config.json"],
               "win32": ["%APPDATA%/Windsurf/mcp_config.json"],
               "linux": ["~/.codeium/windsurf/mcp_config.json"]},
     "notes": ("", "")},
    {"key": "cline", "app": "Cline (VS Code)", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"],
               "win32": ["%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"],
               "linux": ["~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"]},
     "notes": ("Read from the VS Code extension's own storage, wherever VS Code was told to keep it.",
               "从 VS Code 扩展自己的存储里读,位置以 VS Code 的设置目录为准。")},
    {"key": "roo", "app": "Roo Code (VS Code)", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json"],
               "win32": ["%APPDATA%/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json"],
               "linux": ["~/.config/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json"]},
     "notes": ("", "")},
    {"key": "continue", "app": "Continue", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers", "experimental.mcpServers"],
     "paths": {"darwin": ["~/.continue/config.json"], "win32": ["~/.continue/config.json"],
               "linux": ["~/.continue/config.json"]},
     "notes": ("", "")},
    {"key": "lmstudio", "app": "LM Studio", "kind": "mcp", "format": "json",
     "json_keys": ["mcpServers"],
     "paths": {"darwin": ["~/.lmstudio/mcp.json"], "win32": ["~/.lmstudio/mcp.json"],
               "linux": ["~/.lmstudio/mcp.json"]},
     "notes": ("", "")},
    {"key": "zed", "app": "Zed", "kind": "mcp", "format": "json",
     "json_keys": ["context_servers"],
     "paths": {"darwin": ["~/.config/zed/settings.json"], "win32": ["%APPDATA%/Zed/settings.json"],
               "linux": ["~/.config/zed/settings.json"]},
     "notes": ("Zed calls them context servers; the shape is the same.",
               "Zed 管这个叫 context servers,结构是一样的。")},
    {"key": "claude-plugin-mcp", "app": "Claude Code plugins", "kind": "mcp", "format": "glob",
     "glob": "~/.claude/plugins/marketplaces/*/plugins/*/.mcp.json",
     "json_keys": ["mcpServers"],
     "notes": ("MCP servers an installed Claude Code plugin ships in its own .mcp.json.",
               "已安装的 Claude Code 插件自带的 .mcp.json 里的 MCP 服务器。")},
    {"key": "claude-skills", "app": "Claude Code skills", "kind": "skill", "format": "tree",
     "root": "~/.claude/skills",
     "notes": ("Claude-style SKILL.md folders. This project's skills use the same format, so the "
               "text moves across unchanged; any scripts a skill mentions are not copied, because "
               "skills here are text only.",
               "Claude 风格的 SKILL.md 目录。本项目的技能格式相同,文本可原样搬过来;技能里提到的脚本不会被复制,因为这里的技能只有文本。")},
    {"key": "claude-plugin-skills", "app": "Claude Code plugin skills", "kind": "skill", "format": "tree",
     "root": "~/.claude/plugins/marketplaces/*/plugins/*/skills",
     "notes": ("Skills bundled inside installed plugins (found by glob, so the marketplace and "
               "plugin names do not have to be known in advance).",
               "已安装插件里附带的技能(用通配符找,所以不必预先知道市场与插件的名字)。")},

    # --------------------------------------------------------------- WorkBuddy itself
    # Three assets, three shapes on disk, three destinations here:
    #   * a skill      ~/.workbuddy/skills/<name>/SKILL.md              -> a skill
    #   * an expert    <installed plugin>/agents/<name>.md              -> a member
    #   * a connector  ~/.workbuddy/connectors/<workspace>/mcp.json     -> an MCP server
    {"key": "workbuddy-skills", "app": "WorkBuddy skills", "kind": "skill", "format": "tree",
     "root": "~/.workbuddy/skills",
     "notes": ("The skills kept in WorkBuddy's own skills folder. Same SKILL.md format, so the "
               "text moves across unchanged.",
               "WorkBuddy 技能目录里的技能。SKILL.md 格式相同,文本可原样搬过来。")},
    {"key": "workbuddy-plugin-skills", "app": "WorkBuddy plugin skills", "kind": "skill",
     "format": "installed", "sub": "skills", "root": "~/.workbuddy/plugins",
     "notes": ("Skills that the plugins installed in WorkBuddy ship with. Read from its own "
               "record of what is installed, so a plugin kept at several versions is offered once.",
               "WorkBuddy 里已安装插件附带的技能。按它自己的已安装记录读取,所以同一个插件存了多个版本也只出现一次。")},
    {"key": "workbuddy-experts", "app": "WorkBuddy experts", "kind": "expert",
     "format": "installed", "sub": "agents", "glob": "*.md", "root": "~/.workbuddy/plugins",
     "notes": ("Expert packages: each `agents/*.md` is a name, a profession and the prompt, and "
               "becomes a member here. The prompt is kept in the language its author wrote it in, "
               "and the package's picture is not carried over — avatars here are emoji.",
               "专家包:每个 `agents/*.md` 就是名称、专业方向与提示词,在这里变成一个成员。提示词保持作者原来的语言;"
               "专家包里的头像图不会带过来——本应用的头像是 emoji。")},
    {"key": "workbuddy-connectors", "app": "WorkBuddy connectors", "kind": "mcp", "format": "glob",
     "glob": "~/.workbuddy/connectors/*/mcp.json", "json_keys": ["mcpServers"],
     "risks": [("It is authorized inside WorkBuddy, which is where the credential lives — this app "
                "does not have it, so the server will refuse the first call until you supply one",
                "它在 WorkBuddy 里是已授权的,凭据留在那边——本应用没有这份凭据,不补上之前第一次调用就会被拒")],
     "notes": ("The connectors this machine has set up. They are remote services, so the name and "
               "address travel but the authorization does not.",
               "本机已经配置过的连接器。它们是远端服务,所以名称和地址能搬过来,授权不能。")},
    {"key": "workbuddy-connector-catalog", "app": "WorkBuddy connector catalogue", "kind": "mcp",
     "format": "glob", "bulk": True,
     "glob": "~/.workbuddy/connectors-marketplace/connectors/*/mcp.json", "json_keys": ["mcpServers"],
     "risks": [("From WorkBuddy's connector catalogue rather than your own list: it is a name and an "
                "address, with no authorization attached — most of these need an account before "
                "they will answer",
                "来自 WorkBuddy 的连接器目录,而不是你自己配好的那一批:只有名称和地址,不带授权——"
                "其中大多数要先有账号才会应答")],
     "notes": ("Every connector WorkBuddy's catalogue knows about (it is a long list, so it is left "
               "out of a full scan and only read when you ask for it by name).",
               "WorkBuddy 连接器目录里的全部条目(数量很多,所以不参与整体扫描,只有点名看它时才读)。")},
]

BY_KEY = {s["key"]: s for s in SOURCES}


# ------------------------------------------------------------------- helpers
def _platform() -> str:
    import sys
    return "win32" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")


def _mask(values: dict) -> dict:
    return {k: ("••••••" if v else "") for k, v in (values or {}).items()}


def paths_of(src: dict) -> list[Path]:
    """Where this source keeps its file, on this platform. Empty = not applicable here.

    A `tree` source may use a wildcard (`…/marketplaces/*/plugins/*/skills`), because the
    marketplace and plugin names are not known in advance — so it is expanded here rather
    than looked up as a literal directory.

    An `installed` source has no fixed path at all: the directories are whatever the other
    application's plugin record says is installed, joined with the subdirectory this source
    cares about (`skills`, `agents`).
    """
    if src["format"] == "tree":
        raw = str(src["root"])
        if "*" in raw:
            return sorted((Path(p) for p in glob.glob(str(_expand(raw)))), key=str)[:MAX_ITEMS]
        return [_expand(raw)]
    if src["format"] == "installed":
        sub = str(src.get("sub") or "")
        found = []
        for pl in installed_plugins():
            d = (pl["path"] / sub) if sub else pl["path"]
            try:
                if not d.is_symlink() and d.is_dir():
                    found.append(d)
            except OSError:
                continue
        return found[:MAX_ITEMS]
    if src["format"] == "glob":
        return []                                    # resolved by `_files_of` instead
    out = []
    for raw in src.get("paths", {}).get(_platform(), []):
        raw = raw.replace("%APPDATA%", str(_home() / "AppData/Roaming"))
        out.append(_expand(raw))
    return out


def _files_of(src: dict) -> list[Path]:
    """Every readable file this source points at, symlinks and oversized files excluded."""
    if src["format"] == "glob":
        pat = str(_expand(src["glob"]))
        found = sorted(Path(p) for p in glob.glob(pat))[:MAX_ITEMS]
    else:
        found = paths_of(src)
    out = []
    for p in found:
        try:
            if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _dig(data: Any, dotted: str) -> Any:
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _read_json_servers(path: Path, keys: list[str]) -> tuple[list[dict], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return [], [i18n.pick_now(f"{path.name} could not be read as JSON", f"{path.name} 无法按 JSON 读取")]
    for key in keys:
        block = _dig(data, key)
        if isinstance(block, dict) and block:
            try:
                # Re-keyed to the canonical name before parsing: the key may be dotted
                # (`experimental.mcpServers`) or one of the aliases (`context_servers`), and
                # handing the parser anything but `mcpServers` would make it fall back to
                # reading the wrapper itself as a server map.
                servers, notes = parse_mcp_json(json.dumps({"mcpServers": block}))
            except ValueError:
                continue
            return servers, notes
    return [], []


def _read_toml_servers(path: Path, key: str) -> tuple[list[dict], list[str]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError) as e:
        return [], [i18n.pick_now(f"{path.name} could not be read as TOML ({e})", f"{path.name} 无法按 TOML 读取({e})")]
    block = data.get(key)
    if not isinstance(block, dict) or not block:
        return [], []
    # Re-serialised as JSON so one parser handles every shape: a Codex table is only a
    # different notation for the same command/args/env object.
    return parse_mcp_json(json.dumps({"mcpServers": block}))


def risk_notes(server: dict) -> list[str]:
    """What a person should look at before switching this on.

    Heuristics, not a verdict — the point is to put the command in front of somebody rather
    than have it appear in a list as a name and an on/off switch.
    """
    out: list[str] = []
    cmd = Path(str(server.get("command") or "")).name
    if cmd in DOWNLOADERS:
        out.append(i18n.pick_now(f"starts with {cmd}, which downloads and runs a package the first time it is used",
                                 f"以 {cmd} 启动,首次使用时会下载并运行一个包"))
    if any(str(a) in AUTO_YES for a in server.get("args") or []):
        out.append(i18n.pick_now("an argument accepts the download without asking",
                                 "参数里有自动确认,不会询问就直接下载"))
    if any(SHELL_HINT.search(str(a)) for a in server.get("args") or []):
        out.append(i18n.pick_now("an argument contains shell characters; this app starts it without a shell, so what runs here differs from what runs in a terminal",
                                 "参数里含 shell 符号;本应用不经过 shell 启动,所以这里跑的东西和终端里不一样"))
    env = server.get("env") or {}
    if any(SECRET_KEY.search(str(k)) for k in env):
        out.append(i18n.pick_now(f"needs {len(env)} value(s) in its environment, which this app does not carry over — fill them in after importing",
                                 f"需要环境里的 {len(env)} 个值,本应用不会把它们带过来——导入后请自行填写"))
    url = str(server.get("url") or "")
    if url and not url.startswith("https://"):
        out.append(i18n.pick_now("talks over a plain-text connection", "通过明文连接通信"))
    command = str(server.get("command") or "")
    if command.startswith("/") and not command.startswith(str(_home())):
        out.append(i18n.pick_now("runs a program from outside your home directory",
                                 "运行的是你个人目录之外的程序"))
    elif command and not command.startswith("/") and "/" in command:
        out.append(i18n.pick_now(f"starts a relative path ({command}), which resolves against whatever directory this app happens to run in",
                                 f"启动的是相对路径({command}),它取决于本应用恰好从哪个目录启动"))
    return out


def _skill_items(src: dict, store: Any) -> list[dict]:
    from .tools import parse_skill_text, safe_skill_name
    have = set()
    try:
        from .tools import list_skills
        have = {s.name for s in list_skills(store.data_dir / "skills")}
    except Exception:  # noqa: BLE001 — a missing directory just means nothing exists yet
        pass
    out: list[dict] = []
    for root in paths_of(src):
        if not root.is_dir() or root.is_symlink():
            continue
        for path in sorted(root.rglob("SKILL.md"))[:MAX_ITEMS]:
            try:
                rel = path.relative_to(root)
                if len(rel.parts) > MAX_SKILL_DEPTH or path.is_symlink() or path.stat().st_size > MAX_SKILL_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                continue
            skill = parse_skill_text(text, default_name=path.parent.name, path=str(path))
            folder = safe_skill_name(skill.name) or path.parent.name
            out.append({"kind": "skill", "source": src["key"], "app": src["app"],
                        "name": skill.name, "folder": folder, "description": skill.description,
                        "chars": len(skill.body or ""), "path": str(path),
                        "exists": skill.name in have or folder in have})
    return out


# ------------------------------------------------------------- expert packages
# A WorkBuddy expert package is a folder holding `agents/<name>.md` (the prompt, with the
# name and the profession in frontmatter), `avatars/<name>.png`, and sometimes `skills/`.
# It becomes an ordinary member here. Two parts of it cannot travel: the picture, because
# avatars in this app are a single emoji, and the bundled skills, which are offered by the
# skill sources instead of being smuggled in with the expert.
EXPERT_AVATARS: list[tuple[tuple[str, ...], str]] = [
    (("writ", "paper", "academic", "thesis", "essay", "proofread", "editor", "写", "论文", "编辑", "校对"), "✍️"),
    (("image", "design", "poster", "slide", "video", "图", "设计", "海报", "视频"), "🎨"),
    (("code", "program", "software", "engineer", "代码", "开发", "编程"), "💻"),
    (("data", "statist", "analytic", "metric", "数据", "统计", "分析"), "📊"),
    (("financ", "invest", "stock", "account", "金融", "投资", "财务", "股票"), "📈"),
    (("legal", "law", "compliance", "contract", "法律", "合规", "合同"), "⚖️"),
    (("medic", "clinic", "health", "医学", "临床", "健康"), "🩺"),
    (("research", "science", "literature", "文献", "研究", "科研"), "🔬"),
    (("translat", "language", "english", "翻译", "英语", "语言"), "🌐"),
    (("teach", "tutor", "course", "教学", "课程", "教育"), "🎓"),
    (("market", "brand", "sales", "growth", "营销", "品牌", "增长"), "📣"),
    (("product", "project", "manager", "plan", "产品", "项目", "计划", "管理"), "🧭"),
]


def expert_avatar(*texts: str) -> str:
    """The emoji that stands in for an expert package's picture.

    Deterministic and explainable rather than clever: the first group of keywords that
    appears in the expert's name, profession or summary wins, and an expert about none of
    them gets the generic one. Picking a picture is not worth a model call.
    """
    blob = " ".join(t.lower() for t in texts if t)
    for words, emoji in EXPERT_AVATARS:
        if any(w in blob for w in words):
            return emoji
    return "🧑‍🔬"


def _parse_expert(text: str, default_name: str, path: str = "") -> dict:
    """Read one expert package's agent file: frontmatter plus the body, which is the prompt.

    Deliberately not `parse_skill_text`, because the two frontmatters differ in the way that
    matters here: its reader treats an indented line as a nested key to skip, which would
    quietly reduce `displayName: {en, zh}` to nothing and leave the expert named after its
    folder. The scalar reader *is* shared, so `description: >` behaves the same in both.
    """
    out = {"name": default_name, "description": "", "display_en": "", "display_zh": "",
           "profession_en": "", "profession_zh": "", "body": (text or "").strip(), "path": path}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text or "", re.S)
    if not m:
        return out
    out["body"] = m.group(2).strip()
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith((" ", "\t")):          # indented without its parent key
            i += 1
            continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if not rest:                              # a one-level nested map follows
            block: dict[str, str] = {}
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                sub = lines[i].strip()
                i += 1
                if sub:
                    sk, _, sv = sub.partition(":")
                    block[sk.strip()] = sv.strip().strip("'\"")
            field = {"displayName": "display", "profession": "profession"}.get(key)
            if field:
                for lang in ("en", "zh"):
                    if block.get(lang):
                        out[f"{field}_{lang}"] = block[lang]
            continue
        value, i = frontmatter_value(lines, i)
        if key == "name" and value:
            out["name"] = value
        elif key == "description":
            out["description"] = value
    return out


def _slug(value: str) -> str:
    """A stable, language-independent key for an expert package."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")[:60]


def _is_template_body(body: str) -> bool:
    """Is this body nothing but template directives?

    A few of the shipped agents are a one-line include — `{% include "…/prompt.tpl" %}` —
    while the text they stand for lives in a separate template file. Importing one would
    create a member whose entire prompt is that directive: a member that says nothing, while
    the list of what was found claims it is there. They are skipped, and the count is
    reported, so the difference between "not on this machine" and "not a prompt" stays visible.
    """
    rest = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", "", body or "", flags=re.S)
    return not rest.strip()


def expert_label(ex: dict, lang: str, fallback: str = "") -> str:
    """What to call this expert in `lang`: its own name for itself if it has one, else the
    other language's, else whatever it was filed under."""
    if lang == "zh":
        return ex.get("display_zh") or ex.get("display_en") or fallback or ex.get("name") or ""
    return ex.get("display_en") or ex.get("display_zh") or fallback or ex.get("name") or ""


def _expert_items(src: dict, store: Any, notes: list[str] | None = None) -> list[dict]:
    """Expert packages found under this source, as members-to-be. Read-only."""
    pattern = str(src.get("glob") or "*.md")
    agents = store.list_agents()
    out: list[dict] = []
    stubs = 0
    for root in paths_of(src):
        try:
            found = sorted(root.glob(pattern))[:MAX_ITEMS]
        except OSError:
            continue
        for path in found:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EXPERT_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            ex = _parse_expert(text, path.stem, str(path))
            body = (ex["body"] or "").strip()
            if not body:
                continue                          # a package with no prompt is not an expert
            if _is_template_body(body):
                stubs += 1
                continue
            slug = _slug(ex["name"]) or _slug(path.stem)
            if not slug:
                continue
            label = expert_label(ex, i18n.current(), path.stem)
            out.append({"kind": "expert", "source": src["key"], "app": src["app"],
                        "name": slug, "label": label,
                        "role": (ex["profession_zh"] if i18n.current() == "zh" else ex["profession_en"])
                                or ex["profession_en"] or ex["profession_zh"] or "",
                        "description": ex["description"], "chars": len(body),
                        "avatar": expert_avatar(label, ex["profession_en"], ex["profession_zh"],
                                                ex["description"], body[:400]),
                        "bundled_skills": _bundled_skills(path),
                        "path": str(path), "exists": _expert_exists(agents, slug)})
    if stubs and notes is not None:
        notes.append(i18n.pick_now(
            f"{src['app']}: {stubs} agent file(s) are template stubs whose prompt lives in a "
            "separate template file, so there is nothing to import from them",
            f"{src['app']}:有 {stubs} 个 agent 文件只是模板片段(真正的提示词在另一个模板文件里),没有可导入的内容"))
    # One pack ships four experts that all call themselves "PCXX AI Expert". A list where four
    # rows read identically is a list nobody can choose from, so the package name is added
    # where the display name is not unique. It is only the label: the member keeps the name
    # the package chose for itself.
    counts: dict[str, int] = {}
    for it in out:
        counts[it["label"]] = counts.get(it["label"], 0) + 1
    for it in out:
        if counts[it["label"]] > 1:
            it["label"] = f'{it["label"]} ({it["name"]})'
    return out


def _bundled_skills(agent_file: Path) -> int:
    """How many skills the package carries beside the expert. Counted, not imported: the
    skill sources are the ones that install skills."""
    try:
        d = agent_file.parent.parent / "skills"
        if d.is_dir() and not d.is_symlink():
            return len(list(d.rglob("SKILL.md"))[:MAX_ITEMS])
    except OSError:
        pass
    return 0


def _expert_exists(agents: list[dict], slug: str) -> bool:
    """Has this package already been imported?

    Keyed on `origin` alone, which records the package rather than the name: the same expert
    imported twice must be a skip, but a member that merely happens to share a display name
    must not block it — one pack ships four experts that all call themselves the same thing.
    """
    return any((a.get("origin") or "") == f"workbuddy:{slug}" for a in agents)


def unique_member_name(store: Any, wanted: str, slug: str) -> str:
    """A free member name, because `agents.name` is unique.

    Two packages can claim one display name (four shipped experts in a row call themselves
    "PCXX AI Expert"), so the package's own slug is appended instead of letting the second
    import fail on a constraint. Readable, and it says where the member came from.
    """
    taken = {a["name"] for a in store.list_agents()}
    if wanted not in taken:
        return wanted
    alt = f"{wanted} ({slug})"
    n = 2
    while alt in taken:
        alt = f"{wanted} ({slug} {n})"
        n += 1
    return alt


def sources(store: Any) -> list[dict[str, Any]]:
    """Every source, with what was found on this machine. Read-only."""
    out = []
    for src in SOURCES:
        item = {"key": src["key"], "app": src["app"], "kind": src["kind"], "format": src["format"],
                "notes": i18n.pick_now(*src.get("notes", ("", ""))) if src.get("notes") else "",
                "found": False, "files": [], "count": 0, "error": ""}
        try:
            files = _files_of(src)
            roots = [p for p in paths_of(src) if p.exists()]
            item["found"] = bool(files or roots)
            item["files"] = [str(p) for p in files[:5]]
            if src["kind"] == "skill":
                item["count"] = len(_skill_items(src, store))
            elif src["kind"] == "expert":
                item["count"] = len(_expert_items(src, store))
            else:
                # Counted by name, so "N found" is the number of rows the list will show rather
                # than the number of definitions across its files (one connector can be in two
                # of them). A single unreadable file does not blank the source either — the
                # count is what parsed, and if nothing parsed at all, that file's reason is
                # what the row says.
                names: set[str] = set()
                first_error = ""
                for f in files:
                    try:
                        servers, _ = _read_at(src, f)
                    except ValueError as e:
                        first_error = first_error or str(e)
                        continue
                    names.update(s["name"] for s in servers)
                item["count"] = len(names)
                if not names and first_error:
                    item["error"] = first_error[:200]
        except Exception as e:  # noqa: BLE001 — a source that cannot be read is reported, not fatal
            item["error"] = f"{type(e).__name__}: {e}"[:200]
        out.append(item)
    return out


def _read_at(src: dict, path: Path) -> tuple[list[dict], list[str]]:
    if src["format"] == "toml":
        return _read_toml_servers(path, src.get("toml_key", "mcp_servers"))
    return _read_json_servers(path, src.get("json_keys") or ["mcpServers"])


def scan(store: Any, only: list[str] | None = None) -> dict:
    """Collect the importable items from the chosen sources (all of them by default).

    MCP servers come back with `env` / `headers` masked: the import step re-reads the file
    server-side, so the browser never has to hold a credential in order to move it.

    A source marked `bulk` is skipped unless it is named explicitly. One of them is a
    catalogue of every connector an application knows about — useful to search, wrong to
    pour into the default list, where it would bury the handful of things that are actually
    installed on this machine.
    """
    if only:
        wanted = [s for s in SOURCES if s["key"] in only]
    else:
        wanted = [s for s in SOURCES if not s.get("bulk")]
    items: list[dict] = []
    notes: list[str] = []
    for src in wanted:
        if src["kind"] == "skill":
            items.extend(_skill_items(src, store))
            continue
        if src["kind"] == "expert":
            items.extend(_expert_items(src, store, notes))
            continue
        # Anything this source says about *all* of its servers goes first: on a remote
        # connector it is the important part (the authorization stayed behind), and a
        # per-server heuristic has nothing to say about it.
        src_risks = [i18n.pick_now(en, zh) for en, zh in (src.get("risks") or [])]
        for path in _files_of(src):
            try:
                servers, warns = _read_at(src, path)
            except ValueError as e:
                notes.append(f"{src['app']}: {e}")
                continue
            for s in servers:
                items.append({"kind": "mcp", "source": src["key"], "app": src["app"],
                              "name": s["name"], "command": s["command"], "args": s["args"],
                              "url": s["url"], "transport": s["transport"],
                              "env_keys": sorted(s["env"]), "header_keys": sorted(s["headers"]),
                              "env": _mask(s["env"]), "headers": _mask(s["headers"]),
                              "description": s["description"], "enabled": s["enabled"],
                              "path": str(path), "risks": src_risks + risk_notes(s)})
            for w in warns:
                notes.append(f"{src['app']}: {w}")
    have_mcp = {m["name"] for m in store.list_mcp()}
    for it in items:
        if it["kind"] == "mcp":
            it["exists"] = it["name"] in have_mcp
    # One application can define the same thing twice: WorkBuddy keeps a connector record per
    # workspace, and two of them list the same connector. The list is keyed by name — ticking a
    # row ticks that name — so a duplicate renders as two rows that move together, and only one
    # of them could ever be imported. Keep the first and say what was folded away. Two *different*
    # applications offering the same name is a different thing and stays visible: those are
    # different definitions, and the import is per source.
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    folded: list[str] = []
    for it in items:
        key = (it["source"], it["kind"], it["name"])
        if key in seen:
            folded.append(it["name"])
            continue
        seen.add(key)
        unique.append(it)
    if folded:
        shown = ", ".join(sorted(set(folded))[:3])
        notes.append(i18n.pick_now(
            f"{len(folded)} entry/entries are listed once because the same application defines "
            f"them in more than one file: {shown}",
            f"有 {len(folded)} 条同名条目只列一次,因为它们所属的同一个应用在多个文件里定义了它们:{shown}"))
    return {"items": unique[:MAX_ITEMS], "notes": notes[:20],
            "truncated": len(unique) > MAX_ITEMS}


def import_mcp(store: Any, source: str, names: list[str]) -> dict:
    """Import the named servers from one source. Re-reads the config, so nothing has to be
    trusted back from the client — and so a credential never leaves this machine.

    Everything lands **disabled**, regardless of what the other application had: that app
    may have been running this command for months under its own settings, while here it is
    a definition nobody has read yet. Enabling is a deliberate second step in the MCP page.
    """
    from .mcp_client import import_servers
    src = BY_KEY.get(source)
    if not src or src["kind"] != "mcp":
        raise ValueError(i18n.pick_now("That is not an MCP source", "这不是一个 MCP 来源"))
    servers: list[dict] = []
    for path in _files_of(src):
        got, _ = _read_at(src, path)
        servers.extend(got)
    if not servers:
        raise ValueError(i18n.pick_now("Nothing was found in that application's configuration",
                                       "在那个应用的配置里没有找到任何服务器"))
    for s in servers:
        s["enabled"] = False
    return import_servers(store, servers, list(names))


def import_skills(store: Any, source: str, names: list[str]) -> dict:
    """Import the named skills as text. Existing names are skipped, never overwritten."""
    from .tools import write_skill
    src = BY_KEY.get(source)
    if not src or src["kind"] != "skill":
        raise ValueError(i18n.pick_now("That is not a skill source", "这不是一个技能来源"))
    added, skipped = [], []
    for item in _skill_items(src, store):
        if item["name"] not in names:
            continue
        if item["exists"]:
            skipped.append(item["name"])
            continue
        try:
            text = Path(item["path"]).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            skipped.append(item["name"])
            continue
        from .tools import parse_skill_text
        skill = parse_skill_text(text, default_name=item["folder"], path=item["path"])
        if not (skill.body or "").strip():
            skipped.append(item["name"])
            continue
        write_skill(store.data_dir / "skills", skill.name, skill.description, skill.body, "member")
        added.append(skill.name)
    return {"added": added, "skipped": skipped}


def import_experts(store: Any, source: str, names: list[str]) -> dict:
    """Import the named expert packages as members. Existing members are skipped, never touched.

    The member is named and described in the language of this request — that is the name the
    picker will show — while the prompt is copied exactly as the package wrote it. Translating
    an expert's instructions would be inventing an expert, and the package is the only thing
    here that knows what it is supposed to say.

    `origin` records the package a member came from (`workbuddy:<slug>`). That is what makes a
    second import a skip instead of a duplicate, and it stays true if the user renames them.
    """
    src = BY_KEY.get(source)
    if not src or src["kind"] != "expert":
        raise ValueError(i18n.pick_now("That is not an expert source", "这不是一个专家来源"))
    lang = i18n.current()
    added: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    for item in _expert_items(src, store):
        if item["name"] not in names:
            continue
        label = item["label"] or item["name"]
        if item["exists"]:
            skipped.append(label)
            continue
        try:
            text = Path(item["path"]).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            skipped.append(label)
            continue
        ex = _parse_expert(text, Path(item["path"]).stem, item["path"])
        body = (ex["body"] or "").strip()
        if not body:
            skipped.append(label)
            continue
        wanted = expert_label(ex, lang, Path(item["path"]).stem)
        name = unique_member_name(store, wanted, item["name"])
        if name != wanted:
            notes.append(i18n.pick_now(
                f'A member named "{wanted}" is already here, so this one is called "{name}" — '
                "rename it whenever you like",
                f"已有名为「{wanted}」的成员,所以这一位叫「{name}」——随时可以改名"))
        role = ((ex["profession_zh"] if lang == "zh" else ex["profession_en"])
                or ex["profession_en"] or ex["profession_zh"] or "")
        store.create_agent(name, item["avatar"], role, body, None, [], [],
                           origin=f"workbuddy:{item['name']}")
        added.append(name)
        if item["bundled_skills"]:
            notes.append(i18n.pick_now(
                f"{name} ships {item['bundled_skills']} skill(s) of its own — import those from the "
                "skill sources, so you can see what they contain before installing them",
                f"「{name}」自带了 {item['bundled_skills']} 个技能——请到技能来源里单独导入,这样你能先看清内容再装"))
    return {"added": added, "skipped": skipped, "notes": notes}
