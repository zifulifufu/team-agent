"""Vector search: the client, the protocol, and the rules that keep it honest.

Four claims are being defended here, and each of them was a decision rather than an accident:

1. **The two halves agree on the wire.** `scripts/embed-server.py` runs in another interpreter
   (arm64, Python 3.12, torch) because this app's own one cannot load a model at all — so the
   protocol is the only thing holding the feature together. These tests drive the real handler
   over a real socket, in this process, with a fake encoder standing in for the model.
2. **Down is a state, not a crash.** A search with no model still answers, on keywords, and says
   which half it used.
3. **Nothing is fetched behind the reader's back.** A missing model is reported with the command
   that would fetch it, never downloaded because somebody typed a query.
4. **A row that cannot be used is dropped, not shifted.** The failure mode of forgetting this is
   a hit that quietly returns the neighbouring passage.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import threading
from http.server import ThreadingHTTPServer

import httpx
import numpy as np
import pytest

from app import embed
from app.textindex import rrf

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "embed-server.py"


def load_script():
    """The sidecar, imported as a module — its `main()` is guarded, so this starts nothing."""
    spec = importlib.util.spec_from_file_location("embed_server_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakeBed:
    """Stands in for the model: a deterministic vector per text, no torch anywhere."""

    def __init__(self, dim: int = 3):
        self.model_id = "fake/model"
        self.dim = dim
        self.state = "ready"
        self.device = "cpu"
        self.error = ""
        self.hint = ""
        self.max_seq = 512
        self.downloaded = False
        self.load_seconds = 0.0
        self.started = 0.0
        self.embedded = 0
        self.allow_download = False

    def encode(self, texts):
        return [[float(i), float(len(t) % 10), 1.0] for i, t in enumerate(texts)]

    def status(self):
        return {"ready": self.state == "ready", "state": self.state, "model": self.model_id,
                "device": self.device, "dim": self.dim, "max_seq": self.max_seq, "error": self.error,
                "hint": self.hint, "downloaded_now": self.downloaded, "load_seconds": 0.0,
                "up_seconds": 0.0, "embedded": self.embedded, "max_batch": 64,
                "allow_download": self.allow_download}


@pytest.fixture
def sidecar():
    """The real handler, on a real port, in a thread. Yields (base url, the fake bed)."""
    mod = load_script()
    bed = FakeBed()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(bed))
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/v1", bed
    finally:
        httpd.shutdown()
        httpd.server_close()


def settings(base: str = "", **over):
    out = {"embed_enabled": True, "embed_model": "fake/model", "embed_batch": 4,
           "embed_timeout": 30, "embed_autostart": False, "embed_base_url": base or embed.DEFAULT_BASE}
    out.update(over)
    return out


# ------------------------------------------------------------------ the wire, both ends
async def test_the_health_report_says_up_and_ready_are_different_questions(sidecar):
    """A server that is up but still loading must not read as ready: the model takes ~2 minutes
    to load on a cold Metal kernel, and a caller that confuses the two waits for it."""
    base, bed = sidecar
    bed.state = "loading"
    st = await embed.health(settings(base))
    assert st["up"] is True and st["ready"] is False and st["state"] == "loading"
    bed.state = "ready"
    st = await embed.health(settings(base))
    assert st["ready"] is True and st["dim"] == 3 and st["model"] == "fake/model"


async def test_vectors_survive_the_round_trip_through_the_real_server(sidecar):
    base, _bed = sidecar
    vecs = await embed.embed_texts(settings(base), ["alpha", "beta"])
    assert vecs.shape == (2, 3)
    assert [round(float(v), 3) for v in vecs[1]] == [1.0, 4.0, 1.0], "the second text is the second row"


async def test_the_client_lines_results_up_by_index_not_by_arrival():
    """The wire carries an `index` for a reason. A server that labels its vectors correctly but
    returns them in another order must not silently give every answer its neighbour's vector."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["encoding_format"] == "base64", "indexing must not send 1024 decimals per row"
        assert body["model"] == "fake/model"
        blobs = [base64.b64encode(np.asarray([float(i), 0, 0], dtype="<f4").tobytes()).decode()
                 for i in range(len(body["input"]))]
        data = [{"object": "embedding", "index": i, "embedding": b} for i, b in reversed(list(enumerate(blobs)))]
        return httpx.Response(200, json={"object": "list", "data": data, "dim": 3})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await embed.embed_texts(settings(), ["a", "b", "c"], client=c)
    assert [round(float(v[0]), 3) for v in got] == [0.0, 1.0, 2.0]


def test_base64_is_the_same_encoding_on_both_sides():
    """float32 little-endian, exactly as OpenAI does it — four times smaller on the wire than a
    JSON array of decimals, and the endianness is the part that would fail quietly."""
    mod = load_script()
    blob = mod._floats_to_base64([[1.5, -2.25, 0.0]])[0]
    assert [float(x) for x in embed._decode(blob)] == [1.5, -2.25, 0.0]


async def test_a_loading_model_answers_503_rather_than_failing_the_request(sidecar):
    base, bed = sidecar
    bed.state = "loading"
    with pytest.raises(embed.EmbedError) as e:
        await embed.embed_texts(settings(base), ["x"])
    assert "loading" in str(e.value), "the server's own state has to reach the caller"


async def test_an_error_state_carries_the_command_that_would_fix_it(sidecar):
    base, bed = sidecar
    bed.state = "error"
    bed.error = "the weights are not in the local cache"
    bed.hint = "fetch it with: HF_ENDPOINT=https://hf-mirror.com python embed-server.py"
    st = await embed.health(settings(base))
    assert st["state"] == "error" and "hf-mirror" in st["hint"]
    with pytest.raises(embed.EmbedError) as e:
        await embed.embed_texts(settings(base), ["x"])
    assert "hf-mirror" in str(e.value), "a diagnosis without the fix is only half the message"


def test_a_batch_bigger_than_the_server_takes_is_refused_rather_than_half_done(sidecar):
    base, _bed = sidecar
    r = httpx.post(base + "/embeddings", json={"model": "fake/model", "input": ["x"] * 65}, timeout=10)
    assert r.status_code == 400 and "at most 64" in r.json()["error"]["message"]


def test_a_body_that_is_not_json_is_a_400_and_not_a_crash(sidecar):
    base, _bed = sidecar
    r = httpx.post(base + "/embeddings", content=b"{not json", timeout=10)
    assert r.status_code == 400
    r = httpx.post(base + "/embeddings", json={"input": 5}, timeout=10)
    assert r.status_code == 400 and "string" in r.json()["error"]["message"]
    r = httpx.post(base + "/embeddings", json={"input": []}, timeout=10)
    assert r.status_code == 400


def test_an_unknown_path_is_a_404_not_a_hang(sidecar):
    base, _bed = sidecar
    assert httpx.get(base + "/nope", timeout=10).status_code == 404
    assert httpx.get(base.replace("/v1", "") + "/v1/models", timeout=10).status_code == 200


def test_health_answers_under_the_address_a_person_configures(sidecar):
    """The address is OpenAI-style and ends in `/v1`, so `/v1/health` has to answer.

    This was a real bug: it 404ed, and a 404 was reported as "up but not ready" — so `ensure()`
    polled a perfectly healthy server until it timed out, and every search quietly fell back to
    keywords while the model sat there ready. Both spellings are pinned here.
    """
    base, _bed = sidecar
    assert httpx.get(base + "/health", timeout=10).status_code == 200
    assert httpx.get(base.replace("/v1", "") + "/health", timeout=10).status_code == 200


# ------------------------------------------------------------------ the client alone
async def test_a_down_server_is_reported_with_the_command_to_start_it():
    with pytest.raises(embed.EmbedError) as e:
        await embed.embed_texts(settings("http://127.0.0.1:1/v1"), ["x"], timeout=2)
    assert "embed-server.py" in str(e.value) and "hf-mirror" in str(e.value)


async def test_query_vector_never_starts_a_server(monkeypatch):
    """The read path stays a read path: starting a 2 GB model because somebody typed a query is
    exactly the behaviour these tests exist to prevent."""
    def boom(*_a, **_k):
        raise AssertionError("a search must not start the embedding server")

    monkeypatch.setattr(embed, "start", boom)
    vec, note = await embed.query_vector(settings("http://127.0.0.1:1/v1"), "moyamoya")
    assert vec is None and "embed-server.py" in note


async def test_asking_with_vectors_off_is_silent_not_a_complaint():
    vec, note = await embed.query_vector(settings(embed_enabled=False), "moyamoya")
    assert vec is None and note == "", "switching it off is a choice, not a failure to report"


async def test_a_search_without_a_model_says_so_rather_than_looking_empty(monkeypatch):
    """The failure this prevents: a library full of vectors, searched by keywords only, returns a
    thin result — and thin reads exactly like "your library says nothing about this"."""
    monkeypatch.setattr(embed, "start", lambda *_a, **_k: {"started": False, "pid": 0, "reason": "no"})
    vec, note = await embed.query_vector(settings("http://127.0.0.1:1/v1"), "moyamoya")
    assert vec is None and note, "the reader must be told the search was keywords-only"


def test_weights_are_looked_for_on_disk_and_never_fetched_by_a_search(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    assert embed.cached("BAAI/bge-m3") is False
    snap = tmp_path / "hub" / "models--BAAI--bge-m3" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    assert embed.cached("BAAI/bge-m3") is False, "a snapshot with no weights is not the weights"
    (snap / "model.safetensors").write_bytes(b"x")
    assert embed.cached("BAAI/bge-m3") is True


def test_a_remote_address_is_never_started_locally():
    got = embed.start(settings("https://api.example.com/v1"))
    assert got["started"] is False and got["reason"], "and it says why rather than staying quiet"


def test_a_missing_environment_is_named_with_the_way_to_build_it(monkeypatch, tmp_path):
    monkeypatch.setattr(embed, "ROOT", tmp_path)
    monkeypatch.delenv("TEAM_AGENT_EMBED_VENV", raising=False)
    got = embed.start(settings(), data_dir=str(tmp_path))
    assert got["started"] is False
    assert ".venv-embed" in got["reason"] and "sentence-transformers" in got["reason"]


def test_the_fetch_command_carries_both_local_quirks():
    """This machine: huggingface.co does not resolve, and huggingface_hub 1.x otherwise tries its
    Xet backend, which the mirror answers with a 401."""
    said = embed.fetch_command(settings())
    assert "HF_ENDPOINT=https://hf-mirror.com" in said and "HF_HUB_DISABLE_XET=1" in said


def test_without_numpy_the_feature_says_so_instead_of_failing_obscurely(monkeypatch):
    monkeypatch.setattr(embed, "np", None)
    assert embed.available() is False
    assert "numpy" in embed.reason_missing(settings())


# ------------------------------------------------------------------ vectors on disk
def test_a_row_that_cannot_be_used_does_not_shift_the_ones_after_it():
    """The bug this guards: dropping a missing row changes the index of every row after it, so
    every hit past that point returns the neighbouring passage — a search that is wrong and looks
    confident. `matrix` therefore returns the mapping, and the caller is not allowed to guess."""
    good = embed.pack(np.zeros((2, 4), dtype="float32"))
    mat, keep = embed.matrix([good[0], None, good[1]], 4)
    assert mat.shape == (2, 4) and keep == [0, 2]
    wrong_width = np.zeros(3, dtype="float16").tobytes()
    mat, keep = embed.matrix([good[0], wrong_width], 4)
    assert keep == [0] and mat.shape == (1, 4)


def test_the_scope_filter_is_applied_after_scoring_and_cannot_leak():
    mat = np.eye(4, dtype="float32")
    positions = [10, 11, 12, 13]
    docs = ["a", "b", "c", "d"]
    q = np.array([0.0, 0.0, 1.0, 0.0], dtype="float32")
    got = embed.best_per_document(q, mat, positions, docs)
    assert got[0] == ("c", 12, 1.0) and len(got) == 4, "the match leads, the rest follow at zero"
    assert [(d, at) for d, at, _s in embed.best_per_document(q, mat, positions, docs, allow={10, 11})] \
        == [("a", 10), ("b", 11)]
    assert embed.best_per_document(q, mat, positions, docs, allow=set()) == [], "nothing in scope means nothing"
    assert embed.best_per_document(np.zeros(3, dtype="float32"), mat, positions, docs) == [], "a wrong width is not a match"


def test_a_document_is_represented_by_its_best_passage_not_by_all_of_them():
    """The reason a search answers with documents rather than passages: a note averages seventeen
    passages, so seventeen entries of one page would fill a five-line answer."""
    mat = np.asarray([[0.90, 0.0], [0.95, 0.0], [0.10, 0.0]], dtype="float32")
    q = np.asarray([1.0, 0.0], dtype="float32")
    got = [(d, at, round(s, 3)) for d, at, s in embed.best_per_document(q, mat, [0, 1, 2], ["note", "note", "other"])]
    assert got == [("note", 1, 0.95), ("other", 2, 0.1)], "the best passage stands for the note"
    two_rows = mat[:2].copy()
    once = embed.best_per_document(q, two_rows, [0, 1], ["note", "note"])
    assert [(d, at) for d, at, _s in once] == [("note", 1)], "and a note appears once, not once per passage"
    # A mapping that does not line up with the matrix is a caller bug, and the honest answer is no
    # result at all: guessing would hand back the neighbouring passage.
    assert embed.best_per_document(q, mat, [0, 1], ["note", "note"]) == []
    assert embed.best_per_document(q, mat, [0, 1, 2], ["note", "note"]) == []


# ------------------------------------------------------------------ fusion
def test_a_passage_found_by_both_retrievers_comes_first():
    """The whole point of fusing by rank: an exact word and a paraphrase should reinforce each
    other rather than compete, and a hit only one of them found cannot outrank it."""
    keyword = [7, 3, 9]
    vector = [3, 5, 7]
    fused = rrf([keyword, vector])
    assert fused[0][0] == 3, "found by both, so first"
    assert [i for i, _ in fused][:2] == [3, 7]


def test_fusion_is_deterministic_and_survives_an_empty_list():
    assert rrf([[1, 2], [2, 1]]) == rrf([[1, 2], [2, 1]])
    assert rrf([[], [1]]) == [(1, 1 / 61)]
    assert rrf([]) == []


def test_only_the_head_of_a_long_ranking_matters():
    """RRF reads positions, so rank 3000 contributes 1/3060 — the cap keeps a 60k-passage index
    from being walked to the end on every query."""
    long_list = list(range(5000))
    assert rrf([long_list])[0][0] == 0
    assert len(rrf([long_list])) == 5000
