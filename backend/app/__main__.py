"""Entry point: python -m app [--port 8765]"""

import argparse
import ipaddress
import os

import uvicorn

from .main import create_app


def loopback_only(host: str) -> bool:
    """Can only this machine reach an address like this?

    Deliberately not `net.is_local_url`, which answers a different question ("should this go
    through the proxy?") and therefore counts `0.0.0.0` and the LAN ranges as local — the two
    cases where the warning below matters most.
    """
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False                       # a hostname, a LAN address, or an empty host: reachable


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if not loopback_only(args.host) and not os.environ.get("TEAM_AGENT_TOKEN"):
        # The default is loopback, so reaching this line is a deliberate act — say out loud what
        # it means: with no token configured, every /api route answers anyone who can reach the
        # port, and a non-loopback bind puts it on the network. The rest of the app's model is
        # "one local user"; this is the one setting that changes that without asking.
        print(
            f"\n  !! --host {args.host} is not a loopback address, and TEAM_AGENT_TOKEN is empty:\n"
            "  !! every /api route will answer anybody who can reach this port (chats, API keys,\n"
            "  !! library). Set TEAM_AGENT_TOKEN, or bind to 127.0.0.1.\n",
            flush=True,
        )
    uvicorn.run(create_app(background=True), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
