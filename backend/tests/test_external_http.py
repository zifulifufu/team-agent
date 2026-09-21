"""External members on a chat gateway (Cherry Studio's local API gateway, MetaChat): engine
selection, config validation, one turn over HTTP, and the rule that the key never leaves the
backend.

The gateway is never the real one — `tests/fakes.py` serves an OpenAI-compatible
/chat/completions over a real socket, so the whole network path is exercised.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import external
from app.external import ExternalError, ExternalRunner, clean_cfg, kind_of
from app.main import create_app
from tests.conftest import FakeLLM
from tests.fakes import FakeServer, openai_like


def runner_for(tmp_path) -> ExternalRunner:
    return ExternalRunner(tmp_path / "data")


def gateway_cfg(url: str, **over) -> dict:
    return clean_cfg({"base_url": url, "api_key": "k", "model": "gpt-5", **over}, engine="cherry")


# ------------------------------------------------------------------ engine selection
def test_the_engine_list_has_a_command_line_and_two_gateways():
    assert kind_of("workbuddy") == "cli"
    assert kind_of("cherry") == "http" and kind_of("metachat") == "http"
    # anything unknown stays on the stricter path: launcher lookup, permissions, subprocess
    assert kind_of("some-future-engine") == "cli"
    for eid in ("cherry", "metachat"):
        meta = external.ENGINES[eid]
        assert meta["base_url"].startswith("http") and meta["avatar"]
        # both languages, like the workbuddy entry, so the member is created in the right one
        assert meta["prompt"] and meta["prompt_zh"] and meta["role_zh"]


def test_the_overview_offers_each_engine_with_its_kind_and_address(tmp_path):
    cl, _ = _client(tmp_path)
    engines = {e["id"]: e for e in cl.get("/api/external").json()["engines"]}
    assert set(engines) == {"workbuddy", "cherry", "metachat"}
    assert engines["workbuddy"]["kind"] == "cli"
    assert engines["cherry"]["kind"] == "http"
    assert engines["cherry"]["base_url"].endswith(":23333/v1")
    assert engines["metachat"]["base_url"].startswith("https://")
    assert engines["cherry"]["key_hint"]                     # tells the user where the key comes from


# ------------------------------------------------------------------ config validation
def test_a_gateway_config_checks_the_address_and_the_key():
    c = clean_cfg({"base_url": " http://127.0.0.1:23333/v1/ ", "api_key": "cs-sk-abc", "model": "gpt-5"},
                  engine="cherry")
    assert c["base_url"] == "http://127.0.0.1:23333/v1"      # trailing slash trimmed, not doubled later
    assert c["api_key"] == "cs-sk-abc"
    with pytest.raises(ValueError):
        clean_cfg({"base_url": "127.0.0.1:23333"}, engine="cherry")
    with pytest.raises(ValueError):
        clean_cfg({"api_key": "line\nbreak"}, engine="cherry")
    with pytest.raises(ValueError):
        clean_cfg({"base_url": "https://" + "x" * 300}, engine="metachat")


def test_a_command_line_path_is_ignored_for_a_gateway():
    """A gateway has no command line, so a path that would be rejected for the CLI engine is simply
    not looked at — and it is not carried into the gateway member's settings either."""
    c = clean_cfg({"cli_path": "/definitely/not/here/codebuddy"}, engine="metachat")
    assert c["cli_path"] == ""
    assert clean_cfg({}, engine="cherry")["level"] == "read"


# ------------------------------------------------------------------ one turn over HTTP
def test_a_gateway_turn_streams_its_reply(tmp_path):
    with FakeServer(openai_like("来自 Cherry 的回答")) as srv:
        runner = runner_for(tmp_path)
        seen: list[str] = []

        async def collect(d: str) -> None:
            seen.append(d)

        out = asyncio.run(runner._run_http("cherry", gateway_cfg(f"{srv.url}/v1"), system="be brief",
                                           prompt="你好", on_delta=collect))
    assert "来自 Cherry 的回答" in out.text
    assert "".join(seen) == out.text          # what the UI streamed is what the turn returned
    assert out.model == "gpt-5" and out.num_turns == 1


def test_a_gateway_needs_a_model_name(tmp_path):
    """A chat gateway cannot pick a model by itself, so this is refused before anything goes out.
    Built-in messages are bilingual, hence the two spellings."""
    runner = runner_for(tmp_path)
    with pytest.raises(ExternalError) as err:
        asyncio.run(runner._run_http("cherry", clean_cfg({"model": ""}, engine="cherry"), system="", prompt="hi"))
    assert "model" in str(err.value).lower() or "模型" in str(err.value)


def test_a_rejected_key_is_explained_rather_than_dumped(tmp_path):
    with FakeServer(openai_like("nope", status=401)) as srv:
        runner = runner_for(tmp_path)
        with pytest.raises(ExternalError) as err:
            asyncio.run(runner._run_http("cherry", gateway_cfg(f"{srv.url}/v1"), system="", prompt="hi"))
    msg = str(err.value)
    assert "401" in msg and ("key" in msg.lower() or "密钥" in msg)


def test_a_dead_endpoint_says_so(tmp_path):
    runner = runner_for(tmp_path)
    cfg = gateway_cfg("http://127.0.0.1:1/v1")            # nothing listens there
    with pytest.raises(ExternalError) as err:
        asyncio.run(runner._run_http("metachat", cfg, system="", prompt="hi"))
    assert "metachat" in str(err.value).lower()


def test_probe_talks_to_a_gateway_without_a_command_line(tmp_path):
    with FakeServer(openai_like("OK")) as srv:
        runner = runner_for(tmp_path)
        res = asyncio.run(runner.probe(gateway_cfg(f"{srv.url}/v1"), live=True, engine="cherry"))
    assert res["via"] == "http" and res["found"] is True
    assert res["live"]["ok"] is True and "OK" in res["live"]["reply"]
    # no version to read here, and no workspace was created for it either
    assert res["version"] == ""
    assert not (tmp_path / "data" / "external" / "_probe" / "workspace").exists()


def test_a_gateway_member_does_not_get_a_workspace(tmp_path):
    cl, app = _client(tmp_path)
    store = app.state.store
    store.update_settings({"external_agents_enabled": True})
    r = cl.post("/api/external/agents", json={"engine": "cherry", "cfg": {"base_url": "http://127.0.0.1:23333/v1",
                                                                        "model": "gpt-5"}})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert cl.get("/api/external").json()["members"][0]["workspace"] == ""
    assert not (Path(store.data_dir) / "external" / aid).exists()   # nothing was created for it


# ------------------------------------------------------------------ the key never comes back
def _client(tmp_path, name="data"):
    app = create_app(tmp_path / name, completion_fn=FakeLLM(default="OK"))
    return TestClient(app, base_url="http://127.0.0.1"), app


def test_the_gateway_key_is_never_returned(tmp_path):
    cl, app = _client(tmp_path)
    store = app.state.store
    store.update_settings({"external_agents_enabled": True})
    secret = "cs-sk-super-secret"
    resp = cl.post("/api/external/agents", json={
        "engine": "cherry",
        "cfg": {"base_url": "http://127.0.0.1:23333/v1", "api_key": secret, "model": "gpt-5"},
    })
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert secret not in json.dumps(created, ensure_ascii=False)
    assert created["engine_cfg"]["api_key"] == "" and created["engine_cfg"]["has_key"] is True

    listed = cl.get("/api/external").json()["members"][0]
    assert secret not in json.dumps(listed, ensure_ascii=False)
    assert listed["cfg"]["has_key"] is True

    # the placeholder the UI gets back means "unchanged": the stored key survives a settings save
    before = store.get_agent(created["id"])["engine_cfg"]["api_key"]
    cl.patch(f"/api/external/agents/{created['id']}", json={"cfg": {"api_key": "***", "model": "gpt-5-mini"}})
    after = store.get_agent(created["id"])["engine_cfg"]
    assert after["api_key"] == before and after["model"] == "gpt-5-mini"

    # a genuinely new key replaces it
    cl.patch(f"/api/external/agents/{created['id']}", json={"cfg": {"api_key": "cs-sk-rotated"}})
    assert store.get_agent(created["id"])["engine_cfg"]["api_key"] != before


def test_creating_a_gateway_member_names_it_after_the_engine(tmp_path):
    cl, app = _client(tmp_path)
    app.state.store.update_settings({"external_agents_enabled": True})
    assert cl.post("/api/external/agents", json={"engine": "metachat"}).json()["name"] == "MetaChat"
    # a member name may not contain a space, so the engine's display name is squeezed into one
    assert cl.post("/api/external/agents", json={"engine": "cherry"}).json()["name"] == "CherryStudio"


def test_an_unknown_engine_is_refused(tmp_path):
    cl, app = _client(tmp_path)
    app.state.store.update_settings({"external_agents_enabled": True})
    assert cl.post("/api/external/agents", json={"engine": "nope"}).status_code == 400
