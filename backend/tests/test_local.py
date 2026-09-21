"""本地模型:推荐目录 + 硬件评估、自建 DeepSeek 预设、兜底顺序、新模型发现(Ollama / Hugging Face / GitHub,全部用假服务器)。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import local_models as lm
from app import strengths
from app.main import create_app
from app.router import litellm_params
from app.updater import Updater
from tests.conftest import FakeLLM

GB = 1024**3
HW_SMALL = {"ram_gb": 8.0, "disk_free_gb": 100.0, "accel": "none"}
HW_BIG = {"ram_gb": 512.0, "disk_free_gb": 2000.0, "accel": "metal"}


# ------------------------------------------------------------------ 假的外部世界
class FakeWeb:
    """Ollama 模型库页面 + 注册表 + Hugging Face + GitHub + 本机 Ollama 的合体。"""

    def __init__(self) -> None:
        self.library = ["qwen3.9", "qwen3.8", "bge-embed-x", "cloudonly-1", "newlab-2"]
        self.registry = {"qwen3.9:latest": 21, "gemma5:latest": 9.5, "newlab-2:latest": 4.2, "qwen3.8:latest": 18}
        self.hf: dict[str, list[dict]] = {}
        self.gh_orgs: dict[str, list[dict]] = {}
        self.gh_search: list[dict] = []
        self.ollama_release = "v0.9.0"
        self.local_ollama = "0.9.0"
        self.catalog_file: dict | None = None
        self.calls: list[httpx.Request] = []
        self.library_status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if host == "ollama.com":
            if self.library_status != 200:
                return httpx.Response(self.library_status, text="")
            return httpx.Response(200, text="".join(f'<a href="/library/{n}">{n}</a>' for n in self.library))
        if host == "registry.ollama.ai":
            parts = path.strip("/").split("/")          # v2 library <name> manifests <tag>
            key = f"{parts[2]}:{parts[4]}"
            if key in self.registry:
                return httpx.Response(200, json={"layers": [{"size": int(self.registry[key] * GB)}]})
            return httpx.Response(404, json={})
        if host == "huggingface.co":
            return httpx.Response(200, json=self.hf.get(req.url.params.get("author", ""), []))
        if host == "127.0.0.1":
            return httpx.Response(200, json={"version": self.local_ollama})
        if host == "api.github.com":
            if path.startswith("/orgs/"):
                org = path.split("/")[2]
                return httpx.Response(200, json=self.gh_orgs[org]) if org in self.gh_orgs else httpx.Response(404, json={})
            if path == "/search/repositories":
                return httpx.Response(200, json={"items": self.gh_search})
            if path == "/repos/ollama/ollama/releases/latest":
                return httpx.Response(200, json={"tag_name": self.ollama_release, "html_url": "https://gh/ollama"})
            if path.endswith("contents/backend/app/data/local_models.json") and self.catalog_file is not None:
                b = json.dumps(self.catalog_file).encode()
                return httpx.Response(200, json={"type": "file", "size": len(b), "sha": "s", "content": base64.b64encode(b).decode()})
        return httpx.Response(404, json={})


@pytest.fixture
def web():
    w = FakeWeb()
    w.hf = {
        "Qwen": [
            {"id": "Qwen/Qwen3.9-27B", "createdAt": "2026-09-25T00:00:00Z", "pipeline_tag": "image-text-to-text", "tags": ["license:apache-2.0"]},
            {"id": "Qwen/Qwen3.9-27B-FP8", "createdAt": "2026-09-25T00:00:00Z", "pipeline_tag": "text-generation", "tags": []},   # 量化衍生:不报
            {"id": "Qwen/OldModel", "createdAt": "2026-01-01T00:00:00Z", "pipeline_tag": "text-generation", "tags": []},          # 早于目录:不报
            {"id": "Qwen/Qwen3-Embedding", "createdAt": "2026-09-26T00:00:00Z", "pipeline_tag": "feature-extraction", "tags": []},
        ]
    }
    w.gh_orgs = {
        "deepseek-ai": [
            {"full_name": "deepseek-ai/NewThing", "created_at": "2026-09-22T00:00:00Z", "description": "d", "html_url": "u", "stargazers_count": 9},
            {"full_name": "deepseek-ai/Forked", "created_at": "2026-09-22T00:00:00Z", "fork": True},
            {"full_name": "deepseek-ai/Old", "created_at": "2025-01-01T00:00:00Z"},
        ]
    }
    return w


@pytest.fixture
def api(tmp_path, web):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="好的"), github_transport=httpx.MockTransport(web.handler))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


@pytest.fixture
def up(store, web):
    return Updater(store, "0.3.0", transport=httpx.MockTransport(web.handler))


# ------------------------------------------------------------------ 硬件评估
def test_assess_fit_disk_and_slow():
    assert lm.assess(4.7, HW_SMALL) == {"fit": "tight", "disk_ok": True, "slow": False}     # 4.7 > 8*0.5,但 <= 8*0.8
    assert lm.assess(1.1, HW_SMALL)["fit"] == "ok"
    assert lm.assess(9.0, HW_SMALL) == {"fit": "no", "disk_ok": True, "slow": True}
    assert lm.assess(404, HW_SMALL) == {"fit": "no", "disk_ok": False, "slow": True}
    big = lm.assess(404, HW_BIG)
    assert big["fit"] == "tight" and big["disk_ok"] and not big["slow"]                   # 404GB 放进 512GB 内存:能装,但很紧
    assert lm.assess(1, {"ram_gb": None, "disk_free_gb": None, "accel": "unknown"}) == {"fit": "unknown", "disk_ok": True, "slow": False}
    assert lm.assess(0, HW_SMALL)["fit"] == "unknown"                                      # 大小未知不下结论


def test_hardware_is_sane_on_this_machine():
    hw = lm.hardware()
    assert hw["accel"] in ("metal", "cuda", "none", "unknown")
    assert hw["ram_gb"] is None or hw["ram_gb"] > 0


# ------------------------------------------------------------------ 目录内容
def test_shipped_catalog_is_valid_broad_and_honest():
    data = json.loads(lm.SHIPPED.read_text(encoding="utf-8"))
    assert lm.validate(data) is None
    vendors = {f["vendor"] for f in data["families"]}
    assert len(vendors) >= 12 and not vendors <= {"阿里 · 通义千问"}                          # 不只有千问
    tags = {m["tag"] for f in data["families"] for m in f["models"]}
    for must in ("qwen3.8:27b", "gemma4:e4b", "gpt-oss:20b", "muse-glimmer:30b", "granite4.2:8b", "deepseek-r1:7b",
                 "glm-4.7-flash", "ministral-3:8b", "phi4:14b", "nemotron-3.5-lightning:30b"):
        assert must in tags, must
    assert "qwen2.5:7b" not in tags and not any(t.startswith(("llama2", "llama3:", "gemma2")) for t in tags)   # 只放最新一代
    for f in data["families"]:
        assert set(f["strengths"]) <= set(strengths.TAG_IDS), f["id"]
        assert f["runtime"] == "ollama" and f["track"], f["id"]
    ds = next(f for f in data["families"] if f["id"] == "deepseek")
    assert "不是 V3" in ds["desc"] and "云端" in ds["desc"]                                   # 蒸馏版要讲清楚,V4 只有云端版
    v4 = data["selfhost"][0]
    assert v4["license"] == "MIT" and v4["models"][0]["id"] == "deepseek-ai/DeepSeek-V4-Flash"
    assert "deepseek-ai/DeepSeek-V3" in {m["id"] for m in v4["models"]}                      # V3 仍可自建接入
    assert {c["name"] for c in data["cloud_only"]} >= {"kimi-k3", "deepseek-v4-flash"}


def test_validate_rejects_bad_catalogs():
    good = json.loads(lm.SHIPPED.read_text(encoding="utf-8"))
    assert lm.validate({}) and lm.validate({"version": "1"}) and lm.validate({"version": "1", "families": []})
    bad = json.loads(json.dumps(good))
    bad["families"][0]["models"][0]["tag"] = "rm -rf /;x"
    assert "标签" in lm.validate(bad)
    bad = json.loads(json.dumps(good))
    bad["families"][0]["models"][0]["size_gb"] = "18"
    assert "size_gb" in lm.validate(bad)


def test_pure_helpers():
    assert lm.valid_tag("qwen3.8:27b") and lm.valid_tag("user/model:tag") and lm.valid_tag("deepseek-r1")
    assert not lm.valid_tag("") and not lm.valid_tag("a b") and not lm.valid_tag("../x") and not lm.valid_tag("x:") and not lm.valid_tag("A;B")
    assert lm.successor_names("qwen3.8") == ["qwen3.9", "qwen4"]
    assert lm.successor_names("gemma4") == ["gemma4.1", "gemma5"]
    assert lm.successor_names("glm-4.7-flash") == ["glm-4.8-flash", "glm-5-flash"]
    assert lm.successor_names("gpt-oss") == []
    assert lm.parse_library_names('<a href="/library/a">x</a><a href="/library/b.c-d">y</a><a href="/library/a">z</a><a href="/other/z">') == ["a", "b.c-d"]
    assert lm.manifest_size_gb({"layers": [{"size": GB}, {"size": GB // 2}]}) == 1.5
    assert lm.manifest_size_gb({"layers": []}) is None and lm.manifest_size_gb("x") is None
    assert lm.NON_CHAT_RE.search("bge-embed-x") and not lm.NON_CHAT_RE.search("qwen3.9")


def test_catalog_view_installed_extras_and_selfhost(store):
    cat = store.local_catalog
    v = cat.view({"qwen3.8:27b", "deepseek-r1:7b:latest"}, HW_SMALL)
    fam = {f["id"]: f for f in v["families"]}
    q = {m["tag"]: m for m in fam["qwen"]["models"]}
    assert q["qwen3.8:27b"]["installed"] and not q["qwen3.5:9b"]["installed"]
    assert q["qwen3.8:27b"]["fit"] == "no" and q["qwen3.5:0.8b"]["fit"] == "ok"
    assert "extras" not in fam
    cat.add_extra("qwen3.9:latest", 21, "新一代")
    cat.add_extra("qwen3.9:latest", 21.04, "再加一次")                                          # 同一个型号不重复
    v = cat.view(set(), HW_SMALL)
    ex = next(f for f in v["families"] if f["id"] == "extras")
    assert [m["tag"] for m in ex["models"]] == ["qwen3.9:latest"] and ex["models"][0]["size_gb"] == 21.0
    assert "qwen3.9" in cat.known_bases()
    assert v["selfhost"][0]["models"][0]["fit"] == "no" and v["selfhost"][0]["models"][1]["fit"] == "unknown"   # 大小未知的不下结论
    assert cat.remove_extra("qwen3.9:latest") and not cat.remove_extra("qwen3.9:latest")
    with pytest.raises(ValueError):
        cat.add_extra("bad tag", 1)


def test_override_only_wins_when_newer(store):
    cat = store.local_catalog
    data = json.loads(lm.SHIPPED.read_text(encoding="utf-8"))
    data["version"] = "2020-01-01"
    cat.save_override(data)
    assert cat.source == "shipped"
    data["version"] = "2099-01-01"
    data["families"] = data["families"][:2]
    cat.save_override(data)
    assert cat.source == "override" and len(cat.data["families"]) == 2 and cat.version == "2099-01-01"


# ------------------------------------------------------------------ 发现新模型
async def test_discovery_finds_new_models_successors_hf_and_github(up, web, store):
    store.update_settings({"github_token": "ghp_secret"})
    res = await up.check_local_models()
    by = {c["name"]: c for c in res["candidates"]}
    assert by["qwen3.9"]["source"] == "ollama" and by["qwen3.9"]["size_gb"] == 21 and by["qwen3.9"]["tag"] == "qwen3.9:latest"
    assert by["newlab-2"]["source"] == "ollama" and by["newlab-2"]["size_gb"] == 4.2
    assert "qwen3.8" not in by                                     # 已收录
    assert "bge-embed-x" not in by                                 # 嵌入模型不是对话模型
    assert "cloudonly-1" not in by                                 # 注册表里没有本地权重
    # gemma4 → gemma5 版本递增探测命中(用注册表验证过)
    assert by["gemma5"]["source"] == "successor" and by["gemma5"]["replaces"] == "gemma4" and by["gemma5"]["size_gb"] == 9.5
    assert by["Qwen/Qwen3.9-27B"]["source"] == "hf" and by["Qwen/Qwen3.9-27B"]["license"] == "apache-2.0" and by["Qwen/Qwen3.9-27B"]["tag"] is None
    assert not any(n in by for n in ("Qwen/Qwen3.9-27B-FP8", "Qwen/OldModel", "Qwen/Qwen3-Embedding"))
    assert by["deepseek-ai/NewThing"]["source"] == "github"
    assert not any(n in by for n in ("deepseek-ai/Forked", "deepseek-ai/Old"))
    assert res["errors"] == []                                     # 关注名单里有的 org 在 GitHub 上不存在(改名):静默跳过,不算故障
    refs = {u["ref"] for u in store.list_updates("new") if u["kind"] == "localmodel"}
    assert {"qwen3.9", "gemma5", "Qwen/Qwen3.9-27B", "deepseek-ai/NewThing"} <= refs
    # 令牌只发给 api.github.com;全程只读
    assert all("authorization" not in c.headers for c in web.calls if c.url.host != "api.github.com")
    assert all(c.method == "GET" for c in web.calls)


async def test_discovery_is_quiet_when_nothing_new_and_respects_dismiss(up, web, store):
    await up.check_local_models()
    first = [u for u in store.list_updates("new") if u["kind"] == "localmodel"]
    assert first
    for u in first:
        store.set_update_status(u["id"], "dismissed")
    await up.check_local_models()
    assert [u for u in store.list_updates("new") if u["kind"] == "localmodel"] == []      # 忽略过的相同内容不再打扰
    web.registry["qwen3.9:latest"] = 22                                                      # 大小变了才算新内容
    await up.check_local_models()
    assert [u["ref"] for u in store.list_updates("new") if u["kind"] == "localmodel"] == ["qwen3.9"]


async def test_discovery_survives_broken_sources(up, web, store):
    web.library_status = 503
    res = await up.check_local_models()
    assert any("503" in e for e in res["errors"])
    assert any(c["source"] in ("successor", "hf", "github") for c in res["candidates"])    # 其它来源照常


async def test_ollama_version_note_only_when_behind(up, web, store):
    web.ollama_release = "v0.9.5"
    res = await up.check_local_models()
    assert res["ollama"]["latest"] == "v0.9.5" and res["ollama"]["local"] == "0.9.0" and not res["ollama"].get("ok")
    assert any(u["ref"] == "ollama-release" for u in store.list_updates("new"))
    web.local_ollama = "0.9.5"
    assert (await up.check_local_models())["ollama"]["ok"] is True


async def test_discovery_offline_makes_no_requests(up, web, store):
    store.update_settings({"external_calls_enabled": False})
    with pytest.raises(Exception, match="外呼已禁用"):
        await up.check_local_models()
    assert web.calls == []


async def test_probe_and_local_catalog_update(up, web, store):
    assert (await up.probe_ollama("qwen3.9")) == {"tag": "qwen3.9", "exists": True, "size_gb": 21.0}      # 不写版本 = latest
    assert (await up.probe_ollama("qwen3.9:latest")) == {"tag": "qwen3.9:latest", "exists": True, "size_gb": 21.0}
    assert (await up.probe_ollama("nope:1b"))["exists"] is False
    with pytest.raises(Exception, match="不合法"):
        await up.probe_ollama("bad tag;")
    # 目录更新:app_repo 里的 local_models.json 版本更大 → 校验后覆盖
    store.update_settings({"app_repo": "me/team-agent"})
    good = json.loads(lm.SHIPPED.read_text(encoding="utf-8"))
    good["version"] = "2099-12-31"
    web.catalog_file = good
    info = await up.check_local_catalog(apply=False)
    assert info["available"] and not info["applied"]
    assert any(u["kind"] == "localcatalog" for u in store.list_updates("new"))
    info = await up.check_local_catalog(apply=True)
    assert info["applied"] and store.local_catalog.version == "2099-12-31"
    assert not any(u["kind"] == "localcatalog" for u in store.list_updates("new"))
    bad = json.loads(json.dumps(good))
    bad["version"] = "2100-01-01"
    bad["families"][0]["models"][0]["tag"] = "x;y"
    web.catalog_file = bad
    with pytest.raises(Exception, match="格式不对"):
        await up.check_local_catalog(apply=True)
    assert store.local_catalog.version == "2099-12-31"


# ------------------------------------------------------------------ 接口
def test_api_catalog_add_check_and_remove(api, web):
    r = api.get("/api/local/catalog").json()
    assert r["running"] is False and r["hardware"]["accel"] in ("metal", "cuda", "none", "unknown")
    assert len(r["families"]) >= 12 and r["selfhost"][0]["id"] == "deepseek-v4" and r["cloud_only"]

    found = api.post("/api/local/check").json()
    assert found["found"] >= 3 and "catalog_update" in found
    ups = api.get("/api/updates").json()["items"]
    assert any(u["kind"] == "localmodel" and u["ref"] == "qwen3.9" for u in ups)

    assert api.post("/api/local/catalog/add", json={"tag": "nope:1b"}).status_code == 404
    assert api.post("/api/local/catalog/add", json={"tag": "bad tag"}).status_code == 400
    added = api.post("/api/local/catalog/add", json={"tag": "qwen3.9:latest", "note": "新"}).json()
    assert added["size_gb"] == 21.0
    cat = api.get("/api/local/catalog").json()
    assert cat["families"][-1]["id"] == "extras" and cat["families"][-1]["models"][0]["tag"] == "qwen3.9:latest"
    assert not any(u["ref"] == "qwen3.9" for u in api.get("/api/updates").json()["items"])     # 加入后提醒消失
    assert api.delete("/api/local/catalog/extra", params={"tag": "qwen3.9:latest"}).status_code == 200
    assert api.delete("/api/local/catalog/extra", params={"tag": "qwen3.9:latest"}).status_code == 404


def test_api_offline_blocks_local_discovery(api, web):
    api.put("/api/settings", json={"external_calls_enabled": False})
    n = len(web.calls)
    assert api.post("/api/local/check").status_code == 403
    assert api.post("/api/local/catalog/add", json={"tag": "qwen3.9:latest"}).status_code == 403
    assert len(web.calls) == n


# ------------------------------------------------------------------ 自建 DeepSeek 服务
def test_deepseek_selfhost_preset_is_local_openai_compatible(api):
    presets = {p["preset"]: p for p in api.get("/api/presets").json()}
    p = presets["deepseek-selfhost"]
    assert p["is_local"] is True and p["kind"] == "openai_compatible" and p["base_url"].endswith("/v1")
    added = api.post("/api/providers", json={"preset": "deepseek-selfhost"}).json()
    assert added["id"] == "deepseek-selfhost" and added["is_local"] is True
    assert [m["model_name"] for m in added["models"]] == ["deepseek-ai/DeepSeek-V4-Flash"]
    assert "本地" in added["models"][0]["strengths"]                                        # 自建服务算本地:外呼关闭时也能用


def test_selfhost_params_and_offline_use(store, make_router):
    prov = store.add_provider_from_preset("deepseek-selfhost")
    m = store.list_models()[-1]
    params = litellm_params(store.get_provider(prov["id"]), m)
    assert params["model"] == "openai/deepseek-ai/DeepSeek-V4-Flash" and params["api_base"] == "http://127.0.0.1:30000/v1"
    assert params["api_key"] == "sk-none"
    store.update_settings({"external_calls_enabled": False})
    assert m["id"] in [x["id"] for x in make_router(FakeLLM()).usable_models()]


async def test_safety_net_prefers_ollama_then_self_hosted(store, make_router):
    store.add_provider_from_preset("deepseek-selfhost")           # 比 Ollama 晚添加,但兜底顺序仍应 Ollama 在前
    store.update_settings({"route_chain": ["deepseek/deepseek-flash"], "external_calls_enabled": False})
    cands, _ = make_router(FakeLLM()).build_chain()
    assert [c["id"] for c in cands][:2] == ["ollama/qwen2.5:7b", "deepseek-selfhost/deepseek-ai/DeepSeek-V4-Flash"]
    fake = FakeLLM({"ollama_chat/": RuntimeError("ollama down")}, default="来自自建服务")     # Ollama 挂了,继续往自建服务兜底
    r = await make_router(fake).complete([{"role": "user", "content": "hi"}])
    assert r.model_id == "deepseek-selfhost/deepseek-ai/DeepSeek-V4-Flash"
