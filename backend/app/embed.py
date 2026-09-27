"""Vector side of the knowledge base: the client, and the lifecycle of the local model server.

Why this is not a library call, in one paragraph: the app's virtualenv is x86_64 under Rosetta on
Python 3.14, and neither torch nor onnxruntime ships wheels for that; the native arm64 interpreter
that sits beside it can run the model, Metal included. So the model runs as a **separate local
process** (`scripts/embed-server.py`, started from `.venv-embed`) and this module is the client.

Three rules, each of them about not lying to the person searching:

* **A missing vector index is not a failure, it is a state.** Everything here degrades to the
  keyword index that already exists, and says so, with the one command that would fix it.
* **Nothing downloads by itself.** Fetching 2 GB of weights because somebody typed a query is not
  a search — so a server started without `--allow-download` reports a missing model instead.
* **The address is a setting, not a constant.** The wire format is OpenAI's `/v1/embeddings`, so a
  knowledge base indexed locally can be re-indexed through a cloud endpoint by changing one field.

The third of those is also why the model's own id travels with the document: vectors from two
different models are not comparable, and a library that mixed them silently would return
confident nonsense.
"""

from __future__ import annotations

from . import i18n

import asyncio
import os
import subprocess
import tempfile
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Optional on purpose: a library with no vectors still searches by keyword, and an app that
# refuses to start because numpy is absent would be trading a working feature for a missing one.
try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised by the "no numpy" tests instead
    np = None  # type: ignore[assignment]

DEFAULT_BASE = "http://127.0.0.1:8799/v1"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_PORT = 8799

# The app is the Electron shell's child, run from the repository; `.venv-embed` is its sibling.
ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = ROOT / "scripts" / "embed-server.py"

HEALTH_TIMEOUT = 2.0        # a health check that hangs is worse than one that says "down"
QUERY_TIMEOUT = 60.0        # one short query; the model is already warm by the time this matters
BATCH = 16                  # texts per request while filling the index
BATCH_TIMEOUT = 600.0       # a batch on a cold CPU model can take a while


class EmbedError(Exception):
    pass


# ------------------------------------------------------------------ what is configured
def base_url(settings: dict) -> str:
    return str(settings.get("embed_base_url") or DEFAULT_BASE).strip().rstrip("/")


def model_of(settings: dict) -> str:
    return str(settings.get("embed_model") or DEFAULT_MODEL).strip()


def enabled(settings: dict) -> bool:
    return bool(settings.get("embed_enabled", True))


def api_key(settings: dict) -> str:
    return str(settings.get("embed_api_key") or "").strip()


def _headers(settings: dict) -> dict[str, str]:
    key = api_key(settings)
    return {"authorization": f"Bearer {key}"} if key else {}


def _loopback(url: str) -> bool:
    """Whether this address is a server on this machine — the only kind we are allowed to start."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def available() -> bool:
    """Whether this build can hold vectors at all. Said plainly rather than discovered later."""
    return np is not None


def reason_missing(settings: dict) -> str:
    """Why vector search is unavailable, in words the reader can act on. Empty when it is fine."""
    if np is None:
        return i18n.pick_now(
            "Vector search needs numpy, which is not installed in this app's environment: "
            "run `pip install numpy` in backend/.",
            "向量检索需要 numpy,而这个应用的环境里没有装:请在 backend/ 目录里执行 `pip install numpy`。")
    return ""


# ------------------------------------------------------------------ the local server
def venv_python(settings: dict) -> str:
    """The interpreter that can run the model, or "" when there is not one.

    Resolution order: an explicit override (a different machine, a shared install), then the
    environment this repository ships, then a copy under the data directory for a packaged build.
    """
    env = os.environ.get("TEAM_AGENT_EMBED_VENV")
    data = os.environ.get("TEAM_AGENT_DATA") or ""
    cands = [env, str(ROOT / ".venv-embed"), str(Path(data) / "embed-venv") if data else ""]
    for cand in cands:
        if not cand:
            continue
        p = Path(cand) / "bin" / "python"
        if p.exists():
            return str(p)
    return ""


def cached(model: str) -> bool:
    """Whether the weights are already on disk.

    Checked by looking at the Hub cache rather than asking the server, because the point is to
    decide *without* starting one — and to be able to say "this will fetch 2 GB" honestly.
    """
    home = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(Path.home(), ".cache", "huggingface"), "hub")
    folder = "models--" + model.replace("/", "--")
    snap = Path(home) / folder / "snapshots"
    if not snap.is_dir():
        return False
    return any(any(f.suffix in (".safetensors", ".bin") for f in s.iterdir()) for s in snap.iterdir() if s.is_dir())


def _port(url: str) -> int:
    try:
        return int(urlparse(url).port or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def server_argv(settings: dict) -> list[str]:
    py = venv_python(settings)
    if not py:
        return []
    argv = [py, str(SERVER_SCRIPT), "--host", "127.0.0.1", "--port", str(_port(base_url(settings))),
            "--model", model_of(settings)]
    return argv


def fetch_command(settings: dict, *, download: bool = False) -> str:
    """The line a person can paste to make this work, with the mirror that actually resolves here.

    Both extras are there because of what this machine does: huggingface.co is unreachable, and
    huggingface_hub 1.x otherwise tries its Xet backend, which the mirror answers with a 401.
    """
    py = venv_python(settings) or str(ROOT / ".venv-embed" / "bin" / "python")
    tail = " --allow-download" if download else ""
    return (f"HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 "
            f"{py} {SERVER_SCRIPT}{tail}")


async def health(settings: dict, *, client: httpx.AsyncClient | None = None,
                 timeout: float = HEALTH_TIMEOUT) -> dict:
    """The server's own report, or a plain "down". Never raises: this is asked on a hot path."""
    url = f"{base_url(settings)}/health"
    own = client is None
    c = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        r = await c.get(url, timeout=timeout)
        if r.status_code >= 400:
            # A server that answers but refuses is a *configuration* problem — the wrong address,
            # or something else on the port — and must not be mistaken for a model still loading:
            # the difference is between a message now and a caller polling for five minutes.
            return {"up": True, "ready": False, "state": f"http {r.status_code}",
                    "error": f"{url} answered {r.status_code}"}
        body = r.json()
        body["up"] = True
        return body
    except Exception as e:  # noqa: BLE001 - "not running" is an answer, not an error
        return {"up": False, "ready": False, "state": "down", "error": f"{type(e).__name__}"}
    finally:
        if own:
            await c.aclose()


def start(settings: dict, *, data_dir: str = "", allow_download: bool = False) -> dict:
    """Start the local server detached, once. Returns `{"started": bool, "pid": int, "reason": str}`.

    Detached (its own session, its output in a log file) because the model takes tens of seconds to
    load and the app must not hold a pipe open for that long; and because a second app start should
    find the first one still warm rather than loading a duplicate 2 GB model.
    """
    if not _loopback(base_url(settings)):
        return {"started": False, "pid": 0, "reason": i18n.pick_now(
            "The embedding address is not on this machine, so nothing local is started.",
            "嵌入地址不在本机,所以不会启动本地服务。")}
    argv = server_argv(settings)
    if not argv:
        return {"started": False, "pid": 0, "reason": i18n.pick_now(
            f"The embedding environment is missing ({ROOT / '.venv-embed'}). Create it with "
            f"`uv venv --python 3.12 .venv-embed && uv pip install --python .venv-embed/bin/python "
            f"sentence-transformers`, then fetch the weights with: {fetch_command(settings, download=True)}",
            f"嵌入用的运行环境不存在({ROOT / '.venv-embed'})。先建它:"
            f"`uv venv --python 3.12 .venv-embed && uv pip install --python .venv-embed/bin/python "
            f"sentence-transformers`,再用这条命令取权重:{fetch_command(settings, download=True)}")}
    if allow_download:
        argv.append("--allow-download")
    log_dir = Path(data_dir) if data_dir else Path(os.environ.get("TEAM_AGENT_DATA") or (Path.home() / ".team-agent"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "embed-server.log", "ab", buffering=0)  # noqa: SIM115 - handed to the child
    except OSError:
        # A fixed filename in a shared temp directory can be a symlink owned by
        # another user. An anonymous, private file is safe to hand to the child.
        log = tempfile.TemporaryFile(mode="a+b", prefix="team-agent-embed-")
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HUB_DISABLE_XET", "1")   # the mirror cannot serve huggingface_hub's Xet backend
    try:
        p = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(ROOT), env=env)
    except OSError as e:
        return {"started": False, "pid": 0, "reason": i18n.pick_now(
            f"Could not start the embedding server: {e}", f"嵌入服务没能启动:{e}")}
    finally:
        log.close()
    return {"started": True, "pid": p.pid, "reason": ""}


async def ensure(settings: dict, *, data_dir: str = "", wait: float = 0.0,
                 allow_download: bool = False) -> dict:
    """Make sure a usable server is up, starting one if that is configured and `wait` > 0.

    `wait=0` is the honest default for a caller inside a turn: report the state, do not sit on it.
    A real wait is for the settings page and the indexing scripts, which are allowed to take minutes.
    """
    st = await health(settings)
    if st.get("ready"):
        return st
    if st.get("up") and str(st.get("state") or "").startswith("http "):
        # Something is listening and answering with an error. Waiting cannot fix that.
        return st
    if not bool(settings.get("embed_autostart", True)):
        return st
    if not allow_download and not cached(model_of(settings)):
        st["reason"] = i18n.pick_now(
            f"The weights of {model_of(settings)} are not on this machine yet. Fetch them first: "
            f"{fetch_command(settings, download=True)}",
            f"{model_of(settings)} 的权重还没下载到本机。先取权重:{fetch_command(settings, download=True)}")
        return st
    if st.get("up"):
        pass          # already running, merely loading: waiting here is right, starting a second copy is not
    else:
        got = start(settings, data_dir=data_dir, allow_download=allow_download)
        st["started"] = got.get("started")
        st["pid"] = got.get("pid")
        if not got.get("started") and got.get("reason"):
            st["reason"] = got["reason"]
            return st
    deadline = time.monotonic() + max(0.0, wait)
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        st = await health(settings)
        if st.get("ready") or (st.get("up") and st.get("state") == "error"):
            return st
    return st


# ------------------------------------------------------------------ asking for vectors
async def embed_texts(settings: dict, texts: list[str], *, client: httpx.AsyncClient | None = None,
                      batch: int = BATCH, timeout: float | None = None) -> "np.ndarray":
    """Vectors for `texts`, in order. Raises `EmbedError` with a sentence worth reading."""
    if np is None:
        raise EmbedError(reason_missing(settings))
    if not texts:
        return np.zeros((0, 0), dtype="float32")
    if timeout is None:
        timeout = float(settings.get("embed_timeout") or BATCH_TIMEOUT)
    own = client is None
    c = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
    rows: list["np.ndarray"] = []
    try:
        step = max(1, min(int(settings.get("embed_batch") or batch), 64))
        for i in range(0, len(texts), step):
            part = texts[i:i + step]
            payload = {"model": model_of(settings), "input": part, "encoding_format": "base64"}
            try:
                r = await c.post(f"{base_url(settings)}/embeddings", json=payload,
                                 headers=_headers(settings), timeout=timeout)
            except httpx.HTTPError as e:
                raise EmbedError(i18n.pick_now(
                    f"The embedding service at {base_url(settings)} could not be reached "
                    f"({type(e).__name__}). Start it with: {fetch_command(settings)}",
                    f"连不上嵌入服务 {base_url(settings)}({type(e).__name__})。启动它:{fetch_command(settings)}")) from None
            if r.status_code >= 400:
                raise EmbedError(_why(settings, r))
            data = (r.json() or {}).get("data") or []
            if len(data) != len(part):
                raise EmbedError(i18n.pick_now(
                    f"The embedding service returned {len(data)} vectors for {len(part)} texts",
                    f"嵌入服务对 {len(part)} 段文本只返回了 {len(data)} 个向量"))
            for e in sorted(data, key=lambda d: int(d.get("index") or 0)):
                rows.append(_decode(e.get("embedding")))
    finally:
        if own:
            await c.aclose()
    dim = max((r_.shape[0] for r_ in rows), default=0)
    out = np.zeros((len(rows), dim), dtype="float32")
    for i, r_ in enumerate(rows):
        if r_.shape[0] == dim:
            out[i] = r_
    return out


def _decode(value: object) -> "np.ndarray":
    import base64

    if isinstance(value, str):
        return np.frombuffer(base64.b64decode(value), dtype="<f4").astype("float32")
    return np.asarray(value, dtype="float32")


def _why(settings: dict, r: httpx.Response) -> str:
    """The service's own words when it has any — a bare status code is not a diagnosis."""
    said = ""
    try:
        body = r.json() or {}
        err = body.get("error") or {}
        said = str(err.get("message") or "")[:400] if isinstance(err, dict) else str(err)[:400]
        hint = str(err.get("hint") or "") if isinstance(err, dict) else ""
        if hint:
            said = f"{said} — {hint}" if said else hint
    except Exception:  # noqa: BLE001
        said = r.text[:200]
    if r.status_code == 503:
        return i18n.pick_now(
            f"The embedding service is not ready yet: {said or 'still loading'}",
            f"嵌入服务还没准备好:{said or '正在加载'}")
    return i18n.pick_now(
        f"The embedding service refused the request ({r.status_code}): {said or 'no message'}",
        f"嵌入服务拒绝了请求({r.status_code}):{said or '没有说明'}")


async def query_vector(settings: dict, text: str) -> tuple[object, str]:
    """(vector, why-not) for one query. Never raises: a search that cannot vectorise still runs.

    The second element is a sentence to pass on to whoever reads the result — a search that quietly
    used keywords only, over a library that has vectors, looks like a search that failed.
    """
    if not enabled(settings):
        return None, ""
    if reason_missing(settings):
        return None, reason_missing(settings)
    if not text.strip():
        return None, ""
    try:
        vec = await embed_texts(settings, [text], timeout=QUERY_TIMEOUT)
    except EmbedError as e:
        return None, str(e)
    return (vec[0] if vec.shape[0] else None), ""


def warm_in_background(settings: dict, *, data_dir: str = "") -> None:
    """Start the local model early, off the critical path, so the first real search finds it warm.

    Kept synchronous and fire-and-forget on purpose: `create_app` must not wait for a model to load,
    and a failure here has no user to tell — the search that later needs it will say so itself.
    """
    if not enabled(settings) or not _loopback(base_url(settings)):
        return
    if not bool(settings.get("embed_autostart", True)) or not cached(model_of(settings)):
        return
    if not venv_python(settings):
        return

    async def go() -> None:
        await ensure(settings, data_dir=data_dir, wait=120.0)

    try:
        asyncio.get_running_loop().create_task(go())
    except RuntimeError:      # called from a plain thread (a script, a test): just do it there
        threading_start(settings, data_dir=data_dir)


def threading_start(settings: dict, *, data_dir: str = "") -> None:
    def run() -> None:
        try:
            asyncio.run(ensure(settings, data_dir=data_dir, wait=120.0))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, name="embed-warm", daemon=True).start()


# ------------------------------------------------------------------ vectors on disk
def pack(vectors: "np.ndarray") -> list[bytes]:
    """Rows as fp16 bytes. Half the file for a difference no retrieval notices."""
    if np is None:
        return []
    return [np.asarray(v, dtype="<f2").tobytes() for v in np.asarray(vectors)]


def unpack(blob: bytes | None, dim: int = 0) -> "np.ndarray | None":
    """One row back as float32, or None when it is missing or not the shape the model makes."""
    if np is None or not blob:
        return None
    arr = np.frombuffer(blob, dtype="<f2")
    if dim and arr.shape[0] != dim:
        return None
    return arr.astype("float32")


def matrix(rows: list[bytes | None], dim: int) -> tuple["np.ndarray", list[int]]:
    """The index as one contiguous matrix, plus which input row each matrix row came from.

    The mapping is returned rather than assumed away: a row that is missing, or whose width is not
    this model's, is dropped — and dropping it shifts every row after it. Silently returning the
    matrix alone would give every hit past that point the *wrong* chunk, and a knowledge base that
    answers with the neighbouring passage is worse than one that answers with none.
    """
    keep: list[int] = []
    vecs: list["np.ndarray"] = []
    for i, blob in enumerate(rows):
        v = unpack(blob, dim)
        if v is not None:
            keep.append(i)
            vecs.append(v)
    if not vecs:
        return np.zeros((0, dim), dtype="float32"), []
    return np.vstack(vecs), keep


def best_per_document(query: "np.ndarray", mat: "np.ndarray", positions: list[int],
                      docs: list[str], *, pool: int = 3000,
                      allow: set[int] | None = None) -> list[tuple[str, int, float]]:
    """(document, its best passage, that passage's cosine), documents best first.

    Passages are the right unit to *score* and the wrong unit to *return*: a note in a real vault
    averages seventeen passages, so "the best twenty-four passages" is one or two notes — and a
    search that answers with five passages of the same page has answered once. Reducing to the best
    passage per document also reaches further than a fixed head of the passage list: a page whose
    best passage ranks 214th overall is around the thirtieth *document*, which is well inside any
    result list worth reading, while a head of twenty-four passages never sees it.

    `pool` bounds how many passages are examined, so the reduction stays a few milliseconds on a
    large library — and it is a bound worth knowing: a document whose best passage is outside the
    top `pool` of the whole index is one this model does not consider relevant at all.
    """
    if np is None or mat.shape[0] == 0 or query is None or query.shape[0] != mat.shape[1]:
        return []
    if len(docs) != mat.shape[0] or len(positions) != mat.shape[0]:
        return []
    sims = mat @ np.asarray(query, dtype="float32")
    rows = np.arange(sims.shape[0])
    if allow is not None:
        keep = np.fromiter((p in allow for p in positions), dtype=bool, count=len(positions))
        rows = rows[keep]
        if rows.shape[0] == 0:
            return []
    sub = sims[rows]
    if pool and sub.shape[0] > pool:
        top = np.argpartition(-sub, pool - 1)[:pool]
        rows, sub = rows[top], sub[top]
    best: dict[str, tuple[int, float]] = {}
    for r, s in zip(rows.tolist(), sub.tolist()):
        did = docs[r]
        seen = best.get(did)
        if seen is None or s > seen[1]:
            best[did] = (int(positions[r]), float(s))
    return sorted(((d, at, s) for d, (at, s) in best.items()), key=lambda x: (-x[2], x[1]))


def python_version_note() -> str:
    """One line about the interpreter, for the health panel: the mismatch is the whole reason this
    module exists, and a person debugging it deserves to see it stated."""
    return f"{sys.version.split()[0]} ({'numpy ok' if np is not None else 'numpy missing'})"
