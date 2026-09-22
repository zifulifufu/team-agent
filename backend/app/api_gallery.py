"""Template gallery endpoints: catalog / detail / one-click apply.

  * `GET  /api/gallery`            -- catalog (categories, entries, installed state, loading state of custom templates)
  * `GET  /api/gallery/{id}`       -- single entry detail (full body, for reading before install)
  * `POST /api/gallery/{id}/apply` -- one-click apply (create team / create member / install skill / save prompt / add MCP)

Applying is idempotent: repeating it does not create duplicate skills, prompts, or
MCP entries; members are reused by name; a team is created fresh each time (a
duplicate name gets an automatic suffix). Everything only writes text to the local
database and the skills directory.
"""

from __future__ import annotations

from typing import Any

from . import i18n

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import gallery
from .store import Store


class ApplyIn(BaseModel):
    name: str | None = None          # team: name for the group (defaults to the template name; duplicates get a suffix)
    group_id: str | None = None      # agent: which group to pull it into
    overwrite: bool = False          # skill / prompt: whether to overwrite an existing entry with the template version


def build_gallery_router(store: Store, hooks: Any = None) -> APIRouter:
    r = APIRouter()

    @r.get("/api/gallery")
    async def overview() -> dict:
        return gallery.overview(store)

    @r.get("/api/gallery/{item_id}")
    async def detail(item_id: str) -> dict:
        it = gallery.find(store, item_id)
        if it is None:
            raise HTTPException(404, i18n.pick_now("That template does not exist", "模板不存在"))
        return it

    @r.post("/api/gallery/{item_id}/apply")
    async def apply_template(item_id: str, body: ApplyIn) -> dict:
        try:
            out = gallery.apply(store, item_id, body.model_dump())
        except gallery.GalleryError as e:
            raise HTTPException(400, str(e)) from None
        # A hook template is just two files written to the hooks directory, so the manager has to
        # be told to look again — otherwise the entry says "installed" while the panel does not
        # list it, which is exactly the kind of half-done state this app tries to avoid.
        if out.get("kind") == "hook" and hooks is not None:
            hooks.load()
        return out

    return r
