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

# Bounds: a config file is a few kilobytes, and a skill is a document. Anything larger is
# not what this is looking for, and a home directory is not something to walk unbounded.
MAX_FILE_BYTES = 512 * 1024
MAX_SKILL_BYTES = 256 * 1024
MAX_ITEMS = 200
MAX_SKILL_DEPTH = 4

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
    """
    if src["format"] == "tree":
        raw = str(src["root"])
        if "*" in raw:
            return sorted((Path(p) for p in glob.glob(str(_expand(raw)))), key=str)[:MAX_ITEMS]
        return [_expand(raw)]
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
            else:
                total = 0
                for f in files:
                    servers, _ = _read_at(src, f)
                    total += len(servers)
                item["count"] = total
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
    """
    wanted = [s for s in SOURCES if not only or s["key"] in only]
    items: list[dict] = []
    notes: list[str] = []
    for src in wanted:
        if src["kind"] == "skill":
            items.extend(_skill_items(src, store))
            continue
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
                              "path": str(path), "risks": risk_notes(s)})
            for w in warns:
                notes.append(f"{src['app']}: {w}")
    have_mcp = {m["name"] for m in store.list_mcp()}
    for it in items:
        if it["kind"] == "mcp":
            it["exists"] = it["name"] in have_mcp
    return {"items": items[:MAX_ITEMS], "notes": notes[:20],
            "truncated": len(items) > MAX_ITEMS}


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
