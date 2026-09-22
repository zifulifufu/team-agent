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
        if src["format"] in ("tree", "glob"):
            assert (src.get("root") or src.get("glob") or "").startswith("~"), src["key"]
        assert src["kind"] in ("mcp", "skill"), f"{src['key']} declares an unsupported kind"
        assert src["format"] in ("json", "toml", "tree", "glob")
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
