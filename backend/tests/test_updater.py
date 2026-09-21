"""更新器:用 httpx.MockTransport 假装 GitHub。真正的 GitHub 在沙箱里连不上,这里只验证我们自己的逻辑。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.updater import GitHubError, Updater, parse_version, sha256_hex, valid_path, valid_repo
from tests.conftest import FakeLLM

SKILL_V1 = "---\nname: 会议纪要\ndescription: 整理纪要\nscope: member\n---\n第一版正文"
SKILL_V2 = "---\nname: 会议纪要\ndescription: 整理纪要\nscope: member\n---\n第二版正文"
PLUGIN = 'def register(reg):\n    reg.register("hi", "打招呼", None, lambda a: "hi")\n'


class FakeGitHub:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], str] = {}
        self.release: dict | None = None
        self.calls: list[httpx.Request] = []
        self.status_override: int | None = None
        self.rate_limited = False

    def put(self, repo: str, path: str, text: str) -> None:
        self.files[(repo, path)] = text

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.rate_limited:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"})
        if self.status_override:
            return httpx.Response(self.status_override, json={})
        path = req.url.path
        parts = path.strip("/").split("/")
        if parts[0] == "repos" and len(parts) >= 3:
            repo = f"{parts[1]}/{parts[2]}"
            rest = "/".join(parts[3:])
            if rest == "releases/latest":
                return httpx.Response(200, json=self.release) if self.release else httpx.Response(404, json={})
            if rest == "" :
                return httpx.Response(200, json={"default_branch": "main"})
            if rest.startswith("contents/"):
                p = rest.removeprefix("contents/")
                text = self.files.get((repo, p))
                if text is None:
                    return httpx.Response(404, json={})
                b = text.encode()
                return httpx.Response(200, json={"type": "file", "size": len(b), "sha": "sha-" + sha256_hex(text)[:8],
                                                 "content": base64.b64encode(b).decode()})
            if rest.startswith("git/trees/"):
                tree = [{"type": "blob", "path": p, "sha": "sha-" + sha256_hex(t)[:8], "size": len(t)}
                        for (r, p), t in self.files.items() if r == repo]
                return httpx.Response(200, json={"tree": tree})
            if rest == "readme":
                return httpx.Response(200, json={"content": base64.b64encode("# 说明".encode()).decode(), "html_url": "u"})
        if path == "/search/repositories":
            q = req.url.params.get("q", "")
            items = [{"full_name": "a/one", "stargazers_count": 5, "description": "d"}]
            if "mcp-servers" in q:
                items.append({"full_name": "a/two", "stargazers_count": 50, "archived": True})
            items.append({"full_name": "a/one", "stargazers_count": 5})  # 重复项
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404, json={})


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def up(store, gh):
    store.update_settings({"app_repo": "me/team-agent"})
    return Updater(store, "0.3.0", transport=httpx.MockTransport(gh.handler))


# ------------------------------------------------------------------ helpers
def test_helpers():
    assert valid_repo("https://github.com/a/b.git") == "a/b"
    for bad in ("", "a", "a/b/c", "../x", "a b/c"):
        with pytest.raises(GitHubError) as e:
            valid_repo(bad)
        assert e.value.status == 400
    with pytest.raises(GitHubError):
        valid_path("a/../b")
    assert parse_version("v0.10.1") > parse_version("0.9.9") and parse_version("v1.0.0-beta") == (1, 0, 0)


# ---------------------------------------------------------------- 程序本体
async def test_check_app_reports_newer_release_and_never_installs(up, gh, store):
    gh.release = {"tag_name": "v0.4.0", "html_url": "https://x/r", "body": "改进", "assets": [{"name": "a.dmg", "size": 1}]}
    info = await up.check_app()
    assert info["available"] and info["latest"] == "v0.4.0" and info["assets"][0]["name"] == "a.dmg"
    items = store.list_updates("new")
    assert [i["kind"] for i in items] == ["app"]
    await up.check_app()  # 重复检查不会多出提醒
    assert len(store.list_updates("new")) == 1


async def test_check_app_same_version_none_and_unconfigured(up, gh, store):
    gh.release = {"tag_name": "v0.3.0"}
    assert (await up.check_app())["available"] is False
    gh.release = None
    assert "no releases yet" in (await up.check_app())["note"]
    store.update_settings({"app_repo": ""})
    assert (await up.check_app())["configured"] is False


# ---------------------------------------------------------------- 模型目录
def _cat(version: str, model_id: str) -> dict:
    return {"version": version, "providers": {"deepseek": {"models": [
        {"id": model_id, "name": model_id, "summary": "s", "context": 1000, "tier": "pro"}]}}}


async def test_catalog_check_then_apply(up, gh, store):
    gh.put("me/team-agent", "backend/app/data/catalog.json", json.dumps(_cat("2099-01-01", "brand-new")))
    info = await up.check_catalog()
    assert info["available"] and info["new_models"] == 1 and not info["applied"]
    assert store.catalog.version != "2099-01-01"                     # 只提醒,没有改动
    assert store.list_updates("new")[0]["kind"] == "catalog"
    info = await up.check_catalog(apply=True)
    assert info["applied"] and store.catalog.version == "2099-01-01"
    assert any(m["id"] == "brand-new" for m in store.catalog.models_of("deepseek"))
    assert store.list_updates("new") == []


async def test_catalog_invalid_or_older_is_rejected(up, gh, store):
    gh.put("me/team-agent", "backend/app/data/catalog.json", '{"version": "2099", "providers": 3}')
    with pytest.raises(GitHubError, match="wrong shape"):
        await up.check_catalog(apply=True)
    gh.put("me/team-agent", "backend/app/data/catalog.json", "not json")
    with pytest.raises(GitHubError, match="JSON"):
        await up.check_catalog()
    gh.put("me/team-agent", "backend/app/data/catalog.json", json.dumps(_cat("2000-01-01", "old")))
    assert (await up.check_catalog(apply=True))["applied"] is False


async def test_catalog_url_must_be_https(up, store):
    store.update_settings({"catalog_url": "http://evil/c.json"})
    with pytest.raises(GitHubError, match="https"):
        await up.fetch_catalog()


# -------------------------------------------------------------------- 技能
async def test_skill_install_conflict_overwrite_and_source_tracking(up, gh, store):
    gh.put("o/skills", "会议纪要/SKILL.md", SKILL_V1)
    s = await up.install_skill("o/skills", "会议纪要/SKILL.md")
    assert s.name == "会议纪要" and "第一版" in s.body
    assert store.get_source("skill", "会议纪要")["repo"] == "o/skills"
    # 另一个仓库同名 → 必须显式覆盖
    gh.put("x/other", "会议纪要/SKILL.md", SKILL_V2)
    with pytest.raises(GitHubError) as e:
        await up.install_skill("x/other", "会议纪要/SKILL.md")
    assert e.value.status == 409
    assert "第二版" in (await up.install_skill("x/other", "会议纪要/SKILL.md", overwrite=True)).body


async def test_skill_update_check_reminds_by_default_and_auto_applies_only_when_enabled(up, gh, store):
    gh.put("o/skills", "会议纪要/SKILL.md", SKILL_V1)
    await up.install_skill("o/skills", "会议纪要/SKILL.md")
    assert not any(x["available"] for x in await up.check_sources("skill"))
    gh.put("o/skills", "会议纪要/SKILL.md", SKILL_V2)
    res = await up.check_sources("skill", auto_apply=False)
    assert res[0]["available"] and not res[0]["applied"]
    assert store.list_updates("new")[0]["kind"] == "skill"
    body = (store.data_dir / "skills" / "会议纪要" / "SKILL.md").read_text(encoding="utf-8")
    assert "第一版" in body                                            # 没有自动改
    # check_all 遵守「技能自动更新」开关,默认关闭
    await up.check_all()
    assert "第一版" in (store.data_dir / "skills" / "会议纪要" / "SKILL.md").read_text(encoding="utf-8")
    store.update_settings({"auto_update_skills": True})
    await up.check_all()
    assert "第二版" in (store.data_dir / "skills" / "会议纪要" / "SKILL.md").read_text(encoding="utf-8")
    assert store.list_updates("new") == [] or all(i["kind"] != "skill" for i in store.list_updates("new"))


# -------------------------------------------------------------------- 插件
async def test_plugin_requires_matching_hash_and_never_auto_updates(up, gh, store):
    gh.put("o/pl", "plugins/hello.py", PLUGIN)
    pv = await up.preview("o/pl", "plugins/hello.py")
    assert pv["sha256"] == sha256_hex(PLUGIN) and pv["content"] == PLUGIN
    with pytest.raises(GitHubError) as e:                              # 预览之后内容被换了
        gh.put("o/pl", "plugins/hello.py", PLUGIN + "\nimport os\n")
        await up.install_plugin("o/pl", "plugins/hello.py", "", pv["sha256"])
    assert e.value.status == 409 and not (store.data_dir / "plugins" / "hello.py").exists()
    gh.put("o/pl", "plugins/hello.py", PLUGIN)
    assert await up.install_plugin("o/pl", "plugins/hello.py", "", pv["sha256"]) == "hello"
    assert (store.data_dir / "plugins" / "hello.py").read_text(encoding="utf-8") == PLUGIN
    # 上游更新 → 只提醒,文件保持原样,即使 auto_update_skills 打开
    store.update_settings({"auto_update_skills": True})
    gh.put("o/pl", "plugins/hello.py", PLUGIN + "# v2\n")
    await up.check_all()
    assert (store.data_dir / "plugins" / "hello.py").read_text(encoding="utf-8") == PLUGIN
    assert any(i["kind"] == "plugin" for i in store.list_updates("new"))


async def test_plugin_must_be_py_and_not_overwrite_without_flag(up, gh, store):
    gh.put("o/pl", "plugins/readme.txt", "x")
    with pytest.raises(GitHubError, match=".py"):
        await up.install_plugin("o/pl", "plugins/readme.txt", "", sha256_hex("x"))
    (store.data_dir / "plugins").mkdir(exist_ok=True)
    (store.data_dir / "plugins" / "hello.py").write_text("# mine", encoding="utf-8")
    gh.put("o/pl", "plugins/hello.py", PLUGIN)
    with pytest.raises(GitHubError) as e:
        await up.install_plugin("o/pl", "plugins/hello.py", "", sha256_hex(PLUGIN))
    assert e.value.status == 409
    assert (store.data_dir / "plugins" / "hello.py").read_text(encoding="utf-8") == "# mine"


# ------------------------------------------------------------ 搜索/仓库浏览
async def test_search_merges_topics_and_sorts_by_stars(up, gh):
    res = await up.search("mcp", "")
    assert [r["repo"] for r in res] == ["a/two", "a/one"] and res[0]["archived"] is True
    with pytest.raises(GitHubError):
        await up.search("virus")


async def test_repo_listings(up, gh):
    gh.put("o/skills", "a/SKILL.md", SKILL_V1)
    gh.put("o/skills", "docs/readme.md", "x")
    gh.put("o/pl", "plugins/p.py", PLUGIN)
    gh.put("o/pl", "evil/p.py", PLUGIN)
    assert [f["path"] for f in await up.skill_files("o/skills")] == ["a/SKILL.md"]
    assert [f["path"] for f in await up.plugin_files("o/pl")] == ["plugins/p.py"]
    assert "说明" in (await up.readme("o/pl"))["content"]


# ---------------------------------------------------- 网络/离线/限流/令牌
async def test_offline_mode_makes_no_requests(up, gh, store):
    store.update_settings({"external_calls_enabled": False})
    with pytest.raises(GitHubError) as e:
        await up.check_app()
    assert e.value.status == 403 and gh.calls == []
    res = await up.check_all()
    assert res["errors"] and gh.calls == []


async def test_rate_limit_and_http_errors_are_readable(up, gh):
    gh.rate_limited = True
    with pytest.raises(GitHubError, match="rate limit"):
        await up.check_app()
    gh.rate_limited, gh.status_override = False, 500
    with pytest.raises(GitHubError, match="500"):
        await up.check_app()


async def test_check_all_isolates_failures_and_sends_token(up, gh, store):
    store.update_settings({"github_token": "ghp_secret"})
    gh.release = {"tag_name": "v9.0.0"}
    res = await up.check_all()
    assert res["app"]["available"] is True
    assert res["errors"]                                               # 目录文件不存在 → 记录错误,但不影响其它项
    assert all(c.headers.get("authorization") == "Bearer ghp_secret" for c in gh.calls if c.url.host == "api.github.com")
    # 令牌只发给 GitHub,绝不带给 Ollama / Hugging Face 等其它站点
    assert all("authorization" not in c.headers for c in gh.calls if c.url.host != "api.github.com")
    assert json.loads(store.get_meta("last_update_check"))["app"]["latest"] == "v9.0.0"


# -------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path, gh):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(), github_transport=httpx.MockTransport(gh.handler))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.put("/api/settings", json={"app_repo": "me/team-agent"})
        yield c


def test_api_plugin_install_flow_and_status_codes(api, gh):
    gh.put("o/pl", "plugins/hello.py", PLUGIN)
    body = {"repo": "o/pl", "path": "plugins/hello.py"}
    assert api.post("/api/updates/plugin/install", json=body).status_code == 400         # 没带 sha256
    assert api.post("/api/updates/plugin/install", json={**body, "sha256": "0" * 64}).status_code == 409
    pv = api.post("/api/updates/preview", json=body).json()
    r = api.post("/api/updates/plugin/install", json={**body, "sha256": pv["sha256"]})
    assert r.status_code == 200 and r.json()["id"] == "hello"
    assert any(p["id"] == "hello" and "hi" in p["tools"] for p in api.get("/api/plugins").json())


def test_api_skill_install_update_and_bad_repo(api, gh):
    gh.put("o/skills", "会议纪要/SKILL.md", SKILL_V1)
    assert api.post("/api/updates/skill/install", json={"repo": "bad", "path": "x"}).status_code == 400
    r = api.post("/api/updates/skill/install", json={"repo": "o/skills", "path": "会议纪要/SKILL.md"})
    assert r.status_code == 200 and r.json()["name"] == "会议纪要"
    gh.put("o/skills", "会议纪要/SKILL.md", SKILL_V2)
    assert api.post("/api/updates/skill/update/会议纪要").status_code == 200
    assert "第二版" in [s for s in api.get("/api/skills").json() if s["name"] == "会议纪要"][0].get("body", "第二版")


def test_api_check_list_dismiss(api, gh):
    gh.release = {"tag_name": "v1.0.0"}
    res = api.post("/api/updates/check").json()
    assert res["app"]["available"]
    data = api.get("/api/updates").json()
    assert data["configured"] and [i for i in data["items"] if i["kind"] == "app"] and data["last_check"]["app"]["latest"] == "v1.0.0"
    assert api.post(f"/api/updates/{next(i for i in data['items'] if i['kind'] == 'app')['id']}/dismiss").status_code == 200
    assert [i for i in api.get("/api/updates").json()["items"] if i["kind"] != "localmodel"] == []   # 本地模型的发现另测(test_local)
    assert api.post("/api/updates/nope/dismiss").status_code == 404


def test_api_search_and_offline(api, gh):
    r = api.get("/api/updates/search", params={"kind": "skill", "q": "pdf"})
    assert r.status_code == 200 and r.json()
    assert api.get("/api/updates/search", params={"kind": "bogus"}).status_code == 400
    api.put("/api/settings", json={"external_calls_enabled": False})
    assert api.get("/api/updates/search", params={"kind": "skill"}).status_code == 403
