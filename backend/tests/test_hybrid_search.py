"""Hybrid retrieval over a knowledge base: keywords and meaning, and the guards around both.

The claim under test is specific. A keyword index answers a question phrased with the *same words*
as the passage and fails at everything else — a paraphrase, or a Chinese question against an
English library. A vector index answers the paraphrase and is worse than keywords at an exact
identifier. Neither replaces the other, so both run and the rankings are fused by position.

The guards are the other half, and each one is a way this could be wrong while looking right:

* a passage outside the group's scope must not arrive through the vector half either;
* vectors made by a *different* model must be left out rather than averaged in — they are not
  comparable, and mixing them returns confident nonsense;
* a passage that has no vector must not shift the position of the ones that do.
"""

from __future__ import annotations

import numpy as np
import pytest

from app import embed
from app.library import Library
from app.main import create_app

KB = "kb-test"
MODEL = "fake/model"


def build(store, docs: dict[str, str]) -> Library:
    store.add_kb("Test knowledge base", kid=KB)
    store.update_settings({"embed_model": MODEL, "embed_enabled": True, "embed_autostart": False})
    lib = Library(store)
    for title, body in docs.items():
        lib.add_text(title, body, kb_id=KB)
    return lib


def give(store, lib: Library, title: str, *vec: float, model: str = MODEL) -> str:
    """Attach a vector to a document's only passage, the way indexing does."""
    doc = lib.find_by_title(title)
    assert doc is not None, title
    blob = embed.pack(np.asarray([list(vec)], dtype="float32"))[0]
    store.set_chunk_vectors(doc["id"], {0: blob}, model)
    lib.invalidate()
    return doc["id"]


DOCS = {
    "Ophthalmic Artery": "The ophthalmic artery arises from the internal carotid above the "
                         "cavernous sinus and gives off the central retinal artery.",
    "Moyamoya disease": "Progressive stenosis of the terminal internal carotid arteries with basal "
                        "collaterals. Revascularisation is the treatment; Suzuki stages it.",
    "Pulsatile Tinnitus": "Pulsatile tinnitus has a vascular cause in most cases: dural "
                          "arteriovenous fistula, carotid stenosis, and venous sinus stenosis.",
}


# ------------------------------------------------------------------ the keyword half, unchanged
def test_keywords_alone_still_answer(store):
    lib = build(store, DOCS)
    hits = lib.search("pulsatile tinnitus vascular cause", 3)
    assert hits and hits[0]["title"] == "Pulsatile Tinnitus"
    assert hits[0]["via"] == "keyword" and hits[0]["score"] > 0


def test_a_query_vector_of_the_wrong_width_is_ignored_rather_than_misread(store):
    """A library indexed with 1024 dimensions and a 3-dimension query is a mistake somewhere, and
    the honest answer is the keyword result — not a cosine of two different spaces."""
    lib = build(store, DOCS)
    give(store, lib, "Moyamoya disease", 0, 1, 0)
    hits = lib.search("moyamoya", 3, None, np.asarray([0.0, 1.0], dtype="float32"))
    assert hits and hits[0]["title"] == "Moyamoya disease"
    assert all(h["via"] == "keyword" for h in hits)


# ------------------------------------------------------------------ the vector half
def test_a_paraphrase_is_found_by_the_vector_half(store):
    """The whole reason this exists: `revascularization` is nowhere in the passage about
    revascularisation, and a Chinese question about a bypass will not be either."""
    lib = build(store, DOCS)
    give(store, lib, "Moyamoya disease", 1, 0, 0)
    hits = lib.search("bypass surgery for a steno-occlusive arteriopathy", 3, None,
                      np.asarray([1.0, 0.0, 0.0], dtype="float32"))
    assert hits and hits[0]["title"] == "Moyamoya disease"
    assert hits[0]["via"] == "vector" and hits[0]["sim"] > 0.9


def test_a_passage_found_by_both_retrievers_comes_first(store):
    """Ranks reinforce rather than compete: the passage the words point at *and* the vector points
    at must outrank one that either found alone."""
    lib = build(store, DOCS)
    give(store, lib, "Ophthalmic Artery", 0.2, 0, 0)      # the vector's favourite
    give(store, lib, "Moyamoya disease", 1.0, 0, 0)        # the words' favourite
    hits = lib.search("moyamoya revascularisation Suzuki", 3, None,
                      np.asarray([1.0, 0.0, 0.0], dtype="float32"))
    assert hits[0]["title"] == "Moyamoya disease" and hits[0]["via"] == "both"
    assert [h["title"] for h in hits[1:]] , "and the rest are still ranked, not dropped"


def test_the_scope_filter_applies_to_the_vector_half_too(store):
    """A group may read some knowledge bases and not others. A cosine of 1.0 cannot be allowed to
    carry a passage across that line."""
    lib = build(store, DOCS)
    mine = give(store, lib, "Moyamoya disease", 0, 0, 1)
    hidden = give(store, lib, "Pulsatile Tinnitus", 1, 0, 0)
    assert hidden != mine
    hits = lib.search("anything at all", 5, [mine], np.asarray([1.0, 0.0, 0.0], dtype="float32"))
    assert all(h["doc_id"] == mine for h in hits)
    assert hits, "the vector half still has to answer inside the scope it was given"


def test_vectors_from_another_model_are_left_out_rather_than_mixed_in(store):
    lib = build(store, DOCS)
    give(store, lib, "Moyamoya disease", 1, 0, 0, model="someone/else")
    st = lib.vector_status()
    assert st["with_vectors"] == 1 and st["in_index"] == 0, "counted, but deliberately not searchable"
    assert st["note"], "and the reason is available to show"
    hits = lib.search("moyamoya", 3, None, np.asarray([1.0, 0.0, 0.0], dtype="float32"))
    assert all(h["via"] == "keyword" for h in hits)


def test_the_status_says_what_search_would_do(store):
    lib = build(store, DOCS)
    give(store, lib, "Moyamoya disease", 1, 0, 0)
    st = lib.vector_status()
    assert st["enabled"] is True and st["model"] == MODEL and st["in_index"] == 1
    assert st["chunks"] == 3 and st["with_vectors"] == 1 and st["missing"] == 2
    assert st["numpy"] is True and st["venv"] is True


# ------------------------------------------------------------------ indexing
async def test_indexing_writes_vectors_and_resumes_where_it_stopped(store, monkeypatch):
    lib = build(store, DOCS)
    calls: list[list[str]] = []

    async def fake(_settings, texts, **_kw):
        calls.append(list(texts))
        return np.tile(np.asarray([[0.0, 0.0, 1.0]], dtype="float32"), (len(texts), 1))

    monkeypatch.setattr(embed, "embed_texts", fake)
    first = await lib.index_vectors(page=2)
    assert first["indexed"] == 3 and first["left"] == 0 and first["model"] == MODEL
    assert len(calls) == 2, "paged, so progress exists and a crash keeps the pages before it"
    assert store.vector_coverage()["with_vectors"] == 3
    again = await lib.index_vectors()
    assert again["indexed"] == 0 and calls == calls[:2], "nothing is re-embedded on a second run"
    docs = store.list_docs(KB)
    assert {d["embed_model"] for d in docs} == {MODEL}, "the model travels with the document"


async def test_a_batch_that_fails_writes_nothing_for_that_batch(store, monkeypatch):
    """A half-indexed passage would be one that is silently unsearchable by meaning while looking
    indexed from the outside — the worst of both."""
    lib = build(store, DOCS)

    async def boom(*_a, **_k):
        raise embed.EmbedError("the service is not running")

    monkeypatch.setattr(embed, "embed_texts", boom)
    report = await lib.index_vectors()
    assert report["indexed"] == 0 and report["errors"] and "not running" in report["errors"][0]
    assert store.vector_coverage()["with_vectors"] == 0


async def test_indexing_can_be_capped_so_a_first_run_is_a_pilot(store, monkeypatch):
    lib = build(store, DOCS)

    async def fake(_settings, texts, **_kw):
        return np.tile(np.asarray([[1.0, 0.0, 0.0]], dtype="float32"), (len(texts), 1))

    monkeypatch.setattr(embed, "embed_texts", fake)
    report = await lib.index_vectors(max_chunks=1)
    assert report["indexed"] == 1 and report["left"] == 2


def test_vectors_can_be_thrown_away_to_reindex_with_another_model(store):
    lib = build(store, DOCS)
    give(store, lib, "Moyamoya disease", 1, 0, 0)
    assert store.vector_coverage()["with_vectors"] == 1
    assert store.clear_chunk_vectors() == 1
    assert store.vector_coverage()["with_vectors"] == 0
    assert store.get_doc(lib.find_by_title("Moyamoya disease")["id"])["embed_model"] == ""


def test_coverage_counts_come_from_the_database_not_from_memory(store):
    lib = build(store, DOCS)
    assert store.vector_coverage() == {"chunks": 3, "with_vectors": 0, "missing": 3, "models": {}}
    give(store, lib, "Moyamoya disease", 1, 0, 0)
    cov = store.vector_coverage()
    assert cov["with_vectors"] == 1 and cov["models"] == {MODEL: 1}


# ------------------------------------------------------------------ what must not happen
def test_building_the_app_does_not_load_a_model(store, tmp_path, monkeypatch):
    """`create_app` runs on every start, in every test, and in the packaging step. Starting a 2 GB
    model there would be a surprise with a bill attached. It happens in the factory that is *meant*
    to do it, or because a person asked — never as a side effect of opening the app."""
    def boom(*_a, **_k):
        raise AssertionError("create_app must not start the embedding server")

    monkeypatch.setattr(embed, "ensure", boom)
    monkeypatch.setattr(embed, "warm_in_background", boom)
    app = create_app(tmp_path / "data", completion_fn=_llm())
    assert app is not None


def _llm():
    from tests.conftest import FakeLLM
    return FakeLLM(default="ok")


# ------------------------------------------------------------------ the settings page's API
def client(tmp_path, name="vec"):
    from fastapi.testclient import TestClient
    app = create_app(tmp_path / name, completion_fn=_llm())
    return TestClient(app, base_url="http://127.0.0.1"), app


def test_the_status_endpoint_answers_what_a_screen_needs(tmp_path, monkeypatch):
    """One call, no side effects: coverage, the model, the server's own report, and the two
    commands. A page cannot assemble those itself, and asking three endpoints for them is how a
    status line ends up disagreeing with itself."""
    cl, _app = client(tmp_path)
    monkeypatch.setattr(embed, "health", _fake_health)
    st = cl.get("/api/library/vector").json()
    assert {"chunks", "with_vectors", "missing", "models", "enabled", "model", "address",
            "server", "start_command", "fetch_command", "weights", "job"} <= set(st)
    assert st["server"]["up"] is True and st["server"]["device"] == "cpu"
    assert "embed-server.py" in st["start_command"] and "hf-mirror" in st["fetch_command"]


async def _fake_health(_settings, **_kw):
    return {"up": True, "ready": True, "state": "ready", "device": "cpu", "dim": 3}


def test_starting_the_model_does_nothing_when_it_is_already_up(tmp_path, monkeypatch):
    """A second server on the same port is not an improvement, and starting one would fail in a way
    that looks like the first one is broken."""
    cl, _app = client(tmp_path)
    monkeypatch.setattr(embed, "health", _fake_health)
    monkeypatch.setattr(embed, "start", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("must not start a second server")))
    r = cl.post("/api/library/vector/start").json()
    assert r["started"] is False and r["ready"] is True


def test_dropping_the_vectors_leaves_the_documents_alone(tmp_path, monkeypatch):
    cl, app = client(tmp_path)
    lib = app.state.ctx.library
    lib.add_text("A note", "The ophthalmic artery arises from the internal carotid.", kb_id=_kb(app))
    doc = lib.find_by_title("A note")
    store = app.state.store
    store.set_chunk_vectors(doc["id"], {0: embed.pack(np.zeros((1, 3), dtype="float32"))[0]}, MODEL)
    assert store.vector_coverage()["with_vectors"] == 1
    out = cl.post("/api/library/vector/clear").json()
    assert out["cleared"] == 1
    assert store.vector_coverage()["with_vectors"] == 0
    assert store.get_doc(doc["id"])["title"] == "A note", "the text is still there"
    assert store.doc_chunks(doc["id"]), "and so are its passages"


def test_a_running_job_is_visible_and_a_second_one_is_refused(tmp_path, monkeypatch):
    cl, app = client(tmp_path)
    lib = app.state.ctx.library
    lib.add_text("A note", "Moyamoya disease with basal collaterals.", kb_id=_kb(app))

    async def fake(_settings, texts, **_kw):
        return np.tile(np.asarray([[1.0, 0.0, 0.0]], dtype="float32"), (len(texts), 1))

    monkeypatch.setattr(embed, "embed_texts", fake)
    monkeypatch.setattr(embed, "ensure", _fake_ensure)
    assert cl.post("/api/library/vector/index", json={}).json()["started"] is True
    # The job runs as a task, so the very next request sees it in flight. This is the guard that
    # keeps two runs from embedding the same passages twice and racing on the writes.
    app.state.ctx.vec_job["running"] = True
    second = cl.post("/api/library/vector/index", json={}).json()
    assert second["started"] is False and second["reason"]
    app.state.ctx.vec_job["running"] = False


async def _fake_ensure(settings, **_kw):
    return {"up": True, "ready": True, "state": "ready", "device": "cpu", "dim": 3}


def test_the_index_endpoint_names_a_knowledge_base_that_has_nothing_in_it(tmp_path):
    cl, _app = client(tmp_path)
    r = cl.post("/api/library/vector/index", json={"kb_id": "no-such-kb"})
    assert r.status_code == 404 and "no documents" in r.text


def _kb(app) -> str:
    return app.state.store.list_kbs()[0]["id"]
