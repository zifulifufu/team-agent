#!/usr/bin/env python3
"""A local embedding service, so a knowledge base can be searched by meaning rather than by word.

Why a separate process rather than a library call: the app's own virtualenv is x86_64 under
Rosetta on Python 3.14, and neither torch nor onnxruntime publishes wheels for it. The native
arm64 interpreter next to it can run the model, and Metal acceleration comes with it. So the
model lives here, in its own environment, and the app talks to it over loopback.

The wire format is OpenAI's `/v1/embeddings`, deliberately: a knowledge base indexed here can be
re-indexed through a cloud endpoint (or any other local server) by changing one address, and
every piece of client code written against this stays correct.

Two behaviours worth knowing, both of them about not lying to the caller:

* The weights are never downloaded behind your back. Without `--allow-download`, a missing model
  is reported as exactly that, with the command that would fetch it. A search that quietly pulls
  2 GB off the network is not a search.
* `/health` answers immediately, even while the model is still loading. "Up" and "ready" are
  different questions, and a caller that conflates them waits forever.

Run it (the app starts this for you; this is for doing it by hand):

    HF_ENDPOINT=https://hf-mirror.com .venv-embed/bin/python scripts/embed-server.py
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_PORT = 8799
MAX_BATCH = 64
MAX_CHARS = 8000          # per text; a chunk is ~600 characters, so this only bites on odd input


class Bed:
    """The model, its state, and the lock that keeps one encode running at a time.

    `sentence-transformers` is not safe to call from two threads at once, and the server is
    threaded so that `/health` stays answerable while a batch is being embedded.
    """

    def __init__(self, model_id: str, device: str, allow_download: bool, batch: int) -> None:
        self.model_id = model_id
        self.want_device = device
        self.allow_download = allow_download
        self.batch = batch
        self.lock = threading.Lock()
        self.state = "loading"           # loading | ready | error
        self.model: Any = None
        self.device = ""
        self.dim = 0
        self.max_seq = 0
        self.error = ""
        self.hint = ""
        self.downloaded = False
        self.started = time.time()
        self.load_seconds = 0.0
        self.embedded = 0                # texts embedded since start; a cheap liveness signal

    # ------------------------------------------------------------------ loading
    def boot(self) -> None:
        try:
            self._load()
        except Exception as e:  # noqa: BLE001 — whatever went wrong is what the caller is told
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"[:600]
            self.hint = self._download_hint()
            print(f"embed-server: could not load {self.model_id}: {self.error}", file=sys.stderr, flush=True)
            print(f"embed-server: {self.hint}", file=sys.stderr, flush=True)

    def _load(self) -> None:
        t0 = time.time()
        import torch
        from sentence_transformers import SentenceTransformer

        device = self.want_device
        if device == "auto":
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        try:
            # First try the local cache alone: this is the branch that must not touch the network.
            self.model = SentenceTransformer(self.model_id, device=device, local_files_only=True)
        except Exception:
            if not self.allow_download:
                raise RuntimeError(
                    f"the weights of {self.model_id} are not in the local cache "
                    f"(and this server was not started with --allow-download)") from None
            print(f"embed-server: fetching {self.model_id} …", file=sys.stderr, flush=True)
            self.model = SentenceTransformer(self.model_id, device=device)
            self.downloaded = True
        self.dim = int(self._dimension() or 0)
        self.max_seq = int(getattr(self.model, "max_seq_length", 0) or 0)
        self.load_seconds = round(time.time() - t0, 1)
        self.state = "ready"
        print(f"embed-server: {self.model_id} ready on {device} in {self.load_seconds}s "
              f"(dim {self.dim}, max_seq {self.max_seq})", file=sys.stderr, flush=True)

    def _dimension(self) -> int:
        """`sentence-transformers` 6 renamed this method; both names are asked in turn rather than
        pinned to one, because a sidecar that breaks on a dependency upgrade breaks searching."""
        for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
            fn = getattr(self.model, name, None)
            if callable(fn):
                return int(fn() or 0)
        return 0

    def _download_hint(self) -> str:
        exe = sys.executable
        script = os.path.abspath(__file__)
        return (f"fetch it with: HF_ENDPOINT=https://hf-mirror.com {exe} {script} "
                f"--model {self.model_id} --allow-download")

    # ------------------------------------------------------------------ encoding
    def encode(self, texts: list[str]) -> list[list[float]]:
        if self.state != "ready" or self.model is None:
            raise NotReady(self.state)
        clean = [(t[:MAX_CHARS] if t.strip() else " ") for t in texts]
        with self.lock:
            vecs = self.model.encode(clean, batch_size=self.batch, normalize_embeddings=True,
                                     show_progress_bar=False, convert_to_numpy=True)
        self.embedded += len(clean)
        return vecs

    def status(self) -> dict:
        return {"ready": self.state == "ready", "state": self.state, "model": self.model_id,
                "device": self.device, "dim": self.dim, "max_seq": self.max_seq,
                "error": self.error, "hint": self.hint, "downloaded_now": self.downloaded,
                "load_seconds": self.load_seconds, "up_seconds": round(time.time() - self.started, 1),
                "embedded": self.embedded, "max_batch": MAX_BATCH, "allow_download": self.allow_download}


class NotReady(Exception):
    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(state)


def _floats_to_arrays(vecs: Any) -> list[list[float]]:
    return [[float(x) for x in v] for v in vecs]


def _floats_to_base64(vecs: Any) -> list[str]:
    """float32 little-endian, base64 — the same encoding OpenAI offers, and four times smaller
    on the wire than a JSON array of decimals."""
    out: list[str] = []
    for v in vecs:
        raw = struct.pack("<%df" % len(v), *[float(x) for x in v])
        out.append(base64.b64encode(raw).decode("ascii"))
    return out


def make_handler(bed: Bed) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "team-agent-embed/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:   # quieter than the default per-request line
            if os.environ.get("EMBED_SERVER_VERBOSE"):
                sys.stderr.write("embed-server: " + (fmt % args) + "\n")

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — the name http.server dispatches on
            path = self.path.split("?")[0].rstrip("/") or "/"
            # Both spellings, because the address a person configures is OpenAI-style and ends in
            # `/v1` — a health check that only answers at the root would 404 there, and a 404 reads
            # as "up but not ready", which is a caller polling a ready server until it gives up.
            if path.startswith("/v1"):
                path = path[3:] or "/"
            if path in ("/health", "/healthz"):
                self._send(200, bed.status())
            elif path in ("/models",):
                self._send(200, {"object": "list", "data": [
                    {"id": bed.model_id, "object": "model", "owned_by": "local"}]})
            else:
                self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0].rstrip("/")
            if path not in ("/v1/embeddings", "/embeddings"):
                self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
                return
            try:
                n = int(self.headers.get("content-length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError) as e:
                self._send(400, {"error": {"message": f"bad JSON body: {e}", "type": "invalid_request_error"}})
                return
            if bed.state != "ready":
                # 503 rather than 500: it is a state, not a fault, and the caller is told which one.
                self._send(503, {"error": {"message": bed.error or f"the model is still {bed.state}",
                                           "type": "not_ready", "state": bed.state, "hint": bed.hint}})
                return
            raw = req.get("input")
            if isinstance(raw, str):
                texts = [raw]
            elif isinstance(raw, list) and all(isinstance(t, str) for t in raw):
                texts = raw
            else:
                self._send(400, {"error": {"message": "input must be a string or a list of strings",
                                           "type": "invalid_request_error"}})
                return
            if not texts:
                self._send(400, {"error": {"message": "input is empty", "type": "invalid_request_error"}})
                return
            if len(texts) > MAX_BATCH:
                self._send(400, {"error": {"message": f"at most {MAX_BATCH} texts per request (got {len(texts)})",
                                           "type": "invalid_request_error"}})
                return
            t0 = time.time()
            try:
                vecs = bed.encode(texts)
            except NotReady as e:
                self._send(503, {"error": {"message": f"the model is {e.state}", "type": "not_ready",
                                           "state": e.state}})
                return
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"message": f"{type(e).__name__}: {e}"[:600], "type": "server_error"}})
                return
            fmt = str(req.get("encoding_format") or "float")
            data = []
            if fmt == "base64":
                packed = _floats_to_base64(vecs)
                for i, blob in enumerate(packed):
                    data.append({"object": "embedding", "index": i, "embedding": blob, "encoding_format": "base64"})
            else:
                for i, v in enumerate(_floats_to_arrays(vecs)):
                    data.append({"object": "embedding", "index": i, "embedding": v})
            self._send(200, {"object": "list", "data": data, "model": bed.model_id, "dim": bed.dim,
                             "usage": {"prompt_tokens": 0, "total_tokens": 0},
                             "seconds": round(time.time() - t0, 3)})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="Local embedding service for Team Agent")
    ap.add_argument("--model", default=os.environ.get("EMBED_MODEL") or DEFAULT_MODEL)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("EMBED_PORT") or DEFAULT_PORT))
    ap.add_argument("--device", default=os.environ.get("EMBED_DEVICE") or "auto", choices=("auto", "mps", "cpu"))
    ap.add_argument("--allow-download", action="store_true",
                    help="fetch the weights if they are not cached (otherwise a missing model is reported, not fetched)")
    ap.add_argument("--batch", type=int, default=8, help="how many texts the model encodes at once")
    args = ap.parse_args()

    bed = Bed(args.model, args.device, args.allow_download, max(1, args.batch))
    threading.Thread(target=bed.boot, name="boot", daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(bed))
    httpd.daemon_threads = True
    print(f"embed-server: listening on http://{args.host}:{args.port} (model {args.model})",
          file=sys.stderr, flush=True)

    stop = threading.Event()

    def _bye(_signum: int, _frame: Any) -> None:
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    try:
        httpd.serve_forever(poll_interval=0.3)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
