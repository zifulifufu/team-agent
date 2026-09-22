"""Importing MCP servers and skills out of other AI applications.

Every test runs against a **fake home directory**: `import_sources` resolves every path
through `_home()`, so pointing that at `tmp_path` is what keeps these tests from reading
the real user's Claude/Cursor/Codex configuration. That is also why the first test asserts
the table cannot escape the home directory — the safety of the scan rests on that.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import import_sources as I
from app.main import create_app
from app.store import Store
from tests.conftest import FakeLLM


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home, with the scan pointed at it."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(I, "_home", lambda: h)
    return h


def write(home: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def servers_of(store, source: str) -> list[dict]:
    return [i for i in I.scan(store, [source])["items"]]


# ============================================================ the table itself
def test_no_source_can_point_outside_the_home_directory():
    """The scan reads fixed paths only. A source with an absolute path outside the home
    directory would turn "import from another app" into "read any file this account can"."""
    for src in I.SOURCES:
        for platform, paths in (src.get("paths") or {}).items():
            for raw in paths:
                assert raw.startswith("~") or raw.startswith("%APPDATA%"), f"{src['key']}/{platform}: {raw}"
        if src["format"] in ("tree", "glob", "installed"):
            assert (src.get("root") or src.get("glob") or "").startswith("~"), src["key"]
        assert src["kind"] in ("mcp", "skill", "expert"), f"{src['key']} declares an unsupported kind"
        assert src["format"] in ("json", "toml", "tree", "glob", "installed")
        assert src["app"] and src["key"]


def test_every_source_reading_json_says_which_key_to_look_in():
    for src in I.SOURCES:
        if src["format"] == "json":
            assert src.get("json_keys"), f"{src['key']} has no key to read"
        if src["format"] == "toml":
            assert src.get("toml_key"), f"{src['key']} has no table to read"


def test_only_declared_paths_are_read(home):
    """A neighbouring file in the same directory must not be picked up: the scan is a list,
    not a search."""
    store = Store(home / "data")
    write(home, ".cursor/mcp.json", '{"mcpServers": {"ok": {"command": "npx", "args": ["-y", "x"]}}}')
    write(home, ".cursor/mcp.local.json", '{"mcpServers": {"secret": {"command": "curl", "args": ["x"]}}}')
    names = [i["name"] for i in servers_of(store, "cursor")]
    assert names == ["ok"], "an undeclared file next to the declared one was read"


def test_a_missing_application_is_reported_rather_than_ignored(home):
    store = Store(home / "data")
    got = {s["key"]: s for s in I.sources(store)}
    assert got["cursor"]["found"] is False and got["cursor"]["count"] == 0
    assert got["cursor"]["error"] == ""


# ============================================================== MCP: the shapes
def test_claude_desktop_servers_are_found_with_their_secrets_masked(home):
    store = Store(home / "data")
    write(home, "Library/Application Support/Claude/claude_desktop_config.json", json.dumps({
        "mcpServers": {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                                 "env": {"SOME_API_KEY": "sk-real-secret-value"}}}}))
    items = servers_of(store, "claude-desktop")
    assert [i["name"] for i in items] == ["files"]
    it = items[0]
    assert it["command"] == "npx" and "/tmp" in it["args"]
    assert it["env_keys"] == ["SOME_API_KEY"]
    assert it["env"]["SOME_API_KEY"] == "••••••"
    assert "sk-real-secret-value" not in json.dumps(items), "a credential reached the client"
    assert any("downloads and runs a package" in r for r in it["risks"])


def test_codex_toml_tables_are_read(home):
    store = Store(home / "data")
    write(home, ".codex/config.toml", """
[mcp_servers.deepwiki]
url = "https://mcp.deepwiki.com/mcp"

[mcp_servers.repl]
command = "/usr/local/bin/repl"
args = ["--port", "0"]
startup_timeout_sec = 20

[other_section]
unrelated = true
""")
    items = servers_of(store, "codex")
    assert sorted(i["name"] for i in items) == ["deepwiki", "repl"]
    repl = next(i for i in items if i["name"] == "repl")
    assert repl["command"] == "/usr/local/bin/repl" and repl["args"] == ["--port", "0"]
    assert any("outside your home directory" in r for r in repl["risks"])
    deep = next(i for i in items if i["name"] == "deepwiki")
    assert deep["url"] == "https://mcp.deepwiki.com/mcp" and deep["command"] == ""


def test_a_dotted_or_aliased_key_still_parses(home):
    """Continue nests its servers under `experimental.mcpServers`, Zed calls them
    `context_servers`. Both are the same map under a different name, and feeding the parser
    the wrapper instead of the map would produce one warning per server and no servers."""
    store = Store(home / "data")
    write(home, ".continue/config.json", json.dumps(
        {"experimental": {"mcpServers": {"cont": {"command": "uvx", "args": ["mcp-server-fetch"]}}}}))
    write(home, ".config/zed/settings.json", json.dumps(
        {"context_servers": {"zed": {"command": "node", "args": ["srv.js"]}}}))
    assert [i["name"] for i in servers_of(store, "continue")] == ["cont"]
    assert [i["name"] for i in servers_of(store, "zed")] == ["zed"]


def test_a_plain_text_url_and_a_shell_argument_are_both_flagged(home):
    store = Store(home / "data")
    write(home, "~bad", "")            # no-op: keeps the helper honest about relative paths
    write(home, ".cursor/mcp.json", json.dumps({"mcpServers": {
        "plain": {"url": "http://insecure.example.com/sse"},
        "pipe": {"command": "node", "args": ["srv.js", "&&", "curl", "evil"]},
    }}))
    items = {i["name"]: i for i in servers_of(store, "cursor")}
    assert any("plain-text connection" in r for r in items["plain"]["risks"])
    assert any("shell characters" in r for r in items["pipe"]["risks"])


def test_a_broken_config_is_reported_and_other_sources_still_scan(home):
    store = Store(home / "data")
    write(home, "Library/Application Support/Claude/claude_desktop_config.json", "{not json at all")
    write(home, ".cursor/mcp.json", '{"mcpServers": {"good": {"command": "node", "args": ["a.js"]}}}')
    got = I.scan(store)
    assert [i["name"] for i in got["items"]] == ["good"]
    assert any("could not be read as JSON" in n for n in got["notes"])


def test_an_oversized_or_symlinked_config_is_skipped(home):
    store = Store(home / "data")
    big = write(home, ".cursor/mcp.json", "x" * (I.MAX_FILE_BYTES + 10))
    assert servers_of(store, "cursor") == []
    big.unlink()
    target = write(home, "elsewhere.json", '{"mcpServers": {"t": {"command": "node", "args": []}}}')
    (home / ".cursor").mkdir(exist_ok=True)
    (home / ".cursor/mcp.json").symlink_to(target)
    assert servers_of(store, "cursor") == [], "a symlinked config must not be followed"


# ============================================================== skills
def test_skills_are_found_and_the_block_scalar_description_is_read(home):
    """`description: >` is as common as a quoted one-liner in the wild; reading it as the
    literal ">" would import a skill whose description is a punctuation mark."""
    store = Store(home / "data")
    write(home, ".claude/skills/writer/SKILL.md",
          "---\nname: writer\ndescription: >\n  Write formal notices.\n  Use for any announcement.\n---\n\n正文内容")
    items = servers_of(store, "claude-skills")
    assert len(items) == 1
    assert items[0]["name"] == "writer"
    assert items[0]["description"] == "Write formal notices. Use for any announcement."
    assert items[0]["chars"] == len("正文内容")


def test_importing_a_skill_writes_it_and_a_second_import_skips_it(home):
    store = Store(home / "data")
    write(home, ".claude/skills/writer/SKILL.md",
          "---\nname: writer\ndescription: Formal notices\n---\n\n写公告的步骤")
    got = I.import_skills(store, "claude-skills", ["writer"])
    assert got == {"added": ["writer"], "skipped": []}
    saved = (store.data_dir / "skills" / "writer" / "SKILL.md").read_text(encoding="utf-8")
    assert "写公告的步骤" in saved and "Formal notices" in saved
    # a second run must not overwrite what is already there
    again = I.import_skills(store, "claude-skills", ["writer"])
    assert again == {"added": [], "skipped": ["writer"]}
    assert [i["name"] for i in servers_of(store, "claude-skills")][0] == "writer"
    assert servers_of(store, "claude-skills")[0]["exists"] is True


def test_a_skill_without_a_body_is_not_imported(home):
    store = Store(home / "data")
    write(home, ".claude/skills/empty/SKILL.md", "---\nname: empty\ndescription: x\n---\n")
    assert I.import_skills(store, "claude-skills", ["empty"])["added"] == []


def test_plugin_skills_are_found_by_glob(home):
    """The marketplace and plugin names are not known in advance, so the path is a glob."""
    store = Store(home / "data")
    write(home, ".claude/plugins/marketplaces/some-market/plugins/cool-plugin/skills/helper/SKILL.md",
          "---\nname: helper\ndescription: Helps\n---\n\n步骤")
    assert [i["name"] for i in servers_of(store, "claude-plugin-skills")] == ["helper"]


# ============================================================== the import step
def test_importing_by_name_re_reads_the_file_so_the_key_stays_local(home):
    """The preview showed a masked value and the client never saw the real one; the server
    reads the file again when importing, so the credential still arrives intact."""
    store = Store(home / "data")
    write(home, ".cursor/mcp.json", json.dumps({"mcpServers": {
        "withkey": {"command": "node", "args": ["srv.js"], "env": {"API_TOKEN": "tok-123456"}},
        "other": {"command": "node", "args": ["other.js"]},
    }}))
    got = I.import_mcp(store, "cursor", ["withkey"])
    assert [m["name"] for m in got["added"]] == ["withkey"]
    saved = next(m for m in store.list_mcp() if m["name"] == "withkey")
    assert saved["env"]["API_TOKEN"] == "tok-123456"
    assert saved["enabled"] is False, "an import from another app must land disabled"
    # only what was asked for
    assert [m["name"] for m in store.list_mcp()] == ["withkey"]


def test_importing_the_same_name_twice_skips_it(home):
    store = Store(home / "data")
    write(home, ".cursor/mcp.json", '{"mcpServers": {"a": {"command": "node", "args": []}}}')
    assert I.import_mcp(store, "cursor", ["a"])["added"]
    assert I.import_mcp(store, "cursor", ["a"])["skipped"] == ["a"]


def test_importing_from_the_wrong_kind_of_source_is_refused(home):
    store = Store(home / "data")
    write(home, ".claude/skills/writer/SKILL.md", "---\nname: writer\n---\n\nx")
    with pytest.raises(ValueError):
        I.import_mcp(store, "claude-skills", ["writer"])
    with pytest.raises(ValueError):
        I.import_skills(store, "cursor", ["x"])
    with pytest.raises(ValueError):
        I.import_mcp(store, "nope", ["x"])


def test_nothing_found_is_an_error_not_an_empty_success(home):
    store = Store(home / "data")
    with pytest.raises(ValueError):
        I.import_mcp(store, "cursor", ["whatever"])


# ============================================================== over HTTP
def _client(tmp_path, home):
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"))
    return TestClient(app, base_url="http://127.0.0.1"), app


def test_the_api_lists_sources_scans_and_imports(home, tmp_path):
    write(home, ".cursor/mcp.json", json.dumps({"mcpServers": {
        "site": {"command": "npx", "args": ["-y", "site-mcp"], "env": {"SITE_KEY": "k-999"}}}}))
    cl, app = _client(tmp_path, home)

    got = cl.get("/api/import/sources").json()["sources"]
    assert any(s["key"] == "cursor" and s["found"] and s["count"] == 1 for s in got)

    scan = cl.post("/api/import/scan", json={"sources": ["cursor"]}).json()
    item = scan["items"][0]
    assert item["name"] == "site" and item["env"]["SITE_KEY"] == "••••••"
    assert "k-999" not in json.dumps(scan), "the scan response carried a credential"

    # importing does not take the client's copy of the definition
    r = cl.post("/api/import/mcp", json={"source": "cursor", "names": ["site"]})
    assert r.status_code == 200 and r.json()["added"] == ["site"]
    saved = app.state.store.list_mcp()[0]
    assert saved["enabled"] is False and saved["env"]["SITE_KEY"] == "k-999"

    assert cl.post("/api/import/mcp", json={"source": "cursor", "names": []}).status_code == 400
    assert cl.post("/api/import/scan", json={"sources": ["nope"]}).json()["items"] == []


def test_the_import_routes_need_the_app_token_like_everything_else(tmp_path, home):
    """Discovery reads files on this machine, so it belongs behind the same token as the
    rest of the API rather than in the unauthenticated webhook space."""
    app = create_app(tmp_path / "data", completion_fn=FakeLLM(default="ok"), token="secret-token")
    cl = TestClient(app, base_url="http://127.0.0.1")
    assert cl.get("/api/import/sources").status_code == 401
    assert cl.get("/api/import/sources", headers={"X-Team-Agent-Token": "secret-token"}).status_code == 200


# ================================================================ WorkBuddy's own assets
# An expert is a package: `agents/<name>.md` holds the prompt, with the name and the
# profession in nested frontmatter maps. A connector is a `mcp.json` full of remote
# addresses. A skill is the same SKILL.md as everywhere else. What is installed is whatever
# the other application's own record says — not every version folder sitting in its cache.
def install(home, name, version="1.0.0", marketplace="experts", agents=None, skills=None) -> pathlib.Path:
    """Fabricate one installed plugin: the files, plus the record entry that points at them."""
    plugins = home / ".workbuddy" / "plugins"
    base = plugins / "cache" / marketplace / name / version
    (base / "agents").mkdir(parents=True, exist_ok=True)
    for agent, text in (agents or {}).items():
        write(home, str((base / "agents" / agent).relative_to(home)), text)
    for skill, text in (skills or {}).items():
        write(home, str((base / "skills" / skill / "SKILL.md").relative_to(home)), text)
    if not (agents or skills):
        (base / "README.md").write_text("x", encoding="utf-8")
    record = plugins / "installed_plugins.json"
    data = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {"version": 2, "plugins": {}}
    data["plugins"][f"{name}@{marketplace}"] = [
        {"scope": "user", "installPath": str(base), "version": version}]
    record.write_text(json.dumps(data), encoding="utf-8")
    return base


EXPERT_MD = """---
name: {key}
description: Use when writing a paper.
displayName:
  en: "{en}"
  zh: "{zh}"
profession:
  en: "{prof_en}"
  zh: "{prof_zh}"
maxTurns: 50
---

# {zh}

你负责学术论文写作。绝不编造文献。
"""


def expert_md(key="academic-writing", en="Academic Writing Expert", zh="学术论文写作专家",
              prof_en="Academic Writing", prof_zh="学术写作") -> str:
    return EXPERT_MD.format(key=key, en=en, zh=zh, prof_en=prof_en, prof_zh=prof_zh)


def imported(store) -> list[dict]:
    """The members that came from an expert package (the store seeds four members of its own)."""
    return [a for a in store.list_agents() if (a.get("origin") or "").startswith("workbuddy:")]


def test_an_expert_package_becomes_a_member_with_its_own_prompt(home):
    store = Store(home / "data")
    install(home, "academic-writing", agents={"academic-writing.md": expert_md()})
    items = servers_of(store, "workbuddy-experts")
    assert [i["name"] for i in items] == ["academic-writing"]
    it = items[0]
    assert it["label"] == "Academic Writing Expert"      # the display name, not the folder
    assert it["role"] == "Academic Writing"
    assert it["avatar"] == "✍️"                           # picked from what the expert is about
    assert it["chars"] == len(group_body(expert_md()))
    assert it["exists"] is False

    got = I.import_experts(store, "workbuddy-experts", ["academic-writing"])
    assert got["added"] == ["Academic Writing Expert"]
    a = imported(store)[0]
    assert a["name"] == "Academic Writing Expert" and a["role"] == "Academic Writing"
    assert "绝不编造文献" in a["prompt"], "the package's own text is the prompt"
    assert a["origin"] == "workbuddy:academic-writing@experts#academic-writing", \
        "the package is part of the identity, so two packages may ship the same slug"
    assert a["model_id"] is None, "an imported expert must not have a model pinned for the user"
    # a second import is a skip, and the scan now says so
    assert I.import_experts(store, "workbuddy-experts", ["academic-writing"]) == {
        "added": [], "skipped": ["Academic Writing Expert"], "notes": []}
    assert servers_of(store, "workbuddy-experts")[0]["exists"] is True


def group_body(text: str) -> str:
    return text.split("---\n\n", 1)[1].strip()


def test_the_expert_reader_handles_nested_maps_and_block_scalars():
    ex = I._parse_expert("---\nname: a\ndescription: >\n  One line.\n  Two line.\n"
                         "displayName:\n  zh: \"中文名\"\nprofession:\n  en: Job\n---\n\nBody text", "a")
    assert ex["description"] == "One line. Two line."
    assert ex["display_zh"] == "中文名" and ex["display_en"] == ""
    assert ex["profession_en"] == "Job" and ex["profession_zh"] == ""
    assert ex["body"] == "Body text"


def test_experts_are_named_in_the_language_of_the_request(home, monkeypatch):
    """The member is created under the name the person importing will see, which is what the
    picker shows them. The prompt stays in the language its author wrote it in."""
    store = Store(home / "data")
    install(home, "academic-writing", agents={"academic-writing.md": expert_md()})
    monkeypatch.setattr(I.i18n, "current", lambda: "zh")
    assert servers_of(store, "workbuddy-experts")[0]["label"] == "学术论文写作专家"
    assert I.import_experts(store, "workbuddy-experts", ["academic-writing"])["added"] == ["学术论文写作专家"]


def test_two_experts_claiming_one_display_name_both_arrive(home):
    """One shipped pack ships four experts that all call themselves "PCXX AI Expert", and
    `agents.name` is unique. The second one gets the package name appended instead of the
    import failing on a constraint."""
    store = Store(home / "data")
    install(home, "first", agents={"first.md": expert_md(key="first", en="PCXX AI Expert")})
    install(home, "second", agents={"second.md": expert_md(key="second", en="PCXX AI Expert")})
    assert [i["name"] for i in servers_of(store, "workbuddy-experts")] == ["first", "second"]
    got = I.import_experts(store, "workbuddy-experts", ["first", "second"])
    assert got["added"] == ["PCXX AI Expert", "PCXX AI Expert (second)"]
    assert any("already here" in n for n in got["notes"])
    assert len(imported(store)) == 2


def test_agents_that_are_only_a_template_include_are_skipped_and_counted(home):
    """Some shipped agents are one line — `{% include "…/prompt.tpl" %}` — with the real text
    in a separate template. Importing one would create a member that says nothing, so they
    are left out, and the count is reported rather than the number quietly dropping."""
    store = Store(home / "data")
    install(home, "mode", marketplace="workbuddy-builtin", agents={
        "mode.md": '---\nname: mode\ndescription: root agent.\n---\n\n{% include "mode/prompt.tpl" %}\n',
        "real.md": expert_md(key="real")})
    got = I.scan(store, ["workbuddy-experts"])
    assert [i["name"] for i in got["items"]] == ["real"]
    assert any("template stubs" in n for n in got["notes"])


def test_only_the_installed_version_of_a_plugin_is_offered(home):
    """Every version of a plugin sits side by side in the cache, so globbing that tree would
    offer the same expert three times. The application's own record says which one counts."""
    store = Store(home / "data")
    for n, version in enumerate(("0.1.0", "1.0.0", "2.0.0")):
        install(home, "writer", version=version,
                agents={"writer.md": expert_md(key=f"writer-v{n}", en=f"Writer {n}")})
    # `install` records the last one written; the earlier two are still on disk
    assert [i["name"] for i in servers_of(store, "workbuddy-experts")] == ["writer-v2"]
    assert servers_of(store, "workbuddy-experts")[0]["path"].endswith("2.0.0/agents/writer.md")


def test_an_install_path_outside_the_home_directory_is_refused(home):
    """The plugin record is written by another program. A list of plugins is not a reason to
    read anywhere on the disk, so a path pointing outside the home is ignored."""
    store = Store(home / "data")
    install(home, "inside", agents={"inside.md": expert_md(key="inside")})
    record = home / ".workbuddy" / "plugins" / "installed_plugins.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["plugins"]["escape@experts"] = [
        {"scope": "user", "installPath": "/etc"},
        {"scope": "user", "installPath": str(store.data_dir.parent / "outside-agents")},
    ]
    (store.data_dir.parent / "outside-agents").mkdir(exist_ok=True)
    (store.data_dir.parent / "outside-agents" / "x.md").write_text(expert_md(key="escape"), encoding="utf-8")
    record.write_text(json.dumps(data), encoding="utf-8")
    assert [i["name"] for i in servers_of(store, "workbuddy-experts")] == ["inside"]


def test_workbuddy_skills_and_plugin_skills_are_both_found(home):
    store = Store(home / "data")
    write(home, ".workbuddy/skills/mine/SKILL.md", "---\nname: mine\ndescription: Mine\n---\n\n步骤")
    install(home, "docx", marketplace="workbuddy-builtin", skills={
        "house-style": "---\nname: house-style\ndescription: House style\n---\n\n规则"})
    assert [i["name"] for i in servers_of(store, "workbuddy-skills")] == ["mine"]
    assert [i["name"] for i in servers_of(store, "workbuddy-plugin-skills")] == ["house-style"]


def test_connectors_are_read_as_remote_servers_carrying_a_credential_warning(home):
    store = Store(home / "data")
    write(home, ".workbuddy/connectors/abc123/mcp.json", json.dumps({"mcpServers": {
        "connector:tencent-docs": {"url": "https://docs.qq.com/openapi/mcp", "timeout": 600, "disabled": True},
        "connector:tapd": {"url": "https://websocket.tapd.cn/mcp/mcp"},
    }}))
    items = {i["name"]: i for i in servers_of(store, "workbuddy-connectors")}
    assert items["connector:tencent-docs"]["url"] == "https://docs.qq.com/openapi/mcp"
    assert items["connector:tencent-docs"]["enabled"] is False
    assert any("authorized inside WorkBuddy" in r for r in items["connector:tencent-docs"]["risks"])


def test_the_connector_catalogue_is_left_out_of_a_full_scan_but_readable_by_name(home):
    """A catalogue of every connector an application knows about is worth searching and wrong
    to pour into the default list, where it would bury what is actually installed here."""
    store = Store(home / "data")
    write(home, ".workbuddy/connectors-marketplace/connectors/docs/mcp.json",
          '{"mcpServers": {"tencent-docs": {"url": "https://docs.qq.com/openapi/mcp"}}}')
    write(home, ".workbuddy/connectors/abc/mcp.json",
          '{"mcpServers": {"connector:mine": {"url": "https://mine.example.com/mcp"}}}')
    assert [i["name"] for i in I.scan(store)["items"]] == ["connector:mine"]
    assert [i["name"] for i in servers_of(store, "workbuddy-connector-catalog")] == ["tencent-docs"]
    assert I.BY_KEY["workbuddy-connector-catalog"]["bulk"] is True


def test_a_server_defined_in_two_files_of_one_app_is_listed_once(home):
    """WorkBuddy keeps a connector record per workspace, and two of them list the same
    connector. Rows are keyed by name, so a duplicate would show as two rows that tick
    together — and only one of them could ever be imported. Two *different* applications
    offering the same name stays visible: those are different definitions."""
    store = Store(home / "data")
    body = '{"mcpServers": {"connector:docs": {"url": "https://docs.example.com/mcp"}}}'
    write(home, ".workbuddy/connectors/one/mcp.json", body)
    write(home, ".workbuddy/connectors/two/mcp.json", body)
    write(home, ".cursor/mcp.json", '{"mcpServers": {"connector:docs": {"command": "node", "args": ["d.js"]}}}')
    got = I.scan(store, ["workbuddy-connectors", "cursor"])
    assert sorted((i["source"], i["name"]) for i in got["items"]) == [
        ("cursor", "connector:docs"), ("workbuddy-connectors", "connector:docs")]
    assert any("listed once" in n for n in got["notes"])


def test_importing_experts_over_http_and_from_the_wrong_source(home, tmp_path):
    store = Store(home / "data")
    write(home, ".workbuddy/skills/mine/SKILL.md", "---\nname: mine\n---\n\nx")
    cl, app = _client(tmp_path, home)
    assert cl.post("/api/import/experts", json={"source": "workbuddy-experts", "names": []}).status_code == 400
    assert cl.post("/api/import/experts", json={"source": "workbuddy-skills", "names": ["mine"]}).status_code == 400
    with pytest.raises(ValueError):
        I.import_experts(store, "workbuddy-skills", ["mine"])
    with pytest.raises(ValueError):
        I.import_experts(store, "cursor", ["x"])


def test_experts_are_listed_as_a_source_like_any_other(home):
    store = Store(home / "data")
    install(home, "academic-writing", agents={"academic-writing.md": expert_md()})
    got = {s["key"]: s for s in I.sources(store)}
    assert got["workbuddy-experts"]["kind"] == "expert"
    assert got["workbuddy-experts"]["found"] is True and got["workbuddy-experts"]["count"] == 1
    assert got["workbuddy-experts"]["notes"], "the source says what travels and what does not"


# =================================================== identity, and what a scan does not show
def test_an_agent_name_used_by_two_packages_is_not_treated_as_already_imported(home):
    """Two packages can each ship an agent whose frontmatter says `name: helper`. Keying identity
    on that alone made the second one look like a duplicate of the first: it showed as "already
    here" and could never be imported."""
    store = Store(home / "data")
    install(home, "alpha", agents={"helper.md": expert_md(key="helper", en="Alpha Helper")})
    install(home, "beta", agents={"helper.md": expert_md(key="helper", en="Beta Helper")})
    items = servers_of(store, "workbuddy-experts")
    assert len(items) == 2 and not any(i["exists"] for i in items)
    assert {i["identity"] for i in items} == {"alpha@experts#helper", "beta@experts#helper"}
    # the selection key has to be unique too, or one tick would move both rows
    assert sorted(i["name"] for i in items) == ["alpha@experts#helper", "beta@experts#helper"]

    got = I.import_experts(store, "workbuddy-experts", ["alpha@experts#helper", "beta@experts#helper"])
    assert sorted(got["added"]) == ["Alpha Helper", "Beta Helper"]
    assert {a["origin"] for a in imported(store)} == {
        "workbuddy:alpha@experts#helper", "workbuddy:beta@experts#helper"}
    assert I.import_experts(store, "workbuddy-experts",
                            ["alpha@experts#helper"])["added"] == []


def test_an_import_from_before_the_package_was_part_of_the_key_is_still_recognised(home):
    """Members imported by an earlier version carry `workbuddy:<slug>`. Reading those as a
    different expert would import a second copy instead of skipping."""
    store = Store(home / "data")
    install(home, "academic-writing", agents={"academic-writing.md": expert_md()})
    store.create_agent("Imported earlier", "✍️", "", "x", None, [], [],
                       origin="workbuddy:academic-writing")
    assert servers_of(store, "workbuddy-experts")[0]["exists"] is True


def test_a_source_cut_short_says_the_list_is_partial(home, monkeypatch):
    """A source holding more entries than the cap hands back the cap — without saying so, the
    total never exceeds the global limit either, so the answer claimed to be complete."""
    store = Store(home / "data")
    monkeypatch.setattr(I, "MAX_ITEMS", 2)
    for n in range(3):
        install(home, f"pkg{n}", agents={f"a{n}.md": expert_md(key=f"a{n}", en=f"Expert {n}")})
    got = I.scan(store, ["workbuddy-experts"])
    assert len(got["items"]) <= 2
    assert got["truncated"] is True, "the cap on one source has to reach the answer"
    assert any("partial" in n for n in got["notes"]), got["notes"]


def test_a_directory_symlinked_into_a_source_is_not_read(home):
    """`p.is_symlink()` only covers the last component. With `connectors/demo` pointing out of
    the tree, the `mcp.json` underneath is an ordinary file at an ordinary-looking path, and
    reading it reaches somewhere the source never declared."""
    store = Store(home / "data")
    outside = home.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / "mcp.json").write_text(
        '{"mcpServers": {"planted": {"url": "https://planted.example.com/mcp"}}}', encoding="utf-8")
    (home / ".workbuddy" / "connectors").mkdir(parents=True, exist_ok=True)
    (home / ".workbuddy" / "connectors" / "demo").symlink_to(outside, target_is_directory=True)
    assert servers_of(store, "workbuddy-connectors") == []
