"""Template gallery: a first-party template catalog bundled with the program, plus one-click apply.

Why it is built this way (compared with the earlier "example library"):
  * it used to require cloning a third-party repository and filling in a local path for static
    extraction — too roundabout for people, and unsuitable on confidential machines. Templates
    now ship with the program and **work as soon as you open it**: no cloning, no path to fill
    in, no network;
  * the content is entirely this program's own original text and contains no files or data from
    any third-party project, so shipping it with the program creates no third-party license
    obligations (see LICENSE and THIRD_PARTY_NOTICES.md);
  * **single source of truth**: team templates, member roles, skills, prompts and MCP each have
    exactly one built-in definition (presets.TEMPLATES / presets.AGENT_PRESETS /
    tools.EXAMPLE_SKILLS / presets.SEED_PROMPTS / gallery.MCP_TEMPLATES). This module only
    normalizes and applies them and never copies the content, which avoids the notoriously
    hard-to-debug "two definitions slowly drifting apart";
  * **extension point**: `templates/*.json` in the data directory can hold a team's own
    templates, merged into the catalog after schema validation (see `_load_custom`). Entries
    that fail validation are dropped with the reason stated in the UI, never ignored silently;
  * **security boundary**: custom templates may only be of the plain-text kinds
    (team / agent / skill / prompt); **mcp is not accepted** — that would let a JSON file decide
    which commands this machine runs. MCP can only be added from the built-in list, and is
    always imported in a disabled state.

This module does not go online, reads no path outside the data directory, and executes no
code; applying a template only writes text into the local database and the skills directory
(plus one MCP entry that is disabled by default).
"""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import i18n
from .presets import (
    AGENT_PRESETS,
    SEED_AGENTS,
    SEED_PROMPTS,
    TEMPLATES,
    builtin_for,
    builtin_names,
    display_name,
    prompt_for,
    prompt_titles,
)
from .store import Store
from .templates import ensure_agent, group_view
from .tools import (
    EXAMPLE_SKILLS,
    canonical_skill_name,
    display_skill_name,
    list_skills,
    skill_for,
    skill_names,
    write_skill,
)

CATALOG_VERSION = "2.2.0"      # semantic version of the catalog contents: bump it when templates are added or changed
SCHEMA_VERSION = 1            # file format version of custom templates
CUSTOM_DIRNAME = "templates"  # subdirectory of the data directory holding custom templates
CUSTOM_MAX_BYTES = 512 * 1024
CUSTOM_MAX_ITEMS = 200
PLACEHOLDER_DIR = "/path/to/allowed/dir"

# the MCP usage list (single source of truth; api_ext's /api/mcp/templates reads it too).
# These are only "prefilled forms": you have to check the command and arguments yourself,
# and everything is imported in a disabled state.
#
# A server is reached one of two ways, and a template says which by what it fills in:
#   * `command` + `args` — stdio: this app starts that program;
#   * `url` (+ optional `transport` and `headers`) — the program is already running somewhere,
#     on this machine or on the internet. `headers` may hold a fixed value (a client id, say)
#     or an empty one, which means "you paste this in": an empty value is listed as something
#     to fill in, exactly like `env_keys`. Anything whose *name* looks like a credential is
#     moved to the keychain on the way in (`secrets.SENSITIVE_NAME`), so a Bearer token never
#     lands in the database in the clear.
# Signing in to a server that only speaks OAuth cannot happen inside a connection attempt: it needs a
# person in a browser, and the very process this app started is the one that has to serve the
# callback. So the route is `mcp-remote`, and the instruction worth spelling out is the terminal one.
# Written once, because three entries need it and three slightly different paragraphs would be three
# different pieces of advice about the same thing.
_OAUTH_EN = (" Its sign-in is OAuth, which this app cannot drive for you: the first Test will very "
             "likely time out while the browser page is still open. The token is then cached in "
             "~/.mcp-auth, so pressing the same button again connects. If it keeps failing, run the "
             "command above once in a terminal, finish the sign-in there, and test again here. "
             "Requires Node.js.")
_OAUTH_ZH = (" 它用 OAuth 登录,而这个流程本程序替不了你:第一次点「测试连接」很可能在浏览器页面还开着的时候就超时。"
             "令牌会缓存在 ~/.mcp-auth,所以再点一次同一个按钮就通了。要是一直不行,就在终端里把上面那条命令跑一次,"
             "在那边完成登录,再回来测试。需要 Node.js。")

MCP_TEMPLATES: list[dict] = [
    # Pre-filled forms, not a one-click install: the command and arguments are yours to
    # check, and anything imported this way starts disabled. English is the canonical
    # value; `<field>_zh` carries the Chinese wording and i18n.localize() picks one.
    {"key": "filesystem", "name": "Filesystem", "name_zh": "文件系统",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", PLACEHOLDER_DIR],
     "env_keys": [],
     "note": "Let members read and write files in one directory. Replace the last argument with the directory they may access. Requires Node.js.",
     "note_zh": "让成员读写指定目录里的文件。最后一个参数换成你允许访问的目录。需要 Node.js。"},
    {"key": "fetch", "name": "Web fetch", "name_zh": "网页抓取",
     "command": "uvx", "args": ["mcp-server-fetch"],
     "env_keys": [],
     "note": "Fetch a web page and turn it into text so members can read what is behind a link. Requires uv (uvx).",
     "note_zh": "抓取网页并转成文本,成员可以读链接内容。需要 uv(uvx)。"},
    {"key": "memory", "name": "Knowledge-graph memory", "name_zh": "知识图谱记忆",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"],
     "env_keys": [],
     "note": "The official MCP memory server — a different thing from the built-in Memory feature of this app. Requires Node.js.",
     "note_zh": "MCP 官方的记忆服务器(与本程序自带的「记忆」是两套东西)。需要 Node.js。"},
    {"key": "sequential-thinking", "name": "Sequential thinking", "name_zh": "顺序思考",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
     "env_keys": [],
     "note": "Helps the model work through a complex problem step by step. Requires Node.js.",
     "note_zh": "帮助模型把复杂问题分步思考。需要 Node.js。"},
    {"key": "playwright", "name": "Browser (Playwright)", "name_zh": "浏览器(Playwright)",
     "command": "npx", "args": ["@playwright/mcp@latest"],
     "env_keys": [],
     "note": "Let members drive a real browser: open pages, click, fill things in. This is an "
             "execution-class tool, so by default you are asked before every call. The browser may "
             "need downloading on first use; check the Playwright MCP project docs for the exact "
             "arguments. Requires Node.js.",
     "note_zh": "让成员驱动真实的浏览器:打开网页、点击、填写。属于执行类工具,默认每次调用前都会问你。"
                "首次使用可能要下载浏览器,具体参数以 Playwright MCP 项目的文档为准。需要 Node.js。"},
    {"key": "time", "name": "Time and time zones", "name_zh": "时间与时区",
     "command": "uvx", "args": ["mcp-server-time"],
     "env_keys": [],
     "note": "Look up and convert the time in other places. Requires uv (uvx).",
     "note_zh": "查询和换算各地时间。需要 uv(uvx)。"},
    {"key": "git", "name": "Git", "name_zh": "Git",
     "command": "uvx", "args": ["mcp-server-git", "--repository", "/path/to/repo"],
     "env_keys": [],
     "note": "Read the history and diffs of a Git repository. Replace the trailing path with your repository. Requires uv (uvx).",
     "note_zh": "读取指定 Git 仓库的历史和差异。把最后的路径换成你的仓库。需要 uv(uvx)。"},

    # ------------------------------------------------------------------ video making
    # Three ways to make video that live outside this app. Two of them are servers this app
    # talks to over HTTP, which is why the URL form above exists at all.
    {"key": "voicebox", "name": "Voicebox (voice on this machine)",
     "name_zh": "Voicebox(本机语音)",
     "url": "http://127.0.0.1:17493/mcp", "transport": "http",
     # A fixed header, and deliberately not treated as a secret: Voicebox uses it only to
     # remember which voice this client speaks with. Nothing is installed by this entry —
     # Voicebox is a separate desktop app, and it has to be running for the URL to answer.
     "headers": {"X-Voicebox-Client-Id": "team-agent"},
     "env_keys": [],
     "note": "A desktop voice studio on this machine: speak text in a voice you cloned, and "
             "transcribe audio, without anything leaving the computer. Install and start Voicebox "
             "first — this entry only points at it (its MCP server listens on 127.0.0.1:17493). "
             "Reading the audio out loud happens on your speakers, so a member that speaks is "
             "audible to whoever is in the room.",
     "note_zh": "本机的语音工作室:用你自己克隆的音色念出文字,并在本机转写音频,内容不出这台电脑。"
                "需要你先安装并启动 Voicebox——这条只是指向它(它的 MCP 服务监听 127.0.0.1:17493)。"
                "朗读是从你的音箱出来的,所以成员一开口,屋里的人都听得到。"},
    {"key": "heygen", "name": "HeyGen (avatar video)", "name_zh": "HeyGen(数字人视频)",
     "command": "uvx", "args": ["heygen-mcp"], "env_keys": ["HEYGEN_API_KEY"],
     "note": "Make a talking-avatar video from a script, using your own HeyGen account: pick an "
             "avatar and a voice, submit the script, then poll until the video is ready. The key "
             "comes from your HeyGen account settings; rendering happens on their servers and is "
             "billed to that account, so check the script before submitting rather than after. "
             "Requires uv (uvx).",
     "note_zh": "用你自己 HeyGen 账号里的数字人和音色,把一段稿子做成口播视频:选头像与音色、提交、再轮询到出片。"
                "密钥在 HeyGen 账号设置里取;渲染在他们的服务器上完成并按该账号计费,所以请在提交前确认稿子,"
                "而不是提交后再改。需要 uv(uvx)。"},
    {"key": "chatcut", "name": "ChatCut (edit video by describing it)",
     "name_zh": "ChatCut(用描述剪视频)",
     "url": "https://api.chatcut.io/api/external-mcp/mcp", "transport": "http",
     # Empty value = the user pastes it. The name alone puts it in the keychain.
     "headers": {"Authorization": ""},
     "env_keys": [],
     "note": "Cut and shape a real multi-track timeline by describing the edit, and generate clips, "
             "voice-over, music, captions and motion graphics inside the same project. This is their "
             "hosted server, so the Authorization header needs a Bearer token from *your* ChatCut "
             "account — their agent-plugin guide walks through the sign-in that produces one, and "
             "the token is short-lived (about an hour), so a 401 later usually means refreshing it "
             "rather than a wrong key. Watch the account it bills: it is not this app's.",
     "note_zh": "用文字描述剪辑,让它去改真实的轨道时间线,并在同一个项目里生成片段、旁白、音乐、字幕与动态图形。"
                "这是它的托管服务,所以 Authorization 头要填**你自己** ChatCut 账号的 Bearer token——"
                "它那份接入说明里有换取 token 的登录流程;token 有效期不长(约 1 小时),之后报 401 通常是该换一个,"
                "而不是填错了。注意计费记在那个账号上,不是本程序。"},
    {"key": "davinci-resolve", "name": "DaVinci Resolve (cut on a real timeline)",
     "name_zh": "达芬奇(DaVinci Resolve 剪辑)",
     "command": "uvx",
     # Two pins, each one a wall somebody would otherwise walk into: Resolve's own scripting module
     # imports `imp`, which Python removed in 3.12, so the interpreter has to be 3.11; and this
     # package still uses the MCP v1 API (`mcp.server.fastmcp`), which the v2 SDK dropped — without
     # the pin the server dies at import with a message about FastMCP being renamed.
     "args": ["--python", "3.11", "--with", "mcp<2", "davinci-resolve-mcp"],
     "env_keys": [],
     "note": "Edit on the real timeline: lay out a cut list, place clips, add Fusion effects and "
             "titles, grade, and queue renders, through Resolve's own scripting API. Three "
             "prerequisites, and it will not answer without them: DaVinci Resolve Studio (the free "
             "version exposes no scripting API at all), Preferences → System → General → External "
             "scripting using → Local, and Resolve itself open before you press test — the server "
             "exits immediately when it cannot reach Resolve. Requires uv (uvx).",
     "note_zh": "在真实时间线上剪辑:按剪辑清单搭结构、放片段、加 Fusion 特效与字幕、调色、排队渲染,"
                "走的是 Resolve 自己的脚本 API。三个前提缺一不可:DaVinci Resolve Studio"
                "(免费版根本没有对外脚本 API)、偏好设置 → 系统 → 常规 → 外部脚本 设为 Local、"
                "以及按测试之前 Resolve 已经开着——连不上它,这个服务器会立刻退出。需要 uv(uvx)。"},

    # --------------------------------------------------- the everyday services
    # Every endpoint below was contacted before this list was written. That matters here more than
    # anywhere else in this file: a wrong address is not a broken template, it is a template that
    # looks fine and cannot ever work, and the reader finds out one opaque 404 at a time.
    {"key": "github", "name": "GitHub", "name_zh": "GitHub",
     "url": "https://api.githubcopilot.com/mcp/", "transport": "http",
     "headers": {"Authorization": ""}, "env_keys": [],
     "note": "Repositories, issues, pull requests and CI runs, through GitHub's own hosted server — "
             "nothing to install. The header takes a personal access token written as `Bearer "
             "ghp_…` (or `github_pat_…` for a fine-grained one). The scopes on that token are "
             "exactly what a member can then do, so a read-only token is the sensible first try; "
             "GitHub's own documentation also lists Copilot access as a prerequisite for parts of "
             "the server. Their Docker image is the local alternative, but that is a second thing "
             "to install.",
     "note_zh": "仓库、issue、Pull Request、CI 运行情况,走 GitHub 自己的托管服务器,不需要在本机装东西。"
                "请求头填一个个人访问令牌(PAT),写成 `Bearer ghp_…`(细粒度令牌是 `github_pat_…`)。"
                "令牌上的权限范围就是成员之后能做的事,所以第一次建议只给只读权限;"
                "GitHub 自己的文档还把「有 Copilot 访问权」列为这个服务器的部分能力的前提。"
                "用他们的 Docker 镜像也可以在本机跑,但那是要多装一样东西。"},
    {"key": "context7", "name": "Context7 (current library docs)", "name_zh": "Context7(库的当前文档)",
     "url": "https://mcp.context7.com/mcp", "transport": "http",
     # No header in the template on purpose: this one really does connect without a token, and an
     # entry that lists a header as "to be filled in" would be asking for something it does not need.
     # The note says where a key would go if the shared rate limit becomes a problem.
     "env_keys": [],
     "note": "Up-to-date documentation for a library, so a member stops inventing an API from its "
             "training cutoff: it resolves a package name and returns current docs and code "
             "examples. It needs no account and no token — leave it exactly as it is. If the shared "
             "rate limit becomes a problem, take a key from context7.com/dashboard and add a request "
             "header named Authorization with the value `Bearer <key>`.",
     "note_zh": "某个库的当下文档,让成员不再凭训练截止时间里的记忆编 API:它先把包名解析成 "
                "Context7 的库 id,再把当前文档和代码示例取回来。它不需要账号也不需要令牌,照原样导入就能用。"
                "如果觉得公用速率上限不够,到 context7.com/dashboard 取一个 key,"
                "再自己加一条名为 Authorization、值为 `Bearer <key>` 的请求头。"},
    {"key": "supabase", "name": "Supabase (database and logs)", "name_zh": "Supabase(数据库与日志)",
     "url": "https://mcp.supabase.com/mcp?read_only=true", "transport": "http",
     "headers": {"Authorization": ""}, "env_keys": [],
     "note": "Query a project's database, read logs and advisors, list migrations and edge "
             "functions. The address above asks for read-only, which runs every query as a "
             "read-only Postgres user — keep it, because this server can also write: execute_sql "
             "and apply_migration change real data. Add `&project_ref=<ref>` to scope it to a "
             "single project, and paste an access token as `Bearer sbp_…`. It also offers an OAuth "
             "sign-in, which this app cannot drive, so the token is the route here.",
     "note_zh": "查项目数据库、读日志与优化建议、看迁移记录与 Edge Functions。上面那个地址带 `read_only=true`,"
                "所有查询都以只读 Postgres 用户执行——**建议留着**,因为这个服务器也能写:`execute_sql` 与 "
                "`apply_migration` 会改真实数据。加上 `&project_ref=<ref>` 就只限于某一个项目;"
                "访问令牌填成 `Bearer sbp_…`。它也提供 OAuth 登录,但那个流程本程序走不了,所以这里用令牌。"},
    {"key": "vercel", "name": "Vercel (deployments and logs)", "name_zh": "Vercel(部署与日志)",
     "command": "npx", "args": ["-y", "mcp-remote", "https://mcp.vercel.com"], "env_keys": [],
     "note": "Answer \"why did that deploy fail\" without opening the dashboard: read build and "
             "runtime logs, list projects, teams and deployments, and search the docs. It is "
             "read-only — it cannot deploy or change anything." + _OAUTH_EN +
             " Vercel also keeps an allowlist of approved clients while this is in beta, so a "
             "refusal that names the client is theirs rather than something wrong on this machine.",
     "note_zh": "不开控制台就能回答「这次部署为什么失败」:读构建日志与运行日志、列出项目/团队/部署、搜官方文档。"
                "它是只读的——不能部署,也改不了任何东西。" + _OAUTH_ZH +
                "另外这个服务还在 beta,维护着一份「允许的客户端」名单;如果被拒时点名了客户端,那是他们的名单问题,"
                "不是这台机器坏了。"},
    {"key": "sentry", "name": "Sentry (errors and traces)", "name_zh": "Sentry(报错与链路)",
     "url": "https://mcp.sentry.dev/mcp", "transport": "http",
     "headers": {"Authorization": ""}, "env_keys": [],
     "note": "From an error report to the code behind it: search issues and events, read stack "
             "traces, traces and releases. The header takes a Sentry user auth token written as "
             "`Sentry-Bearer sntrys_…` — deliberately not a plain `Bearer`, which Sentry reserves "
             "for the OAuth tokens this app cannot obtain. (They also publish a local stdio server "
             "that reads `SENTRY_ACCESS_TOKEN`, but its natural-language search needs its own model "
             "key, so the hosted one is the shorter path.)",
     "note_zh": "从一条报错走到它背后的代码:检索 issue 与事件、读堆栈、看 trace 与 release。"
                "请求头填 Sentry 的用户令牌,写成 `Sentry-Bearer sntrys_…`——**注意不是普通的 `Bearer`**,"
                "那个前缀被 Sentry 留给本程序拿不到的 OAuth 令牌了。"
                "(他们也有一个本机 stdio 服务器,读 `SENTRY_ACCESS_TOKEN`;但它的自然语言检索要另配一个模型密钥,"
                "所以托管版更省事。)"},
    {"key": "linear", "name": "Linear (issues and projects)", "name_zh": "Linear(issue 与项目)",
     "command": "npx", "args": ["-y", "mcp-remote", "https://mcp.linear.app/mcp/readonly"],
     "env_keys": [],
     "note": "Find, read and write issues, projects, cycles and comments. The address above ends in "
             "`/readonly`, which only ever exposes the read tools — drop that suffix to let a member "
             "create and update issues, and expect to sign in again when you do." + _OAUTH_EN,
     "note_zh": "查找、读取与写入 issue、项目、迭代与评论。上面那个地址以 `/readonly` 结尾,只会暴露只读工具;"
                "想去掉这个限制让成员能建 issue、改 issue,就把后缀去掉——那时通常要重新登录一次。" + _OAUTH_ZH},
    {"key": "figma", "name": "Figma (the design itself)", "name_zh": "Figma(直接给设计稿)",
     "url": "http://127.0.0.1:3845/mcp", "transport": "http", "env_keys": [],
     "note": "Hand a member the design rather than a screenshot: the selected frame's layout, "
             "components, variables and styles, which is what makes \"build this screen\" come out "
             "matching instead of guessed. This is Figma's *local* server, so nothing leaves the "
             "machine and no account is involved here — but it only exists while the Figma desktop "
             "app is running with it switched on (in Figma: Preferences → Enable local MCP server, "
             "or the MCP section of the Dev Mode inspect panel). Needs a Dev or Full seat on a paid "
             "plan. Figma's hosted server at https://mcp.figma.com/mcp is the other way, and it is "
             "OAuth, so this app cannot drive its sign-in.",
     "note_zh": "把设计稿本身交给成员,而不是一张截图:选中画板的布局、组件、变量与样式,"
                "这样「照这个做一屏」才会做得像,而不是靠猜。这是 Figma 的**本机**服务器,内容不出这台机器,"
                "也不需要账号——但它只在 Figma 桌面版**正在运行且已开启**时存在"
                "(在 Figma 里:偏好设置 → 启用本地 MCP 服务器,或开发模式检查面板里的 MCP 一栏)。"
                "需要付费版图上的 Dev 或 Full 席位。Figma 也有托管服务器 https://mcp.figma.com/mcp,"
                "那个走 OAuth,本程序走不了它的登录流程。"},
]

HOOK_TEMPLATES: list[dict] = [
    # Metadata only. The source of each hook is a real file under app/data/hook-templates/, not a
    # string in this module: it is Python that has to *run* in a subprocess, so it deserves to be
    # checked by the same tools as everything else — and nesting it in a literal here had silently
    # turned every `\n` inside it into a line break, which the test that loads each template
    # through the runner is what caught.
    #
    # They are starting points, not finished policies: short, readable, and meant to be edited.
    # English is the canonical text written to disk (the panel shows it that way); `<field>_zh` is
    # the Chinese wording for the gallery itself.
    {"key": "house-style", "name": "House style", "name_zh": "本群规范",
     "icon": "📐", "events": ["pre_prompt"], "timeout_ms": 500,
     "summary": "Append this group's own rules to every prompt a member sends — the same lines for a plain reply, a task, or the consolidation.",
     "summary_zh": "在成员每次调用模型前,把这群的规范追加进提示词——普通回复、任务、整合一视同仁。"},
    {"key": "hold-back-secrets", "name": "Keep secrets out of the transcript",
     "name_zh": "别把密钥写进对话", "icon": "🔒", "events": ["post_reply"], "timeout_ms": 1000,
     "summary": "Hold back a reply whose text quotes a key, a password or an ID number, before it becomes part of the record that exports and backups carry.",
     "summary_zh": "回复里出现密钥、口令或身份证号时,在它变成对话记录(会被导出与备份带上)之前拦下来。"},
    {"key": "no-pii-out", "name": "Check before a message leaves the machine",
     "name_zh": "外发前检查", "icon": "📤", "events": ["before_send"], "timeout_ms": 1000,
     "summary": "Before a reply goes out to a chat channel, hold it back if it carries a phone number or an ID number.",
     "summary_zh": "回复要发到站外聊天通道前,若带手机号或身份证号则拦住。"},
    {"key": "audit-trail", "name": "Audit trail", "name_zh": "审计留痕",
     "icon": "📋", "events": ["round.end", "tool.called"], "timeout_ms": 1500,
     "summary": "Append one line per finished round and per tool call to audit.jsonl, in this hook's own folder. The starting point for a log you can actually read later.",
     "summary_zh": "每轮结束与每次工具调用各追加一行到 audit.jsonl(在这个钩子自己的目录里)。想事后查账,从这里起步。"},
]

_HOOK_DIR = Path(__file__).parent / "data" / "hook-templates"


@lru_cache(maxsize=None)
def hook_code(key: str) -> str:
    """The source of a built-in hook template, or "" when the file is not there."""
    try:
        return (_HOOK_DIR / f"{key}.py").read_text(encoding="utf-8")
    except OSError:
        return ""


_HOOK_ALIASES: dict[str, dict] = {}
for _h in HOOK_TEMPLATES:
    for _name in (_h.get("key"), _h.get("name"), _h.get("name_zh")):
        if _name:
            _HOOK_ALIASES.setdefault(_name, _h)


def hook_names(name: str | None) -> list[str]:
    """Every spelling a template-installed hook id might appear as."""
    entry = _HOOK_ALIASES.get(name or "")
    if not entry:
        return [name] if name else []
    return [n for n in (entry.get("key"), entry.get("name"), entry.get("name_zh")) if n]


CATEGORIES: list[dict] = [
    {"id": "team", "label": "Teams", "label_zh": "团队",
     "hint": "One click builds a group chat: the members, the host, the group rules, and the skills they need are all installed together.",
     "hint_zh": "一键建成群聊:成员、群主、群规则、依赖的技能一次装好。"},
    {"id": "agent", "label": "Roles", "label_zh": "角色",
     "hint": "A single member: create it on its own, or pull it straight into a group you already have.",
     "hint_zh": "单个成员:可以单独创建,也可以直接拉进已有的群。"},
    {"id": "skill", "label": "Skills", "label_zh": "技能",
     "hint": "Plain-text methods and conventions written for the model to read; nothing is executed.",
     "hint_zh": "写给模型看的纯文本方法与规范,不会执行任何代码。"},
    {"id": "prompt", "label": "Prompts", "label_zh": "提示词",
     "hint": "Prompt snippets: keep them in the library, then attach one to a group or make it global.",
     "hint_zh": "常用提示词:存进提示词库后,可挂给某个群或设为全局。"},
    {"id": "hook", "label": "Hooks", "label_zh": "钩子",
     "hint": "Your own code at a few fixed points of a round: append to a prompt, object to a tool call, or stop a reply from being kept. Installed switched off — read it, then turn it on.",
     "hint_zh": "把你自己的一小段代码挂在群聊的几个固定节点上:往提示词里追加内容、否决工具调用、或拦下不该留下的回复。装进来是关闭状态——先读一遍,再打开。"},
    {"id": "mcp", "label": "MCP", "label_zh": "MCP",
     "hint": "How to wire in an external tool: it is imported disabled, so check the command and fill in the keys before you enable it.",
     "hint_zh": "外部工具的接入写法:导入后一律停用,核对命令并填好密钥再启用。"},
]

# Both spellings of a built-in MCP server name resolve to the same entry, so a server
# added before the content was translated still counts as "already added".
MCP_ALIASES: dict[str, dict] = {}
for _m in MCP_TEMPLATES:
    for _name in (_m.get("key"), _m.get("name"), _m.get("name_zh")):
        if _name:
            MCP_ALIASES.setdefault(_name, _m)


def mcp_display_name(name: str | None, lang: str) -> str:
    """A built-in MCP server name as shown in `lang` (either spelling resolves)."""
    entry = MCP_ALIASES.get(name or "")
    if not entry:
        return name or ""
    return (entry.get("name_zh") if lang == "zh" else entry["name"]) or (name or "")


def mcp_names(name: str | None) -> list[str]:
    """Every spelling a stored MCP server name might have been written in."""
    entry = MCP_ALIASES.get(name or "")
    if not entry:
        return [name] if name else []
    return [n for n in (entry.get("name"), entry.get("name_zh")) if n]


KINDS: tuple[str, ...] = tuple(c["id"] for c in CATEGORIES)
CUSTOM_KINDS: tuple[str, ...] = ("team", "agent", "skill", "prompt")   # data only: see _custom_row

_TEAM_ICONS: dict[str, str] = {
    "office": "🏢", "video": "🎬", "writing": "✍️", "brainstorm": "💡", "review": "🧐",
    "research": "📚", "code": "💻", "data": "📊", "translate": "🌐", "proposal": "📑",
    "report": "📄", "clinical": "🩺",
}
_ICONS = {"team": "🧩", "agent": "🤖", "skill": "🧠", "prompt": "💬", "mcp": "🔌", "hook": "🪝"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GalleryError(Exception):
    """The template does not exist, the arguments are wrong, or the target (group) does not exist
— the API layer turns this into a 400."""


# --------------------------------------------------------------------- helpers
def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"  # i18n-keep: truncation marker; U+2026 is not Chinese


def _ver(s: Any) -> tuple[int, ...]:
    """Version numbers are compared numerically: 1.10.0 > 1.9.0 (a string comparison gets it backwards)."""
    parts = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in parts[:4]) or (0,)


def _member_defs() -> dict[str, dict]:
    """Index member definitions by name (built-in seed members + role presets)."""
    return {**{a["name"]: a for a in SEED_AGENTS}, **{a["name"]: a for a in AGENT_PRESETS}}


# --------------------------------------------------------------------- built-in entries
def _skill_display(name: str, lang: str | None = None) -> str:
    """A built-in skill name as shown in `lang` (either spelling resolves)."""
    return display_skill_name(name, lang or i18n.current())


def _team_rows() -> list[dict]:
    who = _member_defs()
    out = []
    for t in TEMPLATES:
        members = []
        for n in t.get("members", []):
            d = who.get(n) or {}
            members.append({"name": d.get("name", n), "name_zh": d.get("name_zh", ""),
                            "avatar": d.get("avatar", "🤖"),
                            "role": d.get("role", ""), "role_zh": d.get("role_zh", "")})
        skills = list(t.get("skills", []))
        out.append({
            "id": f"team:{t['id']}", "kind": "team",
            "name": t["name"], "name_zh": t.get("name_zh", ""),
            "summary": t.get("desc", ""), "summary_zh": t.get("desc_zh", ""),
            "icon": _TEAM_ICONS.get(t["id"], _ICONS["team"]),
            "tags": [f"{len(members)} roles"], "tags_zh": [f"{len(members)} 个角色"],
            "source": "builtin", "home": bool(t.get("home", True)),
            # `preview` is for display only, so it carries both languages; `def` is what
            # gets installed and therefore stays canonical.
            "preview": {"members": members,
                        "host": display_name(t.get("host", ""), "en"),
                        "host_zh": display_name(t.get("host", ""), "zh"),
                        "skills": [_skill_display(s, "en") for s in skills],
                        "skills_zh": [_skill_display(s, "zh") for s in skills],
                        "prompt": _clip(t.get("prompt", ""), 200),
                        "prompt_zh": _clip(t.get("prompt_zh", ""), 200)},
            "def": {"members": list(t.get("members", [])), "host": t.get("host", ""),
                    "skills": skills, "prompt": t.get("prompt", ""), "name": t["name"]},
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
    for key, ex in EXAMPLE_SKILLS.items():
        scope = ex.get("scope", "member")
        out.append({
            "id": f"skill:{key}", "kind": "skill",
            "name": ex["name"], "name_zh": ex.get("name_zh", ""),
            "summary": ex.get("description", ""), "summary_zh": ex.get("description_zh", ""),
            "icon": _ICONS["skill"],
            "tags": ["Group rule" if scope == "group" else "Member skill"],
            "tags_zh": ["群聊规则" if scope == "group" else "成员技能"],
            "source": "builtin",
            "preview": {"description": ex.get("description", ""),
                        "description_zh": ex.get("description_zh", ""), "scope": scope,
                        "body": _clip(ex.get("body", ""), 220),
                        "body_zh": _clip(ex.get("body_zh", ""), 220)},
            "def": {"name": ex["name"], "description": ex.get("description", ""),
                    "body": ex.get("body", ""), "scope": scope},
        })
    return out


def _prompt_rows() -> list[dict]:
    out = []
    for p in SEED_PROMPTS:
        kind = p.get("kind", "general")
        out.append({
            "id": f"prompt:{p['key']}", "kind": "prompt",
            "name": p["title"], "name_zh": p.get("title_zh", ""),
            "summary": _clip(p.get("content", ""), 90),
            "summary_zh": _clip(p.get("content_zh", ""), 90),
            "icon": _ICONS["prompt"],
            "tags": ["Group prompt" if kind == "group" else "General"],
            "tags_zh": ["群提示词" if kind == "group" else "通用"],
            "source": "builtin",
            "preview": {"kind": kind, "content": _clip(p.get("content", ""), 200),
                        "content_zh": _clip(p.get("content_zh", ""), 200)},
            "def": {"title": p["title"], "content": p.get("content", ""),
                    "kind": kind, "use_globally": bool(p.get("use_globally"))},
        })
    return out


def _mcp_headers(m: dict) -> dict[str, str]:
    """The headers a template prefills. An empty value means the user pastes one in."""
    return {str(k): str(v) for k, v in (m.get("headers") or {}).items()}


def _mcp_rows() -> list[dict]:
    out = []
    for m in MCP_TEMPLATES:
        headers = _mcp_headers(m)
        # What the reader still has to supply. Both halves have to be listed: for the HTTP
        # servers that is the only thing standing between the entry and a working connection.
        needs = [*m.get("env_keys", []), *(k for k, v in headers.items() if not v)]
        out.append({
            "id": f"mcp:{m['key']}", "kind": "mcp",
            "name": m["name"], "name_zh": m.get("name_zh", ""),
            "summary": m.get("note", ""), "summary_zh": m.get("note_zh", ""),
            "icon": _ICONS["mcp"],
            "tags": ["Imported disabled"], "tags_zh": ["导入后停用"],
            "source": "builtin",
            "preview": {"command": m.get("command", ""), "args": list(m.get("args", [])),
                        "env_keys": list(m.get("env_keys", [])), "note": m.get("note", ""),
                        "note_zh": m.get("note_zh", ""),
                        "url": m.get("url", ""), "transport": m.get("transport", ""),
                        "headers": headers, "needs": needs},
            "def": {"name": m["name"], "command": m.get("command", ""),
                    "args": list(m.get("args", [])), "env_keys": list(m.get("env_keys", [])),
                    "url": m.get("url", ""), "transport": m.get("transport", ""),
                    "headers": headers, "header_keys": [k for k, v in headers.items() if not v],
                    "note": m.get("note", "")},
        })
    return out


def _hook_rows() -> list[dict]:
    out = []
    for h in HOOK_TEMPLATES:
        code = hook_code(h["key"])
        if not code.strip():
            continue                       # no source shipped: better absent than broken
        out.append({
            "id": f"hook:{h['key']}", "kind": "hook",
            "name": h["name"], "name_zh": h.get("name_zh", ""),
            "summary": h.get("summary", ""), "summary_zh": h.get("summary_zh", ""),
            "icon": h.get("icon") or _ICONS["hook"],
            "tags": ["Installed switched off"], "tags_zh": ["装进来是关闭的"],
            "source": "builtin",
            "preview": {"events": list(h.get("events", [])), "timeout_ms": h.get("timeout_ms", 1500),
                        "code": code},
            "def": {"key": h["key"], "events": list(h.get("events", [])),
                    "timeout_ms": h.get("timeout_ms", 1500), "code": code},
        })
    return out


def _builtin_rows() -> list[dict]:
    return [*_team_rows(), *_agent_rows(), *_skill_rows(), *_prompt_rows(), *_mcp_rows(), *_hook_rows()]


# --------------------------------------------------------------------- custom templates
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
    """Validate the required fields of a custom template by kind; returns the reason it failed
(None when it passes)."""
    if kind == "team":
        names = raw.get("members")
        if not isinstance(names, list) or not names or len(names) > 12:
            return i18n.pick_now("team needs 1-12 members (an array of member names)", "team 需要 1~12 个 members(成员名数组)")
        if any(not isinstance(n, str) or not n.strip() or len(n) > 60 for n in names):
            return i18n.pick_now("team members must be member names of 1-60 characters", "team 的 members 必须是 1~60 字的成员名")
        host = raw.get("host")
        if host is not None and (not isinstance(host, str) or not host.strip()):
            return i18n.pick_now("team host must be a member name", "team 的 host 必须是成员名")
        skills = raw.get("skills", [])
        if not isinstance(skills, list) or len(skills) > 8 or any(not isinstance(x, str) for x in skills):
            return i18n.pick_now("team skills must be an array of at most 8 skill names", "team 的 skills 必须是不超过 8 个的技能名数组")
        if len(str(raw.get("prompt", ""))) > 4000:
            return i18n.pick_now("team prompt is over 4000 characters", "team 的 prompt 超过 4000 字")
    elif kind == "agent":
        if len(str(raw.get("role", ""))) > 60:
            return i18n.pick_now("agent role is over 60 characters", "agent 的 role 超过 60 字")
        if len(str(raw.get("prompt", ""))) > 4000:
            return i18n.pick_now("agent prompt is over 4000 characters", "agent 的 prompt 超过 4000 字")
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 6:
            return i18n.pick_now("agent tags must be an array of at most 6 items", "agent 的 tags 必须是不超过 6 个的数组")
    elif kind == "skill":
        body = raw.get("body")
        if not isinstance(body, str) or not body.strip():
            return i18n.pick_now("skill needs a non-empty body", "skill 需要非空的 body")
        if len(body) > 8000:
            return i18n.pick_now("skill body is over 8000 characters", "skill 的 body 超过 8000 字")
        if raw.get("scope", "member") not in ("member", "group"):
            return i18n.pick_now("skill scope must be member or group", "skill 的 scope 只能是 member 或 group")
        if len(str(raw.get("description", ""))) > 200:
            return i18n.pick_now("skill description is over 200 characters", "skill 的 description 超过 200 字")
    elif kind == "prompt":
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            return i18n.pick_now("prompt needs a non-empty content", "prompt 需要非空的 content")
        if len(content) > 4000:
            return i18n.pick_now("prompt content is over 4000 characters", "prompt 的 content 超过 4000 字")
        # note: the entry's kind already means "this is a prompt", so the prompt's own type is
# called prompt_kind
        if raw.get("prompt_kind", "general") not in ("general", "group"):
            return i18n.pick_now("prompt_kind must be general or group", "prompt 的 prompt_kind 只能是 general(通用) 或 group(群提示词)")
    return None


def _custom_row(raw: Any, seen: set[str]) -> tuple[dict | None, str]:
    """Validate and normalize one custom template. Returns (entry, failure reason)."""
    if not isinstance(raw, dict):
        return None, i18n.pick_now("an entry must be an object", "条目必须是对象")
    rid = raw.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return None, i18n.pick_now("id must be 1-64 characters of letters, digits, dot, underscore or dash, starting with a letter or a digit", "id 必须是 1~64 位、以字母或数字开头的字母数字 . _ - 组合")
    kind = raw.get("kind")
    if kind in ("mcp", "hook"):
        # Both of these make the machine *do* something rather than hold data, and a hook does it on
        # every round. One JSON file dropped into a folder must not be able to install code that
        # runs later, so they are out of the custom format on purpose; install them through the
        # panel, where the file is shown before it is switched on.
        page = "MCP" if kind == "mcp" else "Hooks"
        return None, i18n.pick_now(
            f"custom templates do not support kind={kind} (it would let one JSON file install something that runs on this machine — add it yourself on the {page} page)",
            f"自定义模板不支持 kind={kind}(那等于让一个 JSON 文件往这台机器上装会运行的东西;请到「{'MCP' if kind == 'mcp' else '钩子'}」页自己添加)")
    if kind not in CUSTOM_KINDS:
        return None, i18n.pick_now(f"kind must be one of {' / '.join(CUSTOM_KINDS)}", f"kind 只能是 {' / '.join(CUSTOM_KINDS)}")
    fid = f"{kind}:{rid}"
    if fid in seen:
        return None, i18n.pick_now(f"id \"{rid}\" is not unique among the entries already in the gallery", f"id「{rid}」与目录里已有的条目不唯一")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        return None, i18n.pick_now("name must be 1-60 characters", "name 必须是 1~60 个字")
    req = raw.get("requires")
    if req and _ver(req) > _ver(CATALOG_VERSION):
        return None, i18n.pick_now(f"needs catalog version {req} or newer; this build is {CATALOG_VERSION}", f"需要程序目录版本 {req} 以上,当前是 {CATALOG_VERSION}")
    bad = _require(kind, raw)
    if bad:
        return None, bad
    seen.add(fid)
    summary = _clip(str(raw.get("summary") or raw.get("description") or ""), 200)
    icon = str(raw.get("icon") or _ICONS[kind])[:2] or _ICONS[kind]
    return {
        "id": fid, "kind": kind, "name": name.strip(), "summary": summary, "icon": icon,
        "tags": [t for t in (raw.get("tags") or []) if isinstance(t, str)][:4] or [i18n.pick_now("Custom", "自定义")],
        "source": f"custom:{raw.get('_file', '')}".rstrip(":"),
        "preview": _custom_preview(kind, raw),
        "def": _custom_def(kind, raw, name.strip()),
    }, ""


def _custom_preview(kind: str, raw: dict) -> dict:
    """The listing carries only "summary level" content, with the body served by the detail
endpoint — however large the catalog grows, the response cannot blow up."""
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
    """The full definition used when installing; only the fields each kind really needs are kept."""
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
                out.errors.append({"file": p.name, "reason": i18n.pick_now(f"file is over {CUSTOM_MAX_BYTES // 1024} KB, ignored", f"文件超过 {CUSTOM_MAX_BYTES // 1024} KB,已忽略")})
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.errors.append({"file": p.name, "reason": i18n.pick_now(f"could not be read or parsed: {e}", f"读取或解析失败:{e}")})
            continue
        if not isinstance(raw, dict):
            out.errors.append({"file": p.name, "reason": i18n.pick_now("the top level must be an object", "顶层必须是对象")})
            continue
        sv = raw.get("schema_version", SCHEMA_VERSION)
        if not isinstance(sv, int) or sv > SCHEMA_VERSION:
            out.errors.append({"file": p.name, "reason": i18n.pick_now(f"schema_version={sv} is not supported; this build supports up to {SCHEMA_VERSION}", f"schema_version={sv} 不支持,本程序支持到 {SCHEMA_VERSION}")})
            continue
        items = raw.get("items")
        if not isinstance(items, list):
            out.errors.append({"file": p.name, "reason": i18n.pick_now("the items array is missing", "缺少 items 数组")})
            continue
        if len(items) > CUSTOM_MAX_ITEMS:
            out.errors.append({"file": p.name, "reason": i18n.pick_now(f"more than {CUSTOM_MAX_ITEMS} entries, so the whole file was ignored", f"条目超过 {CUSTOM_MAX_ITEMS} 条,已忽略整个文件")})
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
    """Read the custom templates in the data directory. Cached by file mtime + size, so editing a
file needs no restart."""
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


# --------------------------------------------------------------------- catalog
def _all_items(store: Store) -> list[dict]:
    rows = _builtin_rows() + _load_custom(store).items
    order = {k: i for i, k in enumerate(KINDS)}
    rows.sort(key=lambda r: (order.get(r["kind"], 9), r["name"]))
    return rows


def _states(store: Store) -> dict:
    """Read the current state of every table in one go, used to mark entries as
"installed / not installed"."""
    return {
        "groups": [g["name"] for g in store.list_groups()],
        "agents": {n for a in store.list_agents() for n in (builtin_names(builtin_for(a["name"])) or [a["name"]])},
        "skills": {n for s in list_skills(store.data_dir / "skills") for n in skill_names(s.name)},
        "prompts": {n for p in store.list_prompts() for n in prompt_titles(p["title"])},
        "mcp": {n for m in store.list_mcp() for n in mcp_names(m["name"])},
        "hooks": {d.name for d in (store.data_dir / "hooks").glob("*") if d.is_dir() and not d.name.startswith(".")},
    }


def _spellings(item: dict) -> list[str]:
    """Every spelling the built-in item's name may have been stored under.

    An item returned to the client carries only the requested language, so the other
    spelling is recovered from the id (teams) or from the built-in tables — that is what
    keeps the "already installed" badge correct after a language switch.
    """
    kind, ident = item["kind"], item.get("id", "")
    if kind == "team":
        t = next((x for x in TEMPLATES if f"team:{x['id']}" == ident), None)
        out = [n for n in ((t or {}).get("name"), (t or {}).get("name_zh")) if n]
        return out or [item["name"]]
    if kind == "agent":
        return builtin_names(builtin_for(item["name"])) or [item["name"]]
    if kind == "skill":
        return skill_names(item["name"]) or [item["name"]]
    if kind == "prompt":
        return prompt_titles(item["name"]) or [item["name"]]
    if kind == "mcp":
        return mcp_names(item["name"]) or [item["name"]]
    if kind == "hook":
        return hook_names(item["name"]) or [item["name"]]
    return [item["name"]]


def _state(item: dict, st: dict) -> tuple[bool, str]:
    kind = item["kind"]
    names = _spellings(item)
    if kind == "team":
        n = sum(1 for g in st["groups"] if any(g == nm or g.startswith(nm + " ") for nm in names))
        return (n > 0, i18n.pick_now(f"{n} already created", f"已建 {n} 个群") if n else "")
    if kind == "agent":
        ok = any(nm in st["agents"] for nm in names)
        return (ok, i18n.pick_now("A member with this name exists", "已有同名成员") if ok else "")
    if kind == "skill":
        ok = any(nm in st["skills"] for nm in names)
        return (ok, i18n.pick_now("Already in the skill library", "已在技能库") if ok else "")
    if kind == "prompt":
        ok = any(nm in st["prompts"] for nm in names)
        return (ok, i18n.pick_now("Already in the prompt library", "已在提示词库") if ok else "")
    if kind == "hook":
        ok = any(nm in st["hooks"] for nm in names)
        return (ok, i18n.pick_now("Already in the hooks directory", "已在钩子目录里") if ok else "")
    ok = any(nm in st["mcp"] for nm in names)
    return (ok, i18n.pick_now("Already added", "已添加") if ok else "")


def _slim(item: dict, st: dict) -> dict:
    installed, note = _state(item, st)
    return {k: v for k, v in item.items() if k != "def"} | {"installed": installed, "state_note": note}


def _localized(items: list[dict]) -> list[dict]:
    """Built-in entries are returned in the requested language (English base field / Chinese
`<field>_zh`)."""
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
        "categories": _localized(CATEGORIES),
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


# --------------------------------------------------------------------- apply
def _unique_group_name(store: Store, base: str) -> str:
    fallback = i18n.pick_now("New group", "新群聊")
    base = _clip(base or fallback, 60) or fallback
    taken = {g["name"] for g in store.list_groups()}
    if base not in taken:
        return base
    for n in range(2, 50):
        cand = _clip(base, 60 - len(str(n)) - 1) + f" {n}"
        if cand not in taken:
            return cand
    return _clip(base, 55) + i18n.pick_now(" copy", " 副本")


def _same_member(stored: str | None, wanted: str | None) -> bool:
    """Does a stored member name answer to `wanted`, in either language?"""
    return wanted in (builtin_names(builtin_for(stored)) or [stored])


def _skill_label(name: str, lang: str) -> str:
    return _skill_display(name, lang)


def _prompt_label(title: str, lang: str) -> str:
    entry = prompt_for(title)
    if not entry:
        return title
    return (entry.get("title_zh") if lang == "zh" else entry["title"]) or title


def _defs_index(store: Store) -> dict[str, dict]:
    """Index the available skill and member definitions by name (built-in + custom); both the
Chinese and the English spelling can be looked up."""
    skills: dict[str, dict] = {}
    for key, ex in EXAMPLE_SKILLS.items():
        entry = {**ex, "name": ex["name"], "key": key}
        for nm in (key, ex["name"], ex.get("name_zh")):
            if nm:
                skills.setdefault(nm, entry)
    agents: dict[str, dict] = {}
    for it in _load_custom(store).items:
        d = it.get("def") or {}
        if it["kind"] == "skill":
            skills.setdefault(it["name"], d)
        elif it["kind"] == "agent":
            agents[it["name"]] = d
    return {"skills": skills, "agents": agents}


def _ensure_member(store: Store, name: str, idx: dict) -> dict | None:
    """Reuse a member with the same name; if there is none, create it from the role preset or the
custom role."""
    for a in store.list_agents():
        if _same_member(a["name"], name):
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
    # A built-in skill always lands under its canonical name, so installing a template on
    # a Chinese install does not create a second copy next to the one the first run
    # seeded. Custom skills keep whatever name they were written with.
    target = canonical_skill_name(name) if skill_for(name) else name
    have = {s.name for s in list_skills(d)}
    if (target in have or name in have) and not overwrite:
        return "skipped"
    sk = idx["skills"].get(name)
    if not sk:
        return "missing"
    write_skill(d, target, sk.get("description", ""), sk.get("body", ""),
                sk.get("scope", "member"), version=CATALOG_VERSION)
    return "written"


# One install at a time. A team install creates the members, writes their skills to disk and only
# then creates the group — none of that is a transaction, and `groups.name` has no unique constraint
# to catch a duplicate, so two overlapping installs would interleave into a half-built template.
_APPLY_LOCK = threading.Lock()


def apply(store: Store, item_id: str, opts: dict | None = None) -> dict:
    opts = opts or {}
    item = find(store, item_id)
    if item is None:
        raise GalleryError(i18n.pick_now("That template no longer exists (it may just have been removed).",
                                         "模板不存在(可能刚被移除)"))
    fn = {"team": _apply_team, "agent": _apply_agent, "skill": _apply_skill,
          "prompt": _apply_prompt, "mcp": _apply_mcp, "hook": _apply_hook}[item["kind"]]
    with _APPLY_LOCK:
        return fn(store, item, opts)


def _result(item: dict, summary: str, **kw: Any) -> dict:
    return {"kind": item["kind"], "id": item["id"], "name": item["name"], "summary": summary,
            "group": None, "agents": [], "added": [], "skipped": [], "notes": [], **kw}


def _apply_team(store: Store, item: dict, opts: dict) -> dict:
    src = item["def"]
    idx = _defs_index(store)
    lang = i18n.current()
    members, created, reused, notes = [], [], [], []
    for n in src.get("members", []):
        label = display_name(n, lang)
        existed = any(_same_member(a["name"], n) for a in store.list_agents())
        a = _ensure_member(store, n, idx)
        if a is None:
            notes.append(i18n.pick_now(
                f"No definition found for member \"{label}\" — members of a custom team "
                "template have to resolve to a role preset or a custom role, so it was skipped",
                f"找不到成员「{label}」的定义(自定义团队模板里的成员要能被岗位预设或自定义角色解析),已跳过"))
            continue
        if a["id"] not in {m["id"] for m in members}:
            members.append(a)
            (reused if existed else created).append(label)
    if not members:
        raise GalleryError(i18n.pick_now("This template has no usable members",
                                         "这个模板没有可用的成员"))

    host = next((m for m in members if _same_member(m["name"], src.get("host"))), members[0])
    added, skipped = [], []
    skills, fresh, already = [], 0, 0
    for s in src.get("skills", []) or []:
        r = _install_skill(store, s, idx, bool(opts.get("overwrite")))
        label = _skill_label(s, lang)
        if r == "missing":
            notes.append(i18n.pick_now(
                f"Skill \"{label}\" is not in the template gallery, so it was not attached",
                f"技能「{label}」不在模板中心里,没有挂上去"))
            continue
        skills.append(canonical_skill_name(s))
        if r == "skipped":
            already += 1
            skipped.append(i18n.pick_now(f"Skill: {label}", f"技能:{label}"))
        else:
            fresh += 1
            added.append(i18n.pick_now(f"Skill: {label}", f"技能:{label}"))

    if fresh and already:
        extra = i18n.pick_now(f", and installed {fresh} skill(s) ({already} already present)",
                              f",新装 {fresh} 个技能({already} 个本来就有)")
    elif fresh:
        extra = i18n.pick_now(f", and installed {fresh} skill(s)", f",并装好 {fresh} 个技能")
    elif already:
        extra = i18n.pick_now(f"; its {already} skill(s) were already present",
                              f",随附的 {already} 个技能本来就有")
    else:
        extra = ""

    # `item["name"]` is already in the request language, so a group created from the
    # template is named the way the user sees the template.
    name = _unique_group_name(store, str(opts.get("name") or item["name"] or src.get("name")))
    g = group_view(store.create_group(name, host["id"], [m["id"] for m in members],
                                      ext={"skills": skills}, prompt=src.get("prompt", "")))
    return _result(
        item,
        i18n.pick_now(
            f"Group \"{g['name']}\" is ready: host {display_name(host['name'], lang)}, "
            f"{len(members)} members{extra}.",
            f"已建好群聊「{g['name']}」:群主 {display_name(host['name'], lang)},"
            f"成员 {len(members)} 人{extra}。"),
        group=g, agents=[display_name(m["name"], lang) for m in members],
        added=added, skipped=skipped, notes=notes,
    )


def _apply_agent(store: Store, item: dict, opts: dict) -> dict:
    idx = _defs_index(store)
    lang = i18n.current()
    label = display_name(item["name"], lang)
    existed = any(_same_member(a["name"], item["name"]) for a in store.list_agents())
    a = _ensure_member(store, item["name"], idx)
    if a is None:
        raise GalleryError(i18n.pick_now("This role could not be created", "无法创建这个角色"))
    added, notes = [], []
    if existed:
        notes.append(i18n.pick_now(f'A member named "{label}" already existed, so it was reused',
                                   f"已有同名成员「{label}」,直接复用了它"))
    else:
        added.append(i18n.pick_now(f"Member: {label}", f"成员:{label}"))

    gid = opts.get("group_id")
    g = store.get_group(str(gid)) if gid else None
    if gid:
        if not g:
            raise GalleryError(i18n.pick_now("The group to add them to does not exist",
                                             "要拉进的群聊不存在"))
        if a["id"] in set(g.get("member_ids") or []):
            notes.append(i18n.pick_now(f'"{label}" is already in this group',
                                       f"「{label}」本来就在这个群里"))
        else:
            store.add_member(g["id"], a["id"])
            added.append(i18n.pick_now(f"Joined: {g['name']}", f"入群:{g['name']}"))
    summary = i18n.pick_now(f'Member "{label}" is ready', f"已准备好成员「{label}」")
    summary += (i18n.pick_now(f', and joined the group "{g["name"]}".', f",并加入群「{g['name']}」。")
                if gid and not notes else i18n.pick_now(".", "。"))
    return _result(item, summary, agents=[label], added=added, notes=notes)


def _apply_skill(store: Store, item: dict, opts: dict) -> dict:
    idx = _defs_index(store)
    label = item["name"]
    r = _install_skill(store, label, idx, bool(opts.get("overwrite")))
    if r == "missing":
        raise GalleryError(i18n.pick_now("This skill has no body to write",
                                         "这个技能没有可写入的正文"))
    if r == "skipped":
        return _result(item, i18n.pick_now(
            f'Skill "{label}" is already in the skill library (choose Re-import to overwrite it).',
            f"技能「{label}」已经在技能库里了(要覆盖请选「重新导入」)。"),
            skipped=[i18n.pick_now(f"Skill: {label}", f"技能:{label}")],
            notes=[i18n.pick_now(
                "A skill with this name already exists. To replace it with the version from the "
                "template, click Re-import.",
                "同名技能已存在。想用模板里的版本覆盖它,点「重新导入」。")])
    scope = (idx["skills"].get(label) or {}).get("scope", "member")
    where = (i18n.pick_now("tick it for a group under Extensions on the right-hand side of the chat",
                           "在群聊右侧「扩展」里勾给某个群") if scope == "group"
             else i18n.pick_now("tick it for the members that need it", "勾给需要的成员"))
    return _result(item, i18n.pick_now(
        f'Skill "{label}" imported: {where} and it takes effect.',
        f"已导入技能「{label}」:{where}即可生效。"),
        added=[i18n.pick_now(f"Skill: {label}", f"技能:{label}")])


def _apply_prompt(store: Store, item: dict, opts: dict) -> dict:
    d = item["def"]
    title = d["title"]
    lang = i18n.current()
    label = _prompt_label(title, lang)
    entry = prompt_for(title)
    # A library seeded before the content was translated still holds the other spelling
    # of the title, so match on the built-in entry rather than on the literal string.
    hit = next((p for p in store.list_prompts()
                if p["title"] == title or (entry and prompt_for(p["title"]) is entry)), None)
    if hit is not None:
        if not opts.get("overwrite"):
            return _result(item, i18n.pick_now(
                f'Prompt "{label}" is already in the library (choose Re-import to overwrite it).',
                f"提示词「{label}」已经在库里了(要覆盖请选「重新导入」)。"),
                skipped=[i18n.pick_now(f"Prompt: {label}", f"提示词:{label}")],
                notes=[i18n.pick_now(
                    "A prompt with this title already exists. To replace it with the version from "
                    "the template, click Re-import.",
                    "同名提示词已存在。想用模板里的版本覆盖它,点「重新导入」。")])
        store.update_prompt(hit["id"], {"content": d["content"]})
        return _result(item, i18n.pick_now(
            f'Prompt "{label}" was updated to the version from the template.',
            f"提示词「{label}」已更新为模板里的版本。"),
            added=[i18n.pick_now(f"Prompt: {label}", f"提示词:{label}")])
    store.add_prompt(title, d["content"], d.get("kind", "general"), bool(d.get("use_globally")))
    return _result(item, i18n.pick_now(
        f'Saved to the prompt library: "{label}". Attach it to a group, or make it global on the '
        "Prompts page.",
        f"已存入提示词库:「{label}」。到「提示词」页可以挂给某个群或设为全局。"),
        added=[i18n.pick_now(f"Prompt: {label}", f"提示词:{label}")])


def _apply_hook(store: Store, item: dict, opts: dict) -> dict:
    """Unpack a hook template into the hooks directory — switched off.

    Off, like every other import in this gallery, and more so here: every other kind is data the
    app interprets, while a hook is a program that runs on this machine every round. What the user
    gets is a readable draft they turn on themselves (Settings → Hooks → Run once is right there
    to try it first).

    The two files are the whole format — HOOK.json plus hook.py — so "installing" is writing them
    and then telling the manager to look again; nothing about it is special-cased in the app.
    """
    src = item["def"]
    key = str(src.get("key") or item["id"].split(":", 1)[-1])
    folder = Path(store.data_dir) / "hooks" / key
    if folder.exists() and not opts.get("overwrite"):
        raise GalleryError(i18n.pick_now(
            f"A folder named \"{key}\" is already in the hooks directory. Rename or remove it "
            "first, or ask to overwrite.",
            f"钩子目录里已经有一个叫「{key}」的文件夹了。请先改名或移除,或者选择覆盖。"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hook.py").write_text(str(src.get("code") or ""), encoding="utf-8")
        (folder / "HOOK.json").write_text(json.dumps({
            "name": item["name"], "description": item.get("summary", ""),
            "events": list(src.get("events") or []), "enabled": False,
            "timeout_ms": int(src.get("timeout_ms") or 1500), "on_error": "auto", "groups": [],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise GalleryError(i18n.pick_now(f"Could not write the hook: {e}", f"写不了这个钩子:{e}")) from e
    return _result(item, i18n.pick_now("Hook installed, switched off", "钩子已装好,处于关闭状态"),
                   notes=[i18n.pick_now(
                       "Read it before turning it on: Settings → Hooks → Run once tries it against a "
                       "sample payload, and the file is plain Python you can edit in place.",
                       "打开之前先读一遍:「设置 → 钩子 → 跑一次」会用示例数据试跑,文件就是普通的 Python,可以就地改。")])


def _apply_mcp(store: Store, item: dict, opts: dict) -> dict:
    d = item["def"]
    label = item["name"]
    if any(nm in {m["name"] for m in store.list_mcp()} for nm in mcp_names(d["name"])):
        return _result(item, i18n.pick_now(f'MCP server "{label}" has already been added.',
                                           f"MCP 服务器「{label}」已经添加过了。"),
                       skipped=[i18n.pick_now(f"MCP: {label}", f"MCP:{label}")])
    args = [PLACEHOLDER_DIR if a == "/tmp" else str(a) for a in d.get("args", [])]
    note = (item.get("summary") or "") + i18n.pick_now(
        " It is imported disabled: check the command, fill in the keys it needs, then enable it "
        "on the MCP page.",
        " 导入后是停用状态:核对命令、填好密钥后再到「MCP」页启用。")
    # A URL template is a server somebody else runs, so there is no command to review — but the
    # URL is worth reading before it is enabled, and it goes into the same `description` the MCP
    # page shows. An empty header value stays empty here on purpose: this app does not know the
    # user's token, and guessing one would either fail or look like it worked.
    headers = _mcp_headers(d)
    if headers and not d.get("command"):
        note = note + i18n.pick_now(f" It is reached at {d.get('url', '')}.",
                                    f" 它通过 {d.get('url', '')} 连接。")
    row = store.add_mcp(d["name"], d.get("command", ""), args,
                        {k: "" for k in d.get("env_keys", [])}, d.get("url", ""),
                        d.get("transport", ""), headers, note.strip())
    store.update_mcp(row["id"], {"enabled": False})
    return _result(item, i18n.pick_now(
        f'Added MCP server "{label}" (disabled). Check the command on the MCP page, fill in what it '
        "needs, then enable it.",
        f"已添加 MCP 服务器「{label}」(停用状态)。到「MCP」页核对命令、填好需要的东西再启用。"),
        added=[i18n.pick_now(f"MCP: {label}", f"MCP:{label}")],
        notes=[i18n.pick_now(
            "MCP runs commands on your computer. This app will not enable it for you, and will not "
            "fill in any key for you.",
            "MCP 会在你电脑上运行命令,本程序不会替你启用,也不会替你填任何密钥。")])
