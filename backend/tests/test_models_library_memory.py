import io
import json

import pytest

from app import modelopts, strengths
from app.catalog import Catalog, validate
from app.library import Library, LibraryError
from app.memory import MemoryService, looks_sensitive
from app.router import ModelRouter
from app.textindex import chunk_text, tokenize
from tests.conftest import FakeLLM


# -------------------------------------------------------------- strengths
def test_infer_uses_catalog_flags_name_hints_and_local():
    assert "multimodal" in strengths.infer("x-vl-72b")
    assert "coding" in strengths.infer("qwen3-coder:30b")
    assert {"speed", "low-cost"} <= set(strengths.infer("gpt-9-mini", {"tier": "fast"}))
    local = strengths.infer("qwen2.5:7b", is_local=True)
    assert "local" in local and "reasoning" not in strengths.infer("tiny", {"reasoning": True, "tier": "fast"}, is_local=True)
    assert "long-context" in strengths.infer("kimi-k3") and len(strengths.infer("claude-opus-9", {"tier": "flagship", "reasoning": True, "vision": True, "coding": True})) <= 6
    assert strengths.clean_tags(["写作", "不存在", "写作", "代码"]) == ["writing", "coding"]   # 中文别名 → ASCII id


def test_catalog_shipped_is_valid_and_lookup_works(tmp_path):
    cat = Catalog(tmp_path)
    assert validate(cat.data) is None and cat.source == "shipped" and cat.version
    assert cat.find("deepseek", "deepseek-flash")["tier"] == "fast"
    assert cat.find(None, "kimi-k3")["id"] == "kimi-k3"           # 自定义服务商也能靠型号名查到
    assert cat.retired_of("deepseek")["deepseek-chat"]
    d = cat.defaults("deepseek")
    assert d and all(not cat.find("deepseek", i).get("legacy") for i in d)


def test_catalog_override_wins_only_when_newer_and_valid(tmp_path):
    cat = Catalog(tmp_path)
    shipped = cat.version
    (tmp_path / "catalog.json").write_text(json.dumps({"version": "0000-00-00", "providers": {"x": {"models": []}}}))
    assert Catalog(tmp_path).source == "shipped"
    (tmp_path / "catalog.json").write_text(json.dumps({"version": "9999-01-01", "providers": {"deepseek": {"models": [{"id": "deepseek-z"}]}}}))
    c2 = Catalog(tmp_path)
    assert c2.source == "override" and c2.version == "9999-01-01" and shipped != c2.version
    (tmp_path / "catalog.json").write_text("{坏")
    assert Catalog(tmp_path).source == "shipped"
    assert validate({"version": "1", "providers": {"a": {"models": [{}]}}})


def test_model_strengths_can_be_overridden_and_reset(store):
    m = store.get_model("deepseek/deepseek-flash")
    assert not m["strengths_custom"] and m["strengths"] == m["strengths_auto"]
    m = store.update_model(m["id"], {"strengths": ["写作", "乱写的"]})   # 中文别名与未知标签混着提交
    assert m["strengths"] == ["writing"] and m["strengths_custom"] and m["strengths_auto"] != ["writing"]
    m = store.update_model(m["id"], {"strengths": None})
    assert not m["strengths_custom"]


# ------------------------------------------------------------ model options
def test_model_options_marks_new_added_and_gone(store):
    opts = modelopts.model_options(store, "deepseek")
    assert opts["new_count"] == 0                                   # 第一次打开:现有的都算看过
    by = {m["id"]: m for m in opts["models"]}
    assert by["deepseek-flash"]["added"] and by["deepseek-flash"]["strengths"]
    # 服务商实时清单出现了目录里没有的新型号,并且少了一个你已添加的
    store.set_model_live("deepseek", ["deepseek-flash", "deepseek-brand-new"])
    opts = modelopts.model_options(store, "deepseek")
    by = {m["id"]: m for m in opts["models"]}
    assert by["deepseek-brand-new"]["is_new"] and not by["deepseek-brand-new"]["in_catalog"]
    assert by["deepseek-v4-pro"]["gone"] and not by["deepseek-flash"]["gone"]
    assert opts["models"][0]["id"] == "deepseek-brand-new"           # 新的排最前
    modelopts.mark_seen(store, "deepseek")
    assert modelopts.model_options(store, "deepseek")["new_count"] == 0


def test_model_options_flags_retired_and_new_from_catalog_update(store, tmp_path):
    store.add_model("deepseek", "deepseek-chat")
    modelopts.model_options(store, "deepseek")
    by = {m["id"]: m for m in modelopts.model_options(store, "deepseek")["models"]}
    assert "Retired" in by["deepseek-chat"]["retired_reason"]
    # 目录升级后多出来的型号被标为新
    data = json.loads(json.dumps(store.catalog.data))
    data["version"] = "9999-01-01"
    data["providers"]["deepseek"]["models"].append({"id": "deepseek-v5", "name": "V5", "tier": "flagship"})
    store.catalog.save_override(data)
    opts = modelopts.model_options(store, "deepseek")
    assert opts["catalog_version"] == "9999-01-01" and opts["new_count"] == 1
    assert next(m for m in opts["models"] if m["is_new"])["id"] == "deepseek-v5"


def test_ollama_options_report_installed_not_gone(store):
    store.set_model_live("ollama", ["qwen2.5:7b"])
    by = {m["id"]: m for m in modelopts.model_options(store, "ollama")["models"]}
    assert by["qwen2.5:7b"]["installed"] is True and not by["qwen2.5:7b"]["gone"]
    assert by["qwen3.5:9b"]["installed"] is False        # 目录里有、本机没装


# ------------------------------------------------------------------ routing
def _router(store, fake=None):
    return ModelRouter(store, fake or FakeLLM(default="ok"))


def test_rank_by_tags_prefers_cloud_and_respects_offline(store):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.add_provider_from_preset("gemini", api_key="g-key-1234567890")
    r = _router(store)
    top = r.rank_by_tags(["多模态"])[0]
    assert top["id"].startswith("gemini/") or top["id"].startswith("deepseek/")
    assert all(not m["is_local"] for m in r.rank_by_tags(["中文"]))          # 本地小模型不抢云端的位置
    assert r.rank_by_tags(["本地"])[0]["is_local"]                             # 明确要本地才给本地
    store.update_settings({"external_calls_enabled": False})
    assert [m["id"] for m in r.rank_by_tags(["中文"])] == ["ollama/qwen2.5:7b"]  # 离线:只剩本地
    assert r.rank_by_tags([]) == []


async def test_agent_tags_choose_model_and_fallback_records_first_choice(store):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.add_provider_from_preset("gemini", api_key="g-key-1234567890")
    store.update_settings({"route_chain": ["deepseek/deepseek-flash", "ollama/qwen2.5:7b"]})
    fake = FakeLLM({"gemini/": RuntimeError("boom")}, default="兜底")
    r = _router(store, fake)
    want = r.rank_by_tags(["多模态"], 2)
    assert want
    res = await r.complete([{"role": "user", "content": "hi"}], tags=["多模态"])
    if want[0]["id"].startswith("gemini/"):
        assert res.fallback_from == want[0]["id"] and not res.model_id.startswith("gemini/")
    assert r.resolve(None, ["多模态"])["id"] in {m["id"] for m in store.list_models()}
    assert r.resolve("ollama/qwen2.5:7b", ["多模态"])["id"] == "ollama/qwen2.5:7b"    # 手动指定优先


# ------------------------------------------------------------------ library
def test_tokenize_and_chunk():
    assert tokenize("北京大学 Python") == ["北京", "京大", "大学", "python"]
    assert all(len(c) <= 700 for c in chunk_text(("一句话。" * 100 + "\n\n") * 5))
    assert chunk_text("   ") == []


def test_library_search_ranks_relevant_doc_and_scopes(store):
    lib = Library(store)
    a = lib.add_text("差旅制度", "出差住宿标准:一线城市每晚不超过 600 元。\n\n交通:高铁二等座可报销。")
    b = lib.add_text("食堂菜单", "周一红烧肉,周二清蒸鱼,周三番茄炒蛋。")
    hits = lib.search("出差住宿标准是多少", 3)
    assert hits and hits[0]["doc_id"] == a["id"]
    assert lib.search("红烧肉", 3)[0]["doc_id"] == b["id"]
    assert lib.search("出差住宿", 3, [b["id"]]) == []                # 限定文档范围
    assert lib.search("出差住宿", 3, []) == []
    lib.update(a["id"], {"enabled": False})
    assert lib.search("出差住宿", 3) == []                            # 停用的文档不参与检索
    lib.delete(b["id"])
    assert lib.search("红烧肉", 3) == [] and store.get_doc(b["id"]) is None
    assert store.all_chunks() == []                                   # 级联删除了分块


def test_library_file_formats_and_errors(store):
    import docx

    lib = Library(store)
    d = lib.add_file("说明.md", "# 标题\n\n报销流程见下。".encode("utf-8"))
    assert d["kind"] == "md" and d["chunks"] >= 1
    d2 = lib.add_file("旧文件.txt", "国标编码测试内容".encode("gb18030"))
    assert "国标" in lib.read(d2["id"])["text"]
    d3 = lib.add_file("页面.html", "<html><script>var x=1</script><body><p>网页正文</p></body></html>".encode())
    assert "网页正文" in lib.read(d3["id"])["text"] and "var x" not in lib.read(d3["id"])["text"]
    buf = io.BytesIO()
    doc = docx.Document()
    doc.add_paragraph("合同第一条:甲乙双方")
    doc.save(buf)
    d4 = lib.add_file("合同.docx", buf.getvalue())
    assert "甲乙双方" in lib.read(d4["id"])["text"]
    with pytest.raises(LibraryError, match="不支持"):
        lib.add_file("a.exe", b"MZ")
    with pytest.raises(LibraryError):
        lib.add_text("空", "   ")
    with pytest.raises(LibraryError, match="PDF"):
        lib.add_file("坏.pdf", b"not a pdf")


def test_library_scope_ids_and_find_by_title(store):
    lib = Library(store)
    d = lib.add_text("报价单 2026", "内容内容内容")
    assert lib.find_by_title("报价单")["id"] == d["id"] and lib.find_by_title("不存在") is None
    assert lib.scope_ids({"mode": "all"}) is None and lib.scope_ids({"mode": "off"}) == []
    assert lib.scope_ids({"mode": "selected", "ids": ["x"]}) == ["x"]


# ------------------------------------------------------------------- memory
def test_recall_prefers_relevant_pinned_and_preferences(store):
    mem = MemoryService(store, _router(store))
    g = store.list_groups()[0]["id"]
    aid = store.list_agents()[0]["id"]
    store.add_memory("周报喜欢先给结论再列数据", "global", kind="preference")
    store.add_memory("发布会定在 10 月 12 日", "group", g, "fact")
    store.add_memory("预算口径按含税价", "group", g, "decision")
    store.add_memory("别的群的事", "group", "other-group", "fact")
    pin = store.add_memory("永远用中文回复", "global", kind="preference", pinned=True)
    got = mem.recall(g, aid, "发布会什么时候开", k=3)
    texts = [m["content"] for m in got]
    assert texts[0] == pin["content"]                                     # 置顶优先
    assert "发布会定在 10 月 12 日" in texts and "别的群的事" not in texts
    assert "记忆" in mem.block(got) and mem.block([]) == ""
    assert store.list_memories("group", g)[0]["hits"] >= 0
    assert store.get_memory(got[1]["id"])["hits"] == 1                    # 被取回的记忆记录命中次数


def test_add_memory_dedups_and_sensitive_filter(store):
    a = store.add_memory("同一条", "global")
    b = store.add_memory("同一条", "global")
    assert a["id"] == b["id"] and len(store.list_memories()) == 1
    assert looks_sensitive("我的密码是 abc") and looks_sensitive("sk-abcdefghijklmnop") and looks_sensitive("13800138000")
    assert not looks_sensitive("周报先给结论")


async def test_extract_saves_valid_items_and_skips_secrets_and_dupes(store):
    store.update_provider("deepseek", {"api_key": "sk-test-1234567890"})
    store.update_settings({"memory_auto_extract": True})
    store.add_memory("客户喜欢简洁的汇报", "global", kind="preference")
    reply = json.dumps([
        {"scope": "global", "kind": "preference", "content": "客户喜欢简洁的汇报风格"},        # 与已有几乎重复
        {"scope": "group", "kind": "decision", "content": "发布会主题定为「春季新品」"},
        {"scope": "global", "kind": "fact", "content": "我的密码是 hunter2"},                     # 敏感
        {"scope": "global", "kind": "fact", "content": "很" * 200},                              # 过长
    ], ensure_ascii=False)
    fake = FakeLLM(default="前言\n" + reply)
    mem = MemoryService(store, ModelRouter(store, fake))
    g = store.list_groups()[0]
    saved = await mem.extract(g, "定一下主题", "主题定为春季新品")
    assert [m["content"] for m in saved] == ["发布会主题定为「春季新品」"]
    assert saved[0]["scope"] == "group" and saved[0]["scope_id"] == g["id"] and saved[0]["source"] == "auto"
    store.update_settings({"memory_auto_extract": False})
    assert await mem.extract(g, "x", "y") == []
    fake2 = FakeLLM(default=RuntimeError("down"))
    store.update_settings({"memory_auto_extract": True})
    assert await MemoryService(store, ModelRouter(store, fake2)).extract(g, "x", "y") == []   # 失败静默


def test_record_action_summarises_and_trims(store):
    mem = MemoryService(store, _router(store))
    g = store.list_groups()[0]
    steps = [{"agent": "文案", "model": "deepseek/deepseek-flash", "tools": ["library_search"], "ok": True},
             {"agent": "校对", "model": "ollama/qwen2.5:7b", "fallback": True, "ok": True}]
    m = mem.record_action(g, "写发布会通知", steps, 12.4)
    assert m["kind"] == "action" and "文案" in m["content"] and "library_search" in m["content"] and "回退" in m["content"]
    for i in range(50):
        mem.record_action(g, f"任务{i}", steps, 1)
    assert len(store.list_memories("group", g["id"], "action")) == 40
    assert mem.record_action(g, "空", [], 1) is None
