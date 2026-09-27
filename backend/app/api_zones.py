"""Zones over HTTP — the sidebar list, and one zone's four surfaces.

Two endpoints rather than one, and the split is about cost rather than taste: the sidebar is drawn
on every render and needs four words per zone, while a zone page needs live shelf counts and the
composer's preset list. Fetching the second to draw the first would put a directory scan behind
every navigation redraw.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import i18n, zones


def build_zones_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/zones")
    async def zone_list() -> dict:
        """What the sidebar draws, plus any of the user's own zone files that could not be read.

        The errors ride along instead of being swallowed: a zone file with a broken field would
        otherwise simply not be in the list, and "my zone is gone" has no answer anywhere.
        """
        return {"zones": zones.catalog(), "errors": zones.errors()}

    @r.get("/api/zones/{zone_id}")
    async def zone_detail(zone_id: str) -> dict:
        got = zones.detail(zone_id)
        if got is None:
            known = ", ".join(z["id"] for z in zones.catalog())
            raise HTTPException(404, i18n.pick_now(
                f"There is no zone called \"{zone_id}\". Known zones: {known}.",
                f"没有叫「{zone_id}」的专区。现有的专区:{known}。"))
        return {"zone": got, "errors": zones.errors()}

    return r
