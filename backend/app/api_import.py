"""The routes behind "import from another AI application".

Read-only discovery, then an explicit import of what was ticked. Nothing here starts a
process or enables a server: an imported MCP server lands disabled, and importing a skill
only writes text.

Splitting discovery from import is deliberate. The scan reports what is on the machine; the
import is a separate request that names exactly what to take, and it re-reads the source
file rather than accepting the definition back from the client — which is what keeps a
credential inside this machine and makes the preview the client saw irrelevant to what is
actually written.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import i18n, import_sources


class ScanIn(BaseModel):
    sources: list[str] | None = None      # None = every known source


class TakeIn(BaseModel):
    source: str
    names: list[str] = []


def build_import_router(store: Any) -> APIRouter:
    r = APIRouter()

    @r.get("/api/import/sources")
    async def sources() -> dict:
        """What other AI applications were found on this machine, and how much is in them.

        Reading only: no request leaves the machine, and no file outside the fixed list in
        `import_sources.SOURCES` is opened.
        """
        return {"sources": i18n.localize(import_sources.sources(store))}

    @r.post("/api/import/scan")
    async def scan(body: ScanIn | None = None) -> dict:
        """Scan everything, or only the named sources. A request without a body means
        everything, which is the useful default rather than a 422."""
        got = import_sources.scan(store, body.sources if body else None)
        return i18n.localize(got)

    @r.post("/api/import/mcp")
    async def take_mcp(body: TakeIn) -> dict:
        if not body.names:
            raise HTTPException(400, i18n.pick_now("Nothing was selected", "没有选中任何条目"))
        try:
            got = import_sources.import_mcp(store, body.source, body.names)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"added": [m["name"] for m in got["added"]], "skipped": got["skipped"]}

    @r.post("/api/import/skills")
    async def take_skills(body: TakeIn) -> dict:
        if not body.names:
            raise HTTPException(400, i18n.pick_now("Nothing was selected", "没有选中任何条目"))
        try:
            got = import_sources.import_skills(store, body.source, body.names)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return got

    return r
