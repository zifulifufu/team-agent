"""启动: python -m app [--port 8765]"""

import argparse

import uvicorn

from .main import create_app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    uvicorn.run(create_app(background=True), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
