"""Updates and discovery: check GitHub for new versions of the app itself, the model catalog,
skills and plugins, and search for new skills / plugins / MCP servers.

Safety boundaries (deliberate):
  * Checks and searches are read-only and gated by the "allow cloud model calls" master switch
    (nothing touches the network in offline mode).
  * Text content (the model catalog JSON, SKILL.md) may be updated automatically, and automatic
    skill updates are off by default.
  * Anything that executes code - plugins (Python), MCP servers (command line), the app itself -
    is never installed automatically:
    a plugin must be previewed in full source first, then installed by confirming the sha256
    computed at preview time (the server downloads it again and compares, and refuses if the
    content changed);
    MCP only offers a pre-filled config form, and the command is saved once you confirm it;
    the app itself only reports the new version and the release page, it is never replaced.
  * Content from GitHub is always treated as untrusted input: handled as text only, a skill body
    is never executed as instructions, only injected as a prompt (and can be previewed).
"""

from __future__ import annotations

from . import i18n

import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import catalog as catalog_lib
from . import local_models as lm
from .modelopts import model_options, refresh_live
from .router import has_credentials
from .store import Store
from .versions import is_newer
from .tools import Skill, parse_skill_text, safe_skill_name, write_skill

API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
MAX_TEXT = 512 * 1024
CATALOG_PATH = "backend/app/data/catalog.json"
LOCAL_CATALOG_PATH = "backend/app/data/local_models.json"
OLLAMA_LIBRARY = "https://ollama.com/library?sort=newest"
OLLAMA_REGISTRY = "https://registry.ollama.ai/v2"
HF_API = "https://huggingface.co/api/models"
MAX_NEW_PER_SOURCE = 6      # how many items to report per source, so the first check does not flood the screen
QUANT_RE = re.compile(r"fp8|fp4|int[48]|awq|gptq|gguf|mlx|bnb|nvfp4|mxfp|-eagle|-draft", re.I)

SEARCH_TOPICS = {
    "skill": ["agent-skills", "claude-skills"],
    "mcp": ["mcp-server", "mcp-servers"],
    "plugin": ["team-agent-plugin"],
}
# recommended sources: names come from known official/well-known repos, the contents follow
# what is actually on GitHub (the UI tells you to look before installing)
CURATED = [
    {"kind": "skill", "repo": "anthropics/skills",
     "desc": ("Anthropic's official collection of Agent Skills examples (SKILL.md format)",
              "Anthropic 官方的 Agent Skills 示例集合(SKILL.md 格式)")},
    {"kind": "mcp", "repo": "modelcontextprotocol/servers",
     "desc": ("The official collection of reference MCP servers", "MCP 官方参考服务器集合")},
]


def curated() -> list[dict]:
    """The curated source list, described in the request language."""
    return [{**c, "desc": i18n.pick_now(*c["desc"])} for c in CURATED]


class GitHubError(Exception):
    def __init__(self, msg: str, status: int = 502, kind: str = ""):
        super().__init__(msg)
        self.status = status  # 502 = GitHub/network trouble; 400 = bad arguments; 409 = conflict
        # A stable reason code. Callers must branch on this rather than on the message
        # text: the message is shown to the user and therefore follows the UI language.
        self.kind = kind      # "" | "not_found" | "rate_limit" | "offline"


def valid_repo(repo: str) -> str:
    repo = (repo or "").strip().removeprefix("https://github.com/").strip("/")
    repo = re.sub(r"\.git$", "", repo)
    if not REPO_RE.match(repo) or any(part.strip(".") == "" for part in repo.split("/")):
        raise GitHubError(i18n.pick_now("The repository must be in owner/repo form", "仓库格式应为 owner/repo"), 400)
    return repo


def valid_path(path: str) -> str:
    path = (path or "").strip().lstrip("/")
    if not path or ".." in path.split("/") or len(path) > 300:
        raise GitHubError(i18n.pick_now("That file path is not valid", "文件路径不合法"), 400)
    return path


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0])[:4]) or (0,)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Updater:
    def __init__(self, store: Store, app_version: str, transport: httpx.AsyncBaseTransport | None = None):
        self.store, self.app_version, self._transport = store, app_version, transport
        self.checking = False

    # ---------------------------------------------------------------- http
    def _allowed(self) -> None:
        if not self.store.get_settings()["external_calls_enabled"]:
            raise GitHubError(i18n.pick_now("Outbound calls are disabled (offline mode), so GitHub will not be contacted", "外呼已禁用(离线模式),不会连接 GitHub"), 403, "offline")

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "team-agent-updater",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token = self.store.get_settings().get("github_token") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url=API, headers=headers, timeout=20, transport=self._transport)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        self._allowed()
        try:
            async with self._client() as c:
                r = await c.get(path, params=params)
        except httpx.HTTPError as e:
            raise GitHubError(i18n.pick_now(f"Cannot reach GitHub: {type(e).__name__}", f"无法连接 GitHub:{type(e).__name__}")) from None
        if r.status_code == 404:
            raise GitHubError(i18n.pick_now("GitHub has no such repository or file", "GitHub 上找不到这个仓库或文件"), 404, "not_found")
        if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError(i18n.pick_now("GitHub's API rate limit was hit. Try again later, or fill in a GitHub token in Settings.", "GitHub 接口频率超限,请稍后再试,或在设置里填写 GitHub Token"), 403, "rate_limit")
        if r.status_code >= 400:
            raise GitHubError(i18n.pick_now(f"GitHub returned {r.status_code}", f"GitHub 返回 {r.status_code}"))
        return r.json()

    async def file(self, repo: str, path: str, ref: str = "") -> dict:
        """Read one text file in the repo -> {content, sha (git blob), size}."""
        repo, path = valid_repo(repo), valid_path(path)
        data = await self._get(f"/repos/{repo}/contents/{quote(path)}", {"ref": ref} if ref else None)
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubError(i18n.pick_now("That is not a file", "这不是一个文件"))
        if int(data.get("size", 0)) > MAX_TEXT:
            raise GitHubError(i18n.pick_now("The file is too big (512 KB limit), so it will not be downloaded", "文件太大(上限 512 KB),不会下载"))
        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise GitHubError(i18n.pick_now("The file is not UTF-8 text", "文件不是 UTF-8 文本")) from None
        return {"content": content, "sha": data.get("sha", ""), "size": len(content.encode())}

    # ------------------------------------------------------------ search
    async def search(self, kind: str, query: str = "") -> list[dict]:
        if kind not in SEARCH_TOPICS:
            raise GitHubError(i18n.pick_now("kind must be one of skill / plugin / mcp", "kind 只能是 skill / plugin / mcp"), 400)
        seen: dict[str, dict] = {}
        for topic in SEARCH_TOPICS[kind]:
            q = f"{query.strip()} topic:{topic}".strip()
            data = await self._get("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 15})
            for it in data.get("items", []):
                seen.setdefault(it["full_name"], {
                    "repo": it["full_name"], "description": it.get("description") or "", "stars": it.get("stargazers_count", 0),
                    "updated_at": it.get("pushed_at") or it.get("updated_at") or "", "url": it.get("html_url", ""),
                    "license": ((it.get("license") or {}).get("spdx_id") or ""), "archived": bool(it.get("archived")),
                    "default_branch": it.get("default_branch", ""),
                })
        return sorted(seen.values(), key=lambda x: -x["stars"])

    async def skill_files(self, repo: str, ref: str = "") -> list[dict]:
        repo = valid_repo(repo)
        if not ref:
            ref = (await self._get(f"/repos/{repo}")).get("default_branch", "main")
        tree = await self._get(f"/repos/{repo}/git/trees/{quote(ref)}", {"recursive": "1"})
        out = [{"path": t["path"], "sha": t.get("sha", ""), "name": t["path"].rsplit("/", 2)[-2] if "/" in t["path"] else repo.split("/")[1]}
               for t in tree.get("tree", []) if t.get("type") == "blob" and t["path"].lower().endswith("skill.md")]
        return out[:200]

    async def plugin_files(self, repo: str, ref: str = "") -> list[dict]:
        repo = valid_repo(repo)
        if not ref:
            ref = (await self._get(f"/repos/{repo}")).get("default_branch", "main")
        tree = await self._get(f"/repos/{repo}/git/trees/{quote(ref)}", {"recursive": "1"})
        out = [{"path": t["path"], "sha": t.get("sha", ""), "size": t.get("size", 0)} for t in tree.get("tree", [])
               if t.get("type") == "blob" and t["path"].endswith(".py")
               and (t["path"].count("/") == 0 or t["path"].startswith("plugins/")) and int(t.get("size", 0)) < MAX_TEXT]
        return out[:100]

    async def readme(self, repo: str) -> dict:
        repo = valid_repo(repo)
        data = await self._get(f"/repos/{repo}/readme")
        try:
            text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        except ValueError:
            text = ""
        return {"repo": repo, "content": text[:20000], "url": data.get("html_url", "")}

    # ------------------------------------------------------------- skills
    async def preview(self, repo: str, path: str, ref: str = "") -> dict:
        f = await self.file(repo, path, ref)
        return {**f, "sha256": sha256_hex(f["content"]), "repo": valid_repo(repo), "path": valid_path(path), "ref": ref}

    async def install_skill(self, repo: str, path: str, ref: str = "", overwrite: bool = False) -> Skill:
        f = await self.file(repo, path, ref)
        parts = valid_path(path).split("/")
        parsed = parse_skill_text(f["content"], parts[-2] if len(parts) > 1 else valid_repo(repo).split("/")[1])
        name = safe_skill_name(parsed.name)
        if not name:
            raise GitHubError(i18n.pick_now("The skill has no usable name", "技能没有可用的名称"))
        sdir = self.store.data_dir / "skills"
        exists = (sdir / name / "SKILL.md").exists()
        src = self.store.get_source("skill", name)
        if exists and not overwrite and not (src and src["repo"] == valid_repo(repo)):
            raise GitHubError(i18n.pick_now(f"A skill named \"{name}\" already exists; tick Overwrite to replace it", f"已有同名技能「{name}」,勾选「覆盖」才会替换"), 409)
        skill = write_skill(sdir, name, parsed.description, parsed.body, parsed.scope, parsed.version)
        self.store.set_source("skill", name, valid_repo(repo), valid_path(path), ref, f["sha"])
        self.store.resolve_updates("skill", name)
        return skill

    # ------------------------------------------------------------ plugins
    async def install_plugin(self, repo: str, path: str, ref: str, sha256: str, overwrite: bool = False) -> str:
        """Install a plugin file. sha256 must equal the hash of the content the user saw at preview
        time, otherwise it is refused (so the content cannot be swapped after the preview)."""
        f = await self.file(repo, path, ref)
        if not path.endswith(".py"):
            raise GitHubError(i18n.pick_now("A plugin has to be a .py file", "插件必须是 .py 文件"), 400)
        if sha256_hex(f["content"]) != sha256:
            # 412 rather than 409: the duplicate-name conflict below is also a 409, and the client
            # has to tell the two apart to decide between "re-preview" and "overwrite?". It cannot
            # do that from the message, which is worded per request language.
            raise GitHubError(i18n.pick_now("The file content no longer matches what you previewed (it may have changed); preview it again before installing", "文件内容与你预览的不一致(可能已被修改),请重新预览后再安装"), 412)
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(path).stem)
        dest = self.store.data_dir / "plugins" / f"{stem}.py"
        src = self.store.get_source("plugin", stem)
        if dest.exists() and not overwrite and not (src and src["repo"] == valid_repo(repo)):
            raise GitHubError(i18n.pick_now(f"A plugin named \"{stem}\" already exists; tick Overwrite to replace it", f"已有同名插件「{stem}」,勾选「覆盖」才会替换"), 409)
        dest.write_text(f["content"], encoding="utf-8")
        self.store.set_source("plugin", stem, valid_repo(repo), valid_path(path), ref, f["sha"])
        self.store.resolve_updates("plugin", stem)
        return stem

    # -------------------------------------------------------------- checks
    async def check_app(self) -> dict:
        repo = valid_repo(self.store.get_settings()["app_repo"]) if self.store.get_settings()["app_repo"] else ""
        if not repo:
            return {"configured": False, "current": self.app_version}
        try:
            rel = await self._get(f"/repos/{repo}/releases/latest")
        except GitHubError as e:
            if e.kind == "not_found":
                return {"configured": True, "current": self.app_version, "latest": None, "available": False,
                        "note": i18n.pick_now("This repository has no releases yet", "这个仓库还没有发布(Releases)")}
            raise
        latest = str(rel.get("tag_name", ""))
        avail = parse_version(latest) > parse_version(self.app_version)
        info = {"configured": True, "current": self.app_version, "latest": latest, "available": avail,
                "url": rel.get("html_url", ""), "notes": (rel.get("body") or "")[:3000],
                "published_at": rel.get("published_at", ""),
                "assets": [{"name": a["name"], "size": a.get("size", 0), "url": a.get("browser_download_url", "")}
                           for a in rel.get("assets", [])][:10]}
        if avail:
            self.store.upsert_update("app", latest, i18n.pick_now(f"A new version of the app is available: {latest} (you are on {self.app_version})", f"程序有新版本 {latest}(当前 {self.app_version})"), info)
        return info

    async def fetch_catalog(self) -> dict | None:
        cfg = self.store.get_settings()
        if cfg["catalog_url"]:
            url = cfg["catalog_url"].strip()
            if not url.startswith("https://"):
                raise GitHubError(i18n.pick_now("The model catalog URL must be https", "模型目录地址必须是 https"))
            self._allowed()
            try:
                async with httpx.AsyncClient(timeout=20, transport=self._transport, follow_redirects=True) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                    if len(r.content) > 2 * MAX_TEXT:
                        raise GitHubError(i18n.pick_now("The model catalog file is too big", "模型目录文件太大"))
                    return r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise GitHubError(i18n.pick_now(f"Could not download the model catalog: {type(e).__name__}", f"下载模型目录失败:{type(e).__name__}")) from None
        if cfg["app_repo"]:
            f = await self.file(cfg["app_repo"], CATALOG_PATH)
            try:
                return json.loads(f["content"])
            except ValueError:
                raise GitHubError(i18n.pick_now("The model catalog is not valid JSON", "模型目录不是合法 JSON")) from None
        return None

    async def check_catalog(self, apply: bool = False) -> dict:
        data = await self.fetch_catalog()
        cur = self.store.catalog
        if data is None:
            return {"configured": False, "current": cur.version}
        err = catalog_lib.validate(data)
        if err:
            raise GitHubError(i18n.pick_now(f"The model catalog has the wrong shape: {err}", f"模型目录格式不对:{err}"))
        newer = is_newer(data["version"], cur.version)
        added = 0
        if newer:
            old = {(p, m["id"]) for p, v in cur.data["providers"].items() for m in v["models"]}
            added = sum(1 for p, v in data["providers"].items() for m in v["models"] if (p, m["id"]) not in old)
        info = {"configured": True, "current": cur.version, "latest": str(data["version"]), "available": newer,
                "new_models": added, "applied": False}
        if newer and apply:
            cur.save_override(data)
            info["applied"] = True
            self.store.resolve_updates("catalog", "catalog")
        elif newer:
            self.store.upsert_update("catalog", "catalog", i18n.pick_now(f"A new model catalog is available: {data['version']} ({added} new models)", f"模型目录有新版本 {data['version']}(新增 {added} 个型号)"), info)
        return info

    async def check_sources(self, kind: str, auto_apply: bool = False) -> list[dict]:
        """For installed skills/plugins that recorded a source, compare the sha of the file on GitHub."""
        out = []
        for src in self.store.list_sources(kind):
            try:
                f = await self.file(src["repo"], src["path"], src["ref"])
            except GitHubError as e:
                out.append({"name": src["name"], "error": str(e)})
                continue
            changed = f["sha"] != src["sha"]
            item = {"name": src["name"], "repo": src["repo"], "path": src["path"], "available": changed, "applied": False}
            if changed and kind == "skill" and auto_apply:
                await self.install_skill(src["repo"], src["path"], src["ref"])
                item["applied"] = True
            elif changed:
                label = i18n.pick_now("Skill", "技能") if kind == "skill" else i18n.pick_now("Plugin", "插件")
                name = src["name"]
                self.store.upsert_update(kind, name, i18n.pick_now(
                    f'{label} "{name}" has an update on GitHub',
                    f"{label}《{name}》在 GitHub 上有更新"),
                                         {"name": src["name"], "repo": src["repo"], "path": src["path"], "ref": src["ref"],
                                          "sha": f["sha"], "current_sha": src["sha"]})
            out.append(item)
        return out

    async def check_models(self) -> list[dict]:
        """Query every cloud/local provider for its live model list and look for models you have not seen yet."""
        out = []
        cfg = self.store.get_settings()
        for p in self.store.list_providers():
            if not p["enabled"] or not has_credentials(p) or (not p["is_local"] and not cfg["external_calls_enabled"]):
                continue
            try:
                await refresh_live(self.store, p["id"])
            except Exception as e:  # noqa: BLE001 — one provider failing does not affect the others
                out.append({"provider_id": p["id"], "name": p["name"], "error": str(e)[:120]})
                continue
            opts = model_options(self.store, p["id"])
            new = [m["id"] for m in opts["models"] if m["is_new"]]
            gone = [m["id"] for m in opts["models"] if m["gone"]]
            if p["is_local"]:  # a local provider's list is "what is installed", so "new/gone" means nothing for it
                continue
            if new:
                self.store.upsert_update("model", p["id"], i18n.pick_now(f"{p['name']} has {len(new)} new models", f"{p['name']} 有 {len(new)} 个新模型"),
                                         {"provider_id": p["id"], "ids": new[:50]})
            out.append({"provider_id": p["id"], "name": p["name"], "new": len(new), "gone": gone})
        return out

    # ------------------------------------------------------- local models
    async def _fetch(self, url: str, *, params: dict | None = None, accept: str = "") -> httpx.Response:
        """Reach public sites other than GitHub (Ollama, Hugging Face). Also gated by the outbound-call master switch, read-only."""
        self._allowed()
        headers = {"User-Agent": "team-agent-updater"}
        if accept:
            headers["Accept"] = accept
        try:
            async with httpx.AsyncClient(timeout=20, transport=self._transport, follow_redirects=True, headers=headers) as c:
                return await c.get(url, params=params)
        except httpx.HTTPError as e:
            raise GitHubError(i18n.pick_now(f"Cannot reach {url.split('/')[2]}: {type(e).__name__}", f"无法连接 {url.split('/')[2]}:{type(e).__name__}")) from None

    async def probe_ollama(self, tag: str) -> dict:
        """Ask the Ollama registry to confirm a model really exists and read its download size (the
        sum of its layers). No weights are downloaded."""
        if not lm.valid_tag(tag):
            raise GitHubError(i18n.pick_now("That model tag is not valid (for example qwen3.8:27b)", "型号标签不合法(例如 qwen3.8:27b)"), 400)
        name, _, ver = tag.partition(":")
        ns, _, model = name.rpartition("/")
        path = f"{ns or 'library'}/{model}/manifests/{ver or 'latest'}"
        r = await self._fetch(f"{OLLAMA_REGISTRY}/{path}", accept="application/vnd.docker.distribution.manifest.v2+json")
        if r.status_code in (404, 400):
            return {"tag": tag, "exists": False, "size_gb": None}
        if r.status_code >= 400:
            raise GitHubError(i18n.pick_now(f"The Ollama registry returned {r.status_code}", f"Ollama 注册表返回 {r.status_code}"))
        try:
            size = lm.manifest_size_gb(r.json())
        except ValueError:
            size = None
        return {"tag": tag, "exists": True, "size_gb": size}

    async def _ollama_new(self, known: set[str], out: list[dict], errors: list[str]) -> None:
        r = await self._fetch(OLLAMA_LIBRARY)
        if r.status_code >= 400:
            raise GitHubError(i18n.pick_now(f"ollama.com returned {r.status_code}", f"ollama.com 返回 {r.status_code}"))
        names = [n for n in lm.parse_library_names(r.text)[:40] if n not in known and not lm.NON_CHAT_RE.search(n)]
        if not names:
            return
        sem = asyncio.Semaphore(6)

        async def one(n: str) -> dict | None:
            async with sem:
                try:
                    pr = await self.probe_ollama(f"{n}:latest")
                except GitHubError as e:
                    errors.append(i18n.pick_now(f"Probing {n}: {e}", f"探测 {n}: {e}"))
                    return None
            if not pr["exists"] or not pr["size_gb"]:
                return None  # cloud-only release (there are no local weights to download)
            return {"source": "ollama", "name": n, "tag": f"{n}:latest", "size_gb": pr["size_gb"],
                    "desc": i18n.pick_now("New models in the Ollama library", "Ollama 模型库里的新模型"), "url": f"https://ollama.com/library/{n}"}

        for c in await asyncio.gather(*(one(n) for n in names)):
            if c and len([x for x in out if x["source"] == "ollama"]) < MAX_NEW_PER_SOURCE:
                out.append(c)

    async def _successors(self, cat: lm.LocalCatalog, known: set[str], out: list[dict], errors: list[str]) -> None:
        guesses: dict[str, str] = {}
        for base in cat.tracked_bases():
            for g in lm.successor_names(base):
                if g not in known:
                    guesses.setdefault(g, base)
        sem = asyncio.Semaphore(6)

        async def one(g: str, base: str) -> dict | None:
            async with sem:
                try:
                    pr = await self.probe_ollama(f"{g}:latest")
                except GitHubError as e:
                    errors.append(i18n.pick_now(f"Probing {g}: {e}", f"探测 {g}: {e}"))
                    return None
            if not pr["exists"] or not pr["size_gb"]:
                return None
            return {"source": "successor", "name": g, "tag": f"{g}:latest", "size_gb": pr["size_gb"], "replaces": base,
                    "desc": i18n.pick_now(f"A new generation of {base}", f"{base} 的新一代"), "url": f"https://ollama.com/library/{g}"}

        for c in await asyncio.gather(*(one(g, b) for g, b in guesses.items())):
            if c:
                out.append(c)

    async def _hf_new(self, cat: lm.LocalCatalog, out: list[dict], errors: list[str]) -> None:
        since = cat.version[:10]
        found: list[dict] = []
        for author in cat.watch()["hf_authors"]:
            try:
                r = await self._fetch(HF_API, params={"author": author, "sort": "createdAt", "direction": "-1", "limit": "8"})
                if r.status_code >= 400:
                    continue
                items = r.json()
            except (GitHubError, ValueError) as e:
                errors.append(f"Hugging Face {author}: {e}")
                continue
            n = 0
            for it in items if isinstance(items, list) else []:
                mid = str(it.get("id", ""))
                if (str(it.get("createdAt", ""))[:10] <= since or it.get("pipeline_tag") not in ("text-generation", "image-text-to-text", "any-to-any")
                        or QUANT_RE.search(mid) or lm.NON_CHAT_RE.search(mid) or n >= 2):
                    continue
                lic = next((t.split(":", 1)[1] for t in it.get("tags", []) if str(t).startswith("license:")), "")
                found.append({"source": "hf", "name": mid, "tag": None, "size_gb": None, "license": lic,
                              "desc": i18n.pick_now(f"New on Hugging Face ({str(it.get('createdAt', ''))[:10]})", f"Hugging Face 上新发布({str(it.get('createdAt', ''))[:10]})"), "url": f"https://huggingface.co/{mid}"})
                n += 1
        out.extend(found[:MAX_NEW_PER_SOURCE])

    async def _github_new(self, cat: lm.LocalCatalog, out: list[dict], errors: list[str]) -> None:
        since = cat.version[:10]
        found: list[dict] = []
        for org in cat.watch()["github_orgs"]:
            try:
                data = await self._get(f"/orgs/{org}/repos", {"sort": "created", "direction": "desc", "per_page": 5})
            except GitHubError as e:
                if e.kind == "not_found":
                    continue          # the org on the watchlist does not exist on GitHub (renamed): skip it silently, it is not a failure
                errors.append(f"GitHub {org}: {e}")
                if e.kind == "rate_limit":
                    break
                continue
            for it in [x for x in data if isinstance(x, dict)][:5]:
                if it.get("fork") or it.get("archived") or str(it.get("created_at", ""))[:10] <= since:
                    continue
                found.append({"source": "github", "name": it["full_name"], "tag": None, "size_gb": None,
                              "desc": (it.get("description") or "")[:200], "url": it.get("html_url", ""),
                              "stars": it.get("stargazers_count", 0)})
        try:
            q = f"llm weights in:description created:>{since} stars:>300"
            data = await self._get("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 5})
            for it in data.get("items", []):
                if not any(f["name"] == it["full_name"] for f in found):
                    found.append({"source": "github", "name": it["full_name"], "tag": None, "size_gb": None,
                                  "desc": (it.get("description") or "")[:200], "url": it.get("html_url", ""),
                                  "stars": it.get("stargazers_count", 0)})
        except GitHubError as e:
            errors.append(i18n.pick_now(f"GitHub search: {e}", f"GitHub 搜索: {e}"))
        out.extend(found[: MAX_NEW_PER_SOURCE * 2])

    async def _ollama_version_note(self) -> dict | None:
        """Warn when the local Ollama is older than the latest GitHub release (new models often need
        a newer Ollama). It only warns, it never upgrades by itself."""
        base = next((p["base_url"] for p in self.store.list_providers() if p["kind"] == "ollama"), "") or "http://127.0.0.1:11434"
        try:
            async with httpx.AsyncClient(timeout=3, transport=self._transport) as c:
                local = str((await c.get(base.rstrip("/") + "/api/version")).json().get("version", ""))
        except (httpx.HTTPError, ValueError):
            return None
        rel = await self._get("/repos/ollama/ollama/releases/latest")
        latest = str(rel.get("tag_name", ""))
        if local and latest and parse_version(latest) > parse_version(local):
            return {"local": local, "latest": latest, "url": rel.get("html_url", "https://github.com/ollama/ollama/releases")}
        return {"local": local, "latest": latest, "url": rel.get("html_url", ""), "ok": True}

    async def check_local_models(self) -> dict:
        """Find new open-source models and new versions of series already known: the newest list in
        the Ollama library, probing for higher version numbers in a series (the registry confirms
        they really exist), new repos from the main vendors on Hugging Face and GitHub, and whether
        the local Ollama is behind.
        A discovery is only a reminder: it never downloads several GB of weights on its own, the
        model is added to the list only after you click "add to recommended" and confirm."""
        self._allowed()
        cat = self.store.local_catalog
        known = cat.known_bases()
        cands: list[dict] = []
        errors: list[str] = []
        for fn in (
            lambda: self._ollama_new(known, cands, errors),
            lambda: self._successors(cat, known, cands, errors),
            lambda: self._hf_new(cat, cands, errors),
            lambda: self._github_new(cat, cands, errors),
        ):
            try:
                await fn()
            except GitHubError as e:
                errors.append(str(e))
        uniq: dict[str, dict] = {}
        for c in cands:
            uniq.setdefault(c["name"], c)
        for c in uniq.values():
            label = {"ollama": i18n.pick_now("New Ollama models", "Ollama 新模型"), "successor": i18n.pick_now("New generation", "新一代"), "hf": i18n.pick_now("New Hugging Face models", "Hugging Face 新模型"), "github": i18n.pick_now("New GitHub repositories", "GitHub 新仓库")}[c["source"]]
            size = i18n.pick_now(f"(about {c['size_gb']} GB)", f"(约 {c['size_gb']} GB)") if c.get("size_gb") else ""
            self.store.upsert_update("localmodel", c["name"], f"{label}:{c['name']}{size}", c)
        ollama = None
        try:
            ollama = await self._ollama_version_note()
            if ollama and not ollama.get("ok"):
                self.store.upsert_update("localmodel", "ollama-release", i18n.pick_now(f"A new Ollama version is available: {ollama['latest']} (you have {ollama['local']})", f"Ollama 有新版本 {ollama['latest']}(本机 {ollama['local']})"),
                                         {"source": "ollama-release", "name": "ollama-release", **ollama})
        except GitHubError as e:
            errors.append(i18n.pick_now(f"Ollama version: {e}", f"Ollama 版本: {e}"))
        return {"found": len(uniq), "candidates": list(uniq.values()), "errors": errors, "ollama": ollama, "catalog": cat.version}

    async def check_local_catalog(self, apply: bool = False) -> dict:
        """Update of the local recommended catalog itself: if local_models.json in the app_repo repo
        has a higher version it replaces the current one (after validation). The catalog is only text data."""
        cur = self.store.local_catalog
        repo = self.store.get_settings()["app_repo"]
        if not repo:
            return {"configured": False, "current": cur.version}
        f = await self.file(repo, LOCAL_CATALOG_PATH)
        try:
            data = json.loads(f["content"])
        except ValueError:
            raise GitHubError(i18n.pick_now("The local model catalog is not valid JSON", "本地模型目录不是合法 JSON")) from None
        err = lm.validate(data)
        if err:
            raise GitHubError(i18n.pick_now(f"The local model catalog has the wrong shape: {err}", f"本地模型目录格式不对:{err}"))
        newer = is_newer(data["version"], cur.version)
        info = {"configured": True, "current": cur.version, "latest": str(data["version"]), "available": newer, "applied": False}
        if newer and apply:
            cur.save_override(data)
            info["applied"] = True
            self.store.resolve_updates("localcatalog", "localcatalog")
        elif newer:
            self.store.upsert_update("localcatalog", "localcatalog", i18n.pick_now(f"A new local model catalog is available: {data['version']}", f"本地模型目录有新版本 {data['version']}"), info)
        return info

    async def check_all(self, auto_apply: bool = True) -> dict:
        """Check every source in one pass. A failure in one item does not affect the others, errors are written into the result."""
        if self.checking:
            return {"busy": True}
        self.checking = True
        cfg = self.store.get_settings()
        result: dict[str, Any] = {"at": time.time(), "errors": []}
        try:
            self._allowed()
            for key, fn in (
                ("app", self.check_app),
                ("catalog", lambda: self.check_catalog(apply=auto_apply)),
                ("skills", lambda: self.check_sources("skill", auto_apply and cfg["auto_update_skills"])),
                ("plugins", lambda: self.check_sources("plugin")),
                ("models", self.check_models),
                ("local_catalog", lambda: self.check_local_catalog(apply=auto_apply)),
                ("local_models", self.check_local_models),
            ):
                try:
                    result[key] = await fn()
                except GitHubError as e:
                    result["errors"].append(f"{key}: {e}")
                    result[key] = None
        except GitHubError as e:
            result["errors"].append(str(e))
        finally:
            self.checking = False
        self.store.set_meta("last_update_check", json.dumps(result, ensure_ascii=False, default=str))
        return result

    async def run_forever(self) -> None:
        """Periodic background check. It runs once 20 seconds after startup, then on the configured
        interval. Any exception is swallowed so the main program is unaffected."""
        await asyncio.sleep(20)
        while True:
            try:
                cfg = self.store.get_settings()
                if cfg["auto_check_updates"] and cfg["external_calls_enabled"]:
                    await self.check_all(auto_apply=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            hours = max(1, int(self.store.get_settings()["update_interval_hours"]))
            await asyncio.sleep(hours * 3600)
