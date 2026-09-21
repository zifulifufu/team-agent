"""Real HTTP fakes, so LiteLLM goes through the whole network path
(rather than mocking litellm itself).
"""
import json
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    def __init__(self, app: FastAPI):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fake server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def openai_like(reply: str = "来自云端", status: int = 200):
    """OpenAI-compatible /chat/completions (streaming SSE).

    A status other than 200 simulates failures such as bad credentials.
    """
    app = FastAPI()
    app.state.hits = 0

    @app.post("/{path:path}/chat/completions")
    @app.post("/chat/completions")
    async def chat(request: Request, path: str = ""):
        app.state.hits += 1
        body = await request.json()
        app.state.last_body = body
        if status != 200:
            return JSONResponse({"error": {"message": "Authentication Fails", "type": "auth"}}, status_code=status)

        def gen():
            for i in range(0, len(reply), 2):
                c = {"id": "x", "object": "chat.completion.chunk", "model": body["model"],
                     "choices": [{"index": 0, "delta": {"content": reply[i:i + 2]}, "finish_reason": None}]}
                yield f"data: {json.dumps(c, ensure_ascii=False)}\n\n"
            end = {"id": "x", "object": "chat.completion.chunk", "model": body["model"],
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(end)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def ollama_like(reply: str = "来自本地"):
    """Ollama /api/chat (NDJSON stream)."""
    app = FastAPI()
    app.state.hits = 0

    @app.post("/api/chat")
    async def chat(request: Request):
        app.state.hits += 1
        body = await request.json()
        app.state.last_body = body

        def gen():
            for i in range(0, len(reply), 2):
                yield json.dumps({"model": body["model"], "created_at": "2026-01-01T00:00:00Z",
                                  "message": {"role": "assistant", "content": reply[i:i + 2]}, "done": False},
                                 ensure_ascii=False) + "\n"
            yield json.dumps({"model": body["model"], "created_at": "2026-01-01T00:00:00Z",
                              "message": {"role": "assistant", "content": ""}, "done": True,
                              "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 5}) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    return app
